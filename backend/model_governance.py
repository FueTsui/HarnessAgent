"""模型路由、运行健康与可追溯价格治理。

本模块刻意复用现有追加式 ``Item`` 与 ``AppSetting``，不引入第二套任务事实：

* 每次真实模型尝试写 ``provider.attempt``，故障切换写 ``provider.fallback``；
* 健康状态从最近尝试事件确定性计算，不用进程内瞬态计数冒充共享状态；
* 价格按生效时间追加保存，历史 TokenUsage 始终按当时有效版本估算；
* 路由只在已授权、已启用且能力匹配的 Provider 集合内选择，绝不扩大权限。
"""
from __future__ import annotations

import datetime as dt
import inspect
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from .models import AppSetting, Item, ModelProvider, iso_utc
from .logging_utils import redact_log_value


logger = logging.getLogger(__name__)

PRICING_SETTING_KEY = "model_pricing_v1"
_PRICE_FIELDS = (
    "input_microusd_per_million",
    "output_microusd_per_million",
    "cached_microusd_per_million",
    "reasoning_microusd_per_million",
)
_DEFAULT_HEALTH = {
    "enabled": True,
    "lookback_minutes": 60,
    "min_samples": 3,
    "max_error_rate": 0.6,
    "consecutive_failures": 3,
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _json(value: str, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def _provider_modalities(provider: ModelProvider) -> set[str]:
    value = _json(getattr(provider, "model_input", ""), ["text"])
    return {str(item) for item in value} if isinstance(value, list) else {"text"}


def _event_payload(item: Item) -> dict:
    value = _json(item.payload, {})
    return value if isinstance(value, dict) else {}


def provider_health(
    db: Session,
    provider_id: int,
    *,
    lookback_minutes: int = 60,
    min_samples: int = 3,
    max_error_rate: float = 0.6,
    consecutive_failures: int = 3,
) -> dict:
    """按共享事件事实计算 Provider 健康，不泄露上游错误正文。"""
    lookback_minutes = max(1, min(int(lookback_minutes or 60), 7 * 24 * 60))
    cutoff = utc_now() - dt.timedelta(minutes=lookback_minutes)
    rows = (
        db.query(Item)
        .filter(Item.name == "provider.attempt", Item.created_at >= cutoff)
        .order_by(Item.created_at.asc(), Item.sequence.asc())
        .all()
    )
    attempts: list[tuple[Item, dict]] = []
    for row in rows:
        payload = _event_payload(row)
        if int(payload.get("provider_id") or 0) == int(provider_id):
            attempts.append((row, payload))
    total = len(attempts)
    failures = sum(1 for _, payload in attempts if not bool(payload.get("ok")))
    successes = total - failures
    consecutive = 0
    for _, payload in reversed(attempts):
        if bool(payload.get("ok")):
            break
        consecutive += 1
    latencies = [
        max(0, int(payload.get("latency_ms") or 0))
        for _, payload in attempts
        if bool(payload.get("ok")) and int(payload.get("latency_ms") or 0) >= 0
    ]
    error_rate = failures / total if total else 0.0
    unhealthy = total >= max(1, int(min_samples or 1)) and (
        error_rate >= max(0.0, min(float(max_error_rate), 1.0))
        or consecutive >= max(1, int(consecutive_failures or 1))
    )
    last_item = attempts[-1][0] if attempts else None
    last_payload = attempts[-1][1] if attempts else {}
    return {
        "provider_id": int(provider_id),
        "state": "unhealthy" if unhealthy else ("healthy" if total else "unknown"),
        "lookback_minutes": lookback_minutes,
        "samples": total,
        "successes": successes,
        "failures": failures,
        "error_rate": round(error_rate, 4),
        "consecutive_failures": consecutive,
        "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "last_attempt_at": iso_utc(last_item.created_at) if last_item is not None else "",
        "last_ok": bool(last_payload.get("ok")) if attempts else None,
    }


def _price_entries(db: Session) -> list[dict]:
    row = db.get(AppSetting, PRICING_SETTING_KEY)
    values = _json(row.value if row is not None else "", [])
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def price_entries(db: Session, provider_id: int | None = None) -> list[dict]:
    values = _price_entries(db)
    if provider_id is not None:
        values = [
            item for item in values
            if int(item.get("provider_id") or 0) == int(provider_id)
        ]
    return sorted(values, key=lambda item: str(item.get("effective_from") or ""))


def usd_per_million_to_microusd(value: Any) -> int:
    """把用户输入的 USD/百万 Token 精确换成 micro-USD/百万 Token。"""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("价格必须是有效数字")
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000"):
        raise ValueError("价格必须在 0 到 1,000,000 USD/百万 Token 之间")
    return int((amount * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def microusd_to_usd(value: int) -> str:
    amount = Decimal(int(value or 0)) / Decimal(1_000_000)
    return format(amount.normalize(), "f") if amount else "0"


def append_price(
    db: Session,
    *,
    provider_id: int,
    model: str,
    input_usd_per_million: Any,
    output_usd_per_million: Any,
    cached_usd_per_million: Any = 0,
    reasoning_usd_per_million: Any = 0,
    created_by: int | None = None,
    effective_from: dt.datetime | None = None,
    priced: bool = True,
) -> dict:
    """追加一个价格版本；旧版本不改写、不删除。"""
    provider = db.get(ModelProvider, int(provider_id))
    if provider is None:
        raise ValueError("模型提供商不存在")
    effective = _aware(effective_from) or utc_now()
    entry = {
        "id": uuid.uuid4().hex,
        "provider_id": int(provider_id),
        "model": str(model or provider.model_id or "")[:256],
        "priced": bool(priced),
        "effective_from": effective.isoformat(),
        "created_by": int(created_by) if created_by is not None else None,
        "created_at": utc_now().isoformat(),
        "input_microusd_per_million": usd_per_million_to_microusd(input_usd_per_million),
        "output_microusd_per_million": usd_per_million_to_microusd(output_usd_per_million),
        "cached_microusd_per_million": usd_per_million_to_microusd(cached_usd_per_million),
        "reasoning_microusd_per_million": usd_per_million_to_microusd(reasoning_usd_per_million),
    }
    values = _price_entries(db)
    values.append(entry)
    row = db.get(AppSetting, PRICING_SETTING_KEY)
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if row is None:
        db.add(AppSetting(key=PRICING_SETTING_KEY, value=encoded))
    else:
        row.value = encoded
    db.commit()
    return public_price(entry)


def public_price(entry: dict | None) -> dict | None:
    if not entry:
        return None
    return {
        **{key: entry.get(key) for key in (
            "id", "provider_id", "model", "priced", "effective_from", "created_at"
        )},
        "input_usd_per_million": microusd_to_usd(entry.get("input_microusd_per_million", 0)),
        "output_usd_per_million": microusd_to_usd(entry.get("output_microusd_per_million", 0)),
        "cached_usd_per_million": microusd_to_usd(entry.get("cached_microusd_per_million", 0)),
        "reasoning_usd_per_million": microusd_to_usd(entry.get("reasoning_microusd_per_million", 0)),
    }


def effective_price(
    db: Session,
    provider_id: int | None,
    model: str,
    at: dt.datetime | None = None,
) -> dict | None:
    if provider_id is None:
        return None
    instant = _aware(at) or utc_now()
    matches: list[tuple[dt.datetime, dict]] = []
    for entry in price_entries(db, int(provider_id)):
        entry_model = str(entry.get("model") or "")
        if entry_model not in {"", "*", str(model or "")}:
            continue
        try:
            effective = dt.datetime.fromisoformat(str(entry.get("effective_from") or ""))
        except ValueError:
            continue
        effective = _aware(effective)
        if effective is not None and effective <= instant:
            matches.append((effective, entry))
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0])
    return matches[-1][1]


def estimate_cost(
    db: Session,
    *,
    provider_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    at: dt.datetime | None = None,
) -> dict:
    entry = effective_price(db, provider_id, model, at)
    if entry is None or not bool(entry.get("priced", True)):
        return {"status": "unknown", "microusd": 0, "usd": None, "price_id": None}
    cached = max(0, min(int(cached_tokens or 0), int(input_tokens or 0)))
    reasoning = max(0, min(int(reasoning_tokens or 0), int(output_tokens or 0)))
    ordinary_input = max(0, int(input_tokens or 0) - cached)
    ordinary_output = max(0, int(output_tokens or 0) - reasoning)
    components = (
        (ordinary_input, int(entry.get("input_microusd_per_million") or 0)),
        (ordinary_output, int(entry.get("output_microusd_per_million") or 0)),
        (cached, int(entry.get("cached_microusd_per_million") or 0)),
        (reasoning, int(entry.get("reasoning_microusd_per_million") or 0)),
    )
    total = sum(
        int((Decimal(tokens) * Decimal(rate) / Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        ))
        for tokens, rate in components
    )
    return {
        "status": "estimated",
        "microusd": total,
        "usd": microusd_to_usd(total),
        "price_id": entry.get("id"),
    }


def provider_unit_cost(db: Session, provider: ModelProvider) -> int | None:
    entry = effective_price(db, provider.id, provider.model_id, utc_now())
    if entry is None or not bool(entry.get("priced", True)):
        return None
    # 路由只比较统一的 1M 输入 + 1M 输出基准；真实账单仍按四类 Token 精算。
    return int(entry.get("input_microusd_per_million") or 0) + int(
        entry.get("output_microusd_per_million") or 0
    )


@dataclass
class ProviderRoute:
    primary: ModelProvider | None
    fallbacks: list[ModelProvider] = field(default_factory=list)
    reason: str = "fixed"
    health: dict[int, dict] = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "planned_provider_id": self.primary.id if self.primary is not None else None,
            "fallback_provider_ids": [provider.id for provider in self.fallbacks],
            "reason": self.reason,
            "health": self.health,
        }


def _normalise_health(value: Any) -> dict:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": bool(source.get("enabled", _DEFAULT_HEALTH["enabled"])),
        "lookback_minutes": max(1, min(int(source.get("lookback_minutes") or 60), 10080)),
        "min_samples": max(1, min(int(source.get("min_samples") or 3), 1000)),
        "max_error_rate": max(0.05, min(float(source.get("max_error_rate") or 0.6), 1.0)),
        "consecutive_failures": max(1, min(int(source.get("consecutive_failures") or 3), 100)),
    }


def _matches_rule(rule: dict, query: str, required_modalities: set[str]) -> bool:
    match, value = str(rule.get("match") or ""), rule.get("value")
    if match == "keyword":
        return bool(value) and str(value) in (query or "")
    if match == "length_gt":
        try:
            return len(query or "") > int(value)
        except (TypeError, ValueError):
            return False
    if match == "requires_image":
        return "image" in required_modalities
    if match == "reasoning":
        return bool(value) and any(token in (query or "").lower() for token in (
            "推理", "证明", "分析", "reason", "prove", "analysis"
        ))
    return False


def resolve_route(
    db: Session,
    agent,
    query: str = "",
    *,
    required_modalities: Iterable[str] = ("text",),
) -> ProviderRoute:
    """解析能力、健康和成本感知路由；无候选时安全回退既有固定线路。"""
    routing = _json(getattr(agent, "routing", ""), {})
    if not isinstance(routing, dict):
        routing = {}
    modalities = {str(value) for value in required_modalities} | {"text"}
    mode = str(routing.get("mode") or "fixed")
    primary_id = getattr(agent, "provider_id", None)
    rules = routing.get("rules") if isinstance(routing.get("rules"), list) else []
    if mode in {"rules", "policy"}:
        for rule in rules:
            if isinstance(rule, dict) and _matches_rule(rule, query, modalities):
                primary_id = rule.get("provider_id")
                break
        else:
            primary_id = routing.get("default_provider_id") or primary_id
    fallback_ids = routing.get("fallback_provider_ids") if mode == "policy" else []
    if not isinstance(fallback_ids, list):
        fallback_ids = []
    ordered_ids: list[int] = []
    for raw in [primary_id, *fallback_ids]:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in ordered_ids:
            ordered_ids.append(value)
    providers = [db.get(ModelProvider, value) for value in ordered_ids]
    providers = [
        provider for provider in providers
        if provider is not None and provider.enabled and provider.model_id
        and modalities.issubset(_provider_modalities(provider))
    ]
    if not providers:
        fixed = db.get(ModelProvider, getattr(agent, "provider_id", None)) if getattr(agent, "provider_id", None) else None
        return ProviderRoute(
            primary=fixed if fixed is not None and fixed.enabled else None,
            reason="environment_default" if fixed is None else "fixed_last_resort",
        )
    if mode != "policy":
        return ProviderRoute(primary=providers[0], reason="rules" if mode == "rules" else "fixed")
    health_policy = _normalise_health(routing.get("health"))
    health_map = {
        provider.id: provider_health(db, provider.id, **{
            key: value for key, value in health_policy.items() if key != "enabled"
        })
        for provider in providers
    }
    healthy = [
        provider for provider in providers
        if not health_policy["enabled"] or health_map[provider.id]["state"] != "unhealthy"
    ]
    reason = "ordered"
    if not healthy:
        healthy = providers[:]
        reason = "all_unhealthy_last_resort"
    elif healthy[0].id != providers[0].id:
        reason = "unhealthy_primary_bypassed"
    strategy = str(routing.get("strategy") or "ordered")
    if strategy == "lowest_cost":
        indexed = {provider.id: index for index, provider in enumerate(healthy)}
        healthy.sort(key=lambda provider: (
            provider_unit_cost(db, provider) is None,
            provider_unit_cost(db, provider) or math.inf,
            indexed[provider.id],
        ))
        reason = "lowest_cost" if reason == "ordered" else f"{reason}+lowest_cost"
    return ProviderRoute(
        primary=healthy[0],
        fallbacks=healthy[1:],
        reason=reason,
        health=health_map,
    )


def validate_policy_routing(
    db: Session,
    user,
    routing: dict,
    validate_provider: Callable[[int], None],
) -> dict:
    """规范化新路由合同；旧 rules 合同由调用方兼容处理。"""
    if not isinstance(routing, dict) or routing.get("mode") != "policy":
        raise ValueError("模型路由 mode 必须为 policy")
    default_raw = routing.get("default_provider_id")
    default_id = int(default_raw) if default_raw not in (None, "") else None
    if default_id is not None:
        validate_provider(default_id)
    fallbacks: list[int] = []
    for raw in routing.get("fallback_provider_ids") or []:
        provider_id = int(raw)
        validate_provider(provider_id)
        if provider_id != default_id and provider_id not in fallbacks:
            fallbacks.append(provider_id)
    if len(fallbacks) > 8:
        raise ValueError("故障切换 Provider 最多 8 个")
    rules = []
    raw_rules = routing.get("rules") or []
    if not isinstance(raw_rules, list):
        raise ValueError("模型路由 rules 必须为数组")
    if len(raw_rules) > 1024:
        raise ValueError("模型路由规则最多 1024 条；请调整后保存，不会截断已有规则")
    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        match = str(raw.get("match") or "keyword")
        if match not in {"keyword", "length_gt", "requires_image", "reasoning"}:
            raise ValueError(f"不支持的模型路由匹配类型：{match}")
        provider_id = int(raw.get("provider_id"))
        validate_provider(provider_id)
        value = str(raw.get("value") or "")
        if len(value) > 16000:
            raise ValueError("单条模型路由匹配值最多 16000 字符；请调整后保存，不会截断已有值")
        rules.append({"match": match, "value": value, "provider_id": provider_id})
    return {
        "mode": "policy",
        "default_provider_id": default_id,
        "fallback_provider_ids": fallbacks,
        "strategy": (
            "lowest_cost" if routing.get("strategy") == "lowest_cost" else "ordered"
        ),
        "health": _normalise_health(routing.get("health")),
        "rules": rules,
    }


async def _emit(callback, event_type: str, payload: dict) -> None:
    if callback is None:
        return
    try:
        value = callback(event_type, payload)
        if inspect.isawaitable(value):
            await value
    except Exception as exc:  # noqa: BLE001 - observability must not change model outcome
        logger.warning("Provider governance event write failed (%s): %s", event_type, exc)


def _safe_error(exc: Exception) -> str:
    value = redact_log_value(str(exc or "")).replace("\r", " ").replace("\n", " ")
    # 事件仅需稳定分类；截断正文可避免上游响应意外携带凭据或大段内容。
    return value[:240]


class GovernedLLMClient:
    """在尚未产生用户可见增量时执行有界 Provider 故障切换。"""

    def __init__(
        self,
        clients: list[tuple[int | None, str, Any]],
        *,
        runtime_event=None,
        reason: str = "ordered",
    ) -> None:
        if not clients:
            raise ValueError("至少需要一个模型客户端")
        self._clients = clients
        self._runtime_event = runtime_event
        self.route_reason = reason
        primary = clients[0][2]
        # Runtime 会读取这些通用属性做上下文预算、能力判断和审计展示。
        for name in (
            "context_tokens", "text_model", "vision_model", "model_input",
            "provider_id", "base_url", "wire_api", "model_id",
        ):
            if hasattr(primary, name):
                setattr(self, name, getattr(primary, name))

    def __getattr__(self, name: str):
        return getattr(self._clients[0][2], name)

    async def _invoke(self, method: str, *args, **kwargs):
        last_error: Exception | None = None
        for index, (provider_id, model, client) in enumerate(self._clients):
            emitted = False
            call_kwargs = dict(kwargs)
            if method == "chat_messages_stream" and call_kwargs.get("on_delta") is not None:
                original = call_kwargs["on_delta"]

                def tracked(delta, _callback=original):
                    nonlocal emitted
                    if delta:
                        emitted = True
                    return _callback(delta)

                call_kwargs["on_delta"] = tracked
            started = time.monotonic()
            try:
                result = await getattr(client, method)(*args, **call_kwargs)
                latency = round((time.monotonic() - started) * 1000)
                await _emit(self._runtime_event, "provider.attempt", {
                    "provider_id": provider_id,
                    "model": model,
                    "ok": True,
                    "latency_ms": latency,
                    "attempt_index": index,
                    "route_reason": self.route_reason,
                })
                return result
            except Exception as exc:  # noqa: BLE001 - 结构化失败后决定是否切换
                if getattr(exc, "code", "") == "guardrail_content_blocked":
                    raise
                last_error = exc
                latency = round((time.monotonic() - started) * 1000)
                await _emit(self._runtime_event, "provider.attempt", {
                    "provider_id": provider_id,
                    "model": model,
                    "ok": False,
                    "latency_ms": latency,
                    "attempt_index": index,
                    "error_class": type(exc).__name__,
                    "error": _safe_error(exc),
                    "route_reason": self.route_reason,
                })
                if emitted or index + 1 >= len(self._clients):
                    raise
                next_id, next_model, _ = self._clients[index + 1]
                await _emit(self._runtime_event, "provider.fallback", {
                    "from_provider_id": provider_id,
                    "to_provider_id": next_id,
                    "from_model": model,
                    "to_model": next_model,
                    "reason": type(exc).__name__,
                })
        if last_error is not None:
            raise last_error
        raise RuntimeError("没有可用模型 Provider")

    async def chat(self, *args, **kwargs):
        return await self._invoke("chat", *args, **kwargs)

    async def chat_with_tools(self, *args, **kwargs):
        return await self._invoke("chat_with_tools", *args, **kwargs)

    async def chat_messages_stream(self, *args, **kwargs):
        return await self._invoke("chat_messages_stream", *args, **kwargs)

    async def vision(self, *args, **kwargs):
        return await self._invoke("vision", *args, **kwargs)

    async def ping(self, *args, **kwargs):
        return await self._invoke("ping", *args, **kwargs)


def governed_client(
    primary_snapshot: dict,
    fallback_snapshots: list[dict],
    *,
    client_factory: Callable[[dict], Any],
    runtime_event=None,
    reason: str = "ordered",
):
    snapshots = [primary_snapshot, *(fallback_snapshots or [])]
    clients = []
    for snapshot in snapshots:
        client = client_factory(snapshot)
        clients.append((snapshot.get("id"), str(snapshot.get("model_id") or ""), client))
    if len(clients) == 1:
        return clients[0][2]
    return GovernedLLMClient(clients, runtime_event=runtime_event, reason=reason)
