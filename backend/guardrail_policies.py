"""Deterministic content policies; limited detectors are explicitly advertised.

Safe regex is intentionally a fixed-width subset without branches or variable
repetition. Content is never copied into detection events. Model output is
buffered whenever a model-output policy applies, before any delta is released.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from .database import SessionLocal
from .guardrail_models import GuardrailBlocklist, GuardrailPolicy
from .models import User

POINTS = ("user_input", "tool_input", "tool_output", "model_output")
DETECTORS = [
    {"id": "blocklist", "name": "阻止列表", "available": True, "description": "确定性匹配完整术语或受限安全正则；不理解上下文。"},
    {"id": "pii", "name": "个人信息", "available": True, "description": "识别常见邮箱、中国大陆手机号和18位身份证格式；不保证识别所有个人信息，不验证身份真伪。"},
    {"id": "prompt_injection", "name": "明显提示词注入", "available": True, "description": "仅检测常见中英文忽略指令、泄露系统提示的固定模式；不覆盖语义改写、编码或间接注入。"},
] + [
    {"id": key, "name": name, "available": False, "description": "未配置语义分类检测器，不能启用；关键词检测不能代替此项。"}
    for key, name in (("hate", "仇恨"), ("sexual", "性内容"), ("self_harm", "自残"), ("violence", "暴力"), ("copyright", "版权"), ("groundedness", "事实依据"))
]
MAX_SCAN_CHARS = 1_000_000


def safe_regex(value: str) -> re.Pattern:
    """Allow literals, classes, anchors, dot and exact {n}; no backtracking trees."""
    if not value or len(value) > 128:
        raise ValueError("安全正则长度须为1–128字符")
    index, width = 0, 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            if index >= len(value) or value[index].isdigit() or value[index] in "AZbBGNkuxU":
                raise ValueError("不支持反向引用、Unicode转义或位置扩展")
        elif char == "[":
            index += 1
            if index < len(value) and value[index] == "^":
                index += 1
            while index < len(value) and value[index] != "]":
                if value[index] == "\\":
                    index += 1
                index += 1
            if index >= len(value):
                raise ValueError("字符类缺少结束括号")
        elif char in "()*+?|}":
            raise ValueError("安全正则禁止分组、分支和可变重复；请使用字符类与固定{n}")
        elif char == "{":
            end = value.find("}", index)
            count = value[index + 1:end] if end >= 0 else ""
            if not count.isdigit() or not 1 <= int(count) <= 64 or index == 0:
                raise ValueError("仅支持1–64的固定次数{n}")
            width += int(count)
            index = end
        width += 1
        index += 1
    if width > 256:
        raise ValueError("安全正则匹配宽度过长")
    try:
        return re.compile(value)
    except re.error as exc:
        raise ValueError("安全正则语法不正确") from exc


class BlockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=256)
    mode: Literal["exact", "regex"] = "exact"
    case_sensitive: StrictBool = False

    @model_validator(mode="after")
    def validate_entry(self):
        self.value = self.value.strip()
        if not self.value:
            raise ValueError("术语不能为空")
        if self.mode == "regex":
            safe_regex(self.value)
        return self


class BlocklistBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    entries: list[BlockEntry] = Field(min_length=1, max_length=1000)
    revision: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()


class ContentRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detector: Literal["blocklist", "pii", "prompt_injection"]
    points: list[Literal["user_input", "tool_input", "tool_output", "model_output"]] = Field(min_length=1, max_length=4)
    action: Literal["block", "warn"] = "block"
    blocklist_ids: list[int] = Field(default_factory=list, max_length=20)
    pii_types: list[Literal["email", "phone", "china_id"]] = Field(default_factory=lambda: ["email", "phone", "china_id"])
    enabled: StrictBool = True

    @model_validator(mode="after")
    def valid_rule(self):
        if self.detector == "blocklist" and not self.blocklist_ids:
            raise ValueError("阻止列表规则至少绑定一份列表")
        if self.detector == "pii" and not self.pii_types:
            raise ValueError("个人信息规则至少选择一种类型")
        self.points = list(dict.fromkeys(self.points))
        return self


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    enabled: StrictBool = True
    rules: list[ContentRule] = Field(min_length=1, max_length=32)
    agent_ids: list[int] = Field(default_factory=list, max_length=100)
    provider_ids: list[int] = Field(default_factory=list, max_length=100)
    all_targets: StrictBool = False
    revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_policy(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("名称不能为空")
        if not self.all_targets and not self.agent_ids and not self.provider_ids:
            raise ValueError("请选择至少一个智能体或模型，或选择全部可作用任务")
        if any(value < 1 for value in self.agent_ids + self.provider_ids):
            raise ValueError("绑定ID必须为正整数")
        return self


class ContentBlocked(RuntimeError):
    code = "guardrail_content_blocked"

    def __init__(self, point: str):
        super().__init__(f"内容护栏已在 {point} 阶段阻止此次调用；可在护栏规则中检查命中项")


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    # Values are inspected separately from JSON field names and escapes.
    if isinstance(value, dict):
        return "\n".join(text_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(text_value(item) for item in value)
    return str(value) if value is not None else ""


PII_PATTERNS = {
    "email": re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"),
    "phone": re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)"),
    "china_id": re.compile(r"(?<!\w)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\w)"),
}
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+(?:instructions|prompts|rules)", re.I),
    re.compile(r"(?:reveal|print|show|disclose)\s+(?:your\s+|the\s+)?system\s+prompt", re.I),
    re.compile(r"(?:忽略|无视|绕过)(?:所有|全部)?(?:之前|此前|系统|上面|以上)(?:的)?(?:指令|提示|规则)"),
    re.compile(r"(?:输出|泄露|显示|打印)(?:你的|完整的|所有)?系统提示词"),
]


def entry_pattern(entry: dict) -> re.Pattern:
    value = entry["value"]
    if entry["mode"] == "regex":
        safe_regex(value)
        pattern = value
    else:
        # Chinese has no whitespace word separator. Latin/digit terms use word
        # boundaries so "cat" does not block "education" or "concatenate".
        left = r"(?<![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", value) else ""
        right = r"(?![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]$", value) else ""
        pattern = left + re.escape(value) + right
    return re.compile(pattern, 0 if entry.get("case_sensitive") else re.I)


def evaluate_content(policies: list[dict], blocklists: dict[int, list[dict]], point: str, text: str) -> dict:
    matches = []
    for policy in policies:
        if not policy.get("enabled", True):
            continue
        for index, rule in enumerate(policy.get("rules", [])):
            if not rule.get("enabled", True) or point not in rule["points"]:
                continue
            if len(text) > MAX_SCAN_CHARS:
                count = 1
                detector = "scan_limit"
            else:
                detector = rule["detector"]
                if detector == "blocklist":
                    patterns = [entry_pattern(entry) for bid in rule["blocklist_ids"] for entry in blocklists[bid]]
                elif detector == "pii":
                    patterns = [PII_PATTERNS[key] for key in rule["pii_types"]]
                else:
                    patterns = INJECTION_PATTERNS
                count = sum(1 for pattern in patterns if pattern.search(text))
            if count:
                matches.append({
                    "policy_id": policy.get("id"), "policy_name": policy["name"],
                    "detector": detector, "action": "block" if detector == "scan_limit" else rule["action"],
                    "rule_index": index, "count": count,
                })
    decision = "block" if any(match["action"] == "block" for match in matches) else "warn" if matches else "allow"
    return {"decision": decision, "matches": matches, "point": point}


def active_content_policies(*, user_id=None, agent_id=None, provider_id=None) -> tuple[list[dict], dict]:
    # Internal calls without an authenticated actor have no applicable personal
    # policy. Real task execution always supplies the authenticated user id.
    if user_id is None:
        return [], {}
    with SessionLocal() as db:
        rows = db.query(GuardrailPolicy, User.role).join(User, User.id == GuardrailPolicy.created_by).filter(GuardrailPolicy.enabled.is_(True)).all()
        policies, ids = [], set()
        for row, creator_role in rows:
            if creator_role != "root" and row.created_by != user_id:
                continue
            config = json.loads(row.config)
            if not (config.get("all_targets") or agent_id in config.get("agent_ids", []) or provider_id in config.get("provider_ids", [])):
                continue
            policy = {**config, "id": row.id, "name": row.name, "enabled": row.enabled}
            policies.append(policy)
            for rule in config["rules"]:
                ids.update(rule.get("blocklist_ids", []))
        blocklists = {row.id: json.loads(row.entries) for row in db.query(GuardrailBlocklist).filter(GuardrailBlocklist.id.in_(ids)).all()} if ids else {}
        if ids - set(blocklists):
            raise RuntimeError("Referenced guardrail blocklist missing")
        return policies, blocklists


async def _enforce_snapshot(policies, lists, point, content, *, user_id=None, agent_id=None, provider_id=None, runtime_event=None, **_unused):
    try:
        result = await asyncio.to_thread(evaluate_content, policies, lists, point, text_value(content))
    except Exception as exc:
        raise ContentBlocked(point) from exc
    if result["matches"] and runtime_event:
        value = runtime_event("guardrail.content_evaluated", {**result, "agent_id": agent_id, "provider_id": provider_id})
        if inspect.isawaitable(value):
            await value
    if result["decision"] == "block":
        from .guardrail_reviews import request_review
        if not any(m["detector"] == "scan_limit" for m in result["matches"]) and await request_review(user_id, agent_id, {**result, "provider_id": provider_id}, runtime_event):
            return {**result, "decision": "approved"}
        raise ContentBlocked(point)
    return result


async def enforce_content(point: str, content: Any, *, user_id=None, agent_id=None, provider_id=None, runtime_event=None) -> dict:
    try:
        policies, lists = await asyncio.to_thread(active_content_policies, user_id=user_id, agent_id=agent_id, provider_id=provider_id)
        return await _enforce_snapshot(policies, lists, point, content, user_id=user_id, agent_id=agent_id, provider_id=provider_id, runtime_event=runtime_event)
    except ContentBlocked:
        raise
    except Exception as exc:
        raise ContentBlocked(point) from exc


class GuardedModelClient:
    def __init__(self, client, *, user_id, agent_id, provider_id=None, runtime_event=None):
        self._guardrail_client = client
        self._identity = dict(user_id=user_id, agent_id=agent_id, provider_id=provider_id, runtime_event=runtime_event)

    def __getattr__(self, name):
        method = getattr(self._guardrail_client, name)
        if name not in {"chat", "chat_with_tools", "chat_messages_stream", "vision"}:
            return method

        async def invoke(*args, **kwargs):
            if name in {"chat", "vision"}:
                user_input = args[1] if len(args) > 1 else kwargs.get("user", "")
            else:
                messages = args[0] if args else kwargs.get("messages", [])
                user_input = [item.get("content", "") for item in messages if isinstance(item, dict) and item.get("role") == "user"] if isinstance(messages, list) else messages
            identity = {key: value for key, value in self._identity.items() if key != "runtime_event"}
            try:
                policies, lists = await asyncio.to_thread(active_content_policies, **identity)
            except Exception as exc:
                raise ContentBlocked("user_input") from exc
            await _enforce_snapshot(policies, lists, "user_input", user_input, **self._identity)
            original = kwargs.get("on_delta")
            checks_output = any(rule.get("enabled", True) and "model_output" in rule["points"] for policy in policies for rule in policy["rules"])
            if name == "chat_messages_stream" and original and checks_output:
                # Named policies are captured for each provider invocation.
                # Output checks hold deltas until the whole response passes.
                kwargs["on_delta"] = lambda _delta: None
            result = await method(*args, **kwargs)
            parent = self.__dict__.get("_guardrail_parent")
            if parent is not None:
                parent.provider_id = self._identity["provider_id"]
            output = result.get("content", "") if isinstance(result, dict) else result
            await _enforce_snapshot(policies, lists, "model_output", output, **self._identity)
            if name == "chat_messages_stream" and original and checks_output and output:
                value = original(output)
                if inspect.isawaitable(value):
                    await value
            return result
        return invoke


def guard_model_client(llm, *, user_id=None, agent_id=None, runtime_event=None):
    if user_id is None:
        return llm
    if isinstance(llm, GuardedModelClient):
        if llm._identity["user_id"] == user_id and llm._identity["agent_id"] == agent_id:
            return llm
        return GuardedModelClient(
            llm._guardrail_client, user_id=user_id, agent_id=agent_id,
            provider_id=llm._identity["provider_id"], runtime_event=runtime_event,
        )
    if hasattr(llm, "_clients"):
        # Wrap actual fallback clients with their own identities, retaining the
        # existing routing and accounting wrapper.
        # copy.copy probes __setstate__ before the proxy has _clients, invoking
        # its forwarding __getattr__ recursively. Copy its instance state only.
        isolated = object.__new__(type(llm))
        isolated.__dict__.update(llm.__dict__)
        llm = isolated
        llm._clients = [(pid, model, guard_model_client(client, user_id=user_id, agent_id=agent_id, runtime_event=runtime_event)) for pid, model, client in llm._clients]
        for pid, _model, client in llm._clients:
            client._identity["provider_id"] = pid
            client._guardrail_parent = llm
        return llm
    return GuardedModelClient(llm, user_id=user_id, agent_id=agent_id, provider_id=getattr(llm, "provider_id", None), runtime_event=runtime_event)
