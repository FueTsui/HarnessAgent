"""模型 Token 用量采集。

调用上下文通过 ContextVar 从 worker 传到任意深度的模型调用；模型客户端只需把上游
响应交给 ``record_response_usage``，无需依赖 API/任务实现。未处于用户任务上下文时
（例如管理员“测试连接”）不会写入用户用量。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from dataclasses import dataclass
from typing import Iterator

from .database import SessionLocal
import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import TokenUsage, User, UserTokenLimit

logger = logging.getLogger(__name__)

PERIOD_FIELDS = ("weekly", "monthly", "total")


def _as_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _later(*values: datetime.datetime | None) -> datetime.datetime:
    normalized = [value for value in (_as_utc(item) for item in values) if value is not None]
    return max(normalized)


def usage_period_starts(
    policy: UserTokenLimit | None,
    *,
    now: datetime.datetime | None = None,
) -> dict[str, datetime.datetime | None]:
    """返回本周、本月和总量统计起点；手动重置基线仅影响当前自然周期。"""
    current = _as_utc(now) or _utc_now()
    week_start = (current - datetime.timedelta(days=current.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "weekly": _later(week_start, getattr(policy, "weekly_reset_at", None)),
        "monthly": _later(month_start, getattr(policy, "monthly_reset_at", None)),
        "total": None,
    }


def get_or_create_limits(db: Session, user_id: int) -> UserTokenLimit:
    policy = db.get(UserTokenLimit, int(user_id))
    if policy is None:
        policy = UserTokenLimit(user_id=int(user_id))
        db.add(policy)
        db.flush()
    return policy


def usage_limit_snapshot(
    db: Session,
    user_id: int,
    *,
    now: datetime.datetime | None = None,
    policy: UserTokenLimit | None = None,
) -> dict:
    """计算用户当前自然周、自然月和历史总量及剩余额度。"""
    policy = policy or db.get(UserTokenLimit, int(user_id))
    starts = usage_period_starts(policy, now=now)
    result: dict[str, dict] = {}
    for period in PERIOD_FIELDS:
        query = db.query(func.coalesce(func.sum(TokenUsage.total_tokens), 0)).filter(
            TokenUsage.user_id == int(user_id)
        )
        if starts[period] is not None:
            query = query.filter(TokenUsage.created_at >= starts[period])
        used = int(query.scalar() or 0)
        limit = max(0, int(getattr(policy, f"{period}_limit", 0) or 0))
        result[period] = {
            "used": used,
            "limit": limit,
            "remaining": None if limit == 0 else max(0, limit - used),
            "exceeded": bool(limit and used >= limit),
            "starts_at": (
                starts[period].isoformat() if starts[period] is not None else ""
            ),
        }
    return result


def token_limit_violation(db: Session, user_id: int) -> str:
    """达到限额时返回用户可见原因；空字符串表示允许创建新任务。"""
    policy = db.get(UserTokenLimit, int(user_id))
    if policy is None:
        return ""
    snapshot = usage_limit_snapshot(db, user_id, policy=policy)
    labels = {"weekly": "每周", "monthly": "每月", "total": "总量"}
    for period in PERIOD_FIELDS:
        item = snapshot[period]
        if item["exceeded"]:
            return (
                f"{labels[period]} Token 用量已达上限 "
                f"{_format_wan_tokens(item['used'])}/{_format_wan_tokens(item['limit'])}，"
                "请联系系统管理员调整或重置额度"
            )
    return ""


def _format_wan_tokens(value: int) -> str:
    """按管理员额度设置口径显示精确的万 Token，最多保留四位小数。"""
    whole, remainder = divmod(max(0, int(value or 0)), 10_000)
    if not remainder:
        return f"{whole:,} 万"
    fraction = f"{remainder:04d}".rstrip("0")
    return f"{whole:,}.{fraction} 万"


@dataclass(frozen=True)
class UsageContext:
    user_id: int
    run_id: str = ""
    agent_id: int | None = None


_usage_context: contextvars.ContextVar[UsageContext | None] = contextvars.ContextVar(
    "token_usage_context", default=None
)


class TokenQuotaExceeded(RuntimeError):
    """当前任务在后续模型调用前发现用户额度已经耗尽。"""


def ensure_usage_allowed() -> None:
    """每次上游模型调用前重检额度，限制多轮 Agent Loop 的继续透支。"""
    context = _usage_context.get()
    if context is None:
        return
    db = SessionLocal()
    try:
        violation = token_limit_violation(db, context.user_id)
    finally:
        db.close()
    if violation:
        raise TokenQuotaExceeded(violation)


@contextlib.contextmanager
def bind_usage_context(
    user_id: int, *, run_id: str = "", agent_id: int | None = None
) -> Iterator[None]:
    """把本次任务的归属绑定到当前异步上下文。"""
    token = _usage_context.set(
        UsageContext(user_id=int(user_id), run_id=(run_id or "")[:32], agent_id=agent_id)
    )
    try:
        yield
    finally:
        _usage_context.reset(token)


def extract_usage(data: dict) -> dict | None:
    """兼容 Chat Completions、Responses、Anthropic 及常见 SSE 事件的 usage。"""
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    # Responses SSE 的终态事件把最终响应包在 response 中。
    if not isinstance(usage, dict) and isinstance(data.get("response"), dict):
        usage = data["response"].get("usage")
    # 少数 Anthropic 事件把当前消息包在 message 中。
    if not isinstance(usage, dict) and isinstance(data.get("message"), dict):
        usage = data["message"].get("usage")
    if not isinstance(usage, dict):
        return None

    def as_int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = as_int(usage.get("input_tokens") or usage.get("prompt_tokens"))
    output_tokens = as_int(usage.get("output_tokens") or usage.get("completion_tokens"))
    total_tokens = as_int(usage.get("total_tokens")) or (input_tokens + output_tokens)
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    cached_tokens = as_int(
        usage.get("cache_read_input_tokens")
        or (input_details.get("cached_tokens") if isinstance(input_details, dict) else 0)
        or 0
    )
    reasoning_tokens = as_int(
        (output_details.get("reasoning_tokens") if isinstance(output_details, dict) else 0) or 0
    )
    if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0:
        return None
    return {
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "cached_tokens": max(0, cached_tokens),
        "reasoning_tokens": max(0, reasoning_tokens),
        "total_tokens": max(0, total_tokens),
    }


def record_response_usage(
    data: dict, *, provider_id: int | None = None, model: str = ""
) -> bool:
    """把一个完整响应或终态 SSE 事件记入数据库；返回是否成功识别并写入。"""
    context = _usage_context.get()
    values = extract_usage(data)
    if context is None or values is None:
        return False
    db = SessionLocal()
    try:
        user = db.get(User, context.user_id)
        if user is None:
            logger.warning("Token 用量写入跳过：归属用户已不存在（user_id=%s）", context.user_id)
            return False
        db.add(
            TokenUsage(
                user_id=context.user_id,
                username=user.username,
                run_id=context.run_id,
                agent_id=context.agent_id,
                provider_id=provider_id,
                model=(model or "")[:128],
                **values,
            )
        )
        db.commit()
        return True
    except Exception:  # noqa: BLE001 - 统计失败不能影响用户的模型响应
        db.rollback()
        logger.warning("Token 用量写入失败（不影响本次模型调用）", exc_info=True)
        return False
    finally:
        db.close()
