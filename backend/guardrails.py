"""Root-managed, additive tool guardrails shared by preview and runtime.

These checks never grant a tool binding, relax the workspace/network sandbox, or
replace a task's approval policy. Settings are read before each actual dispatch,
so a running task observes changes on its next tool call.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import approval_policy
from .database import SessionLocal
from .models import AppSetting

logger = logging.getLogger(__name__)
SETTING_KEY = "guardrails.v1"


class GuardrailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = True
    block_mutating_tools: StrictBool = False
    require_high_risk_approval: StrictBool = False
    require_external_approval: StrictBool = False
    blocked_tools: list[str] = Field(default_factory=list, max_length=100)
    max_argument_chars: int = Field(default=0, ge=0, le=1_000_000, strict=True)

    @field_validator("blocked_tools")
    @classmethod
    def normalize_tools(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            name = value.strip()
            if not name or len(name) > 256 or any(char.isspace() for char in name):
                raise ValueError("工具名称须为 1–256 字符且不含空白")
            if name not in result:
                result.append(name)
        return result


RULES = (
    ("block_mutating_tools", "拦截所有写操作", "阻止内置工具与 MCP 的写入、执行及外部变更。"),
    ("require_high_risk_approval", "高风险操作强制审批", "高风险内置工具和具有破坏性的 MCP 调用始终要求单次审批。"),
    ("require_external_approval", "外部写操作强制审批", "所有 MCP 写操作都需要单次审批，包括任务使用完全访问策略时。"),
    ("blocked_tools", "工具禁用清单", "按工具名或 mcp:服务ID:远端工具名精确拦截。"),
    ("max_argument_chars", "调用参数长度上限", "按序列化 JSON 字符数拦截超长参数；0 表示不追加限制。"),
)
RULE_NAMES = {key: name for key, name, _description in RULES}
BASELINE = [
    {"id": "authorization", "name": "角色与资源权限", "description": "仍校验用户角色、资源归属和智能体工具绑定。", "status": "enforced"},
    {"id": "workspace", "name": "工作区隔离", "description": "文件、命令与公共智能体执行边界持续生效。", "status": "enforced"},
    {"id": "network", "name": "网络访问边界", "description": "出站请求仍受现有网络地址校验与部署设置约束。", "status": "enforced"},
    {"id": "approval", "name": "任务审批策略", "description": "单次批准仍绑定任务、用户、智能体和工具范围；禁用护栏不改变原审批策略。", "status": "enforced"},
]


class RevisionConflict(ValueError):
    pass


def read_config(db: Session) -> dict:
    row = db.get(AppSetting, SETTING_KEY)
    if row is None:
        return {"config": GuardrailConfig(), "revision": 0, "updated_at": ""}
    payload = json.loads(row.value)
    return {
        "config": GuardrailConfig.model_validate(payload["config"]),
        "revision": int(payload["revision"]),
        "updated_at": str(payload.get("updated_at") or ""),
    }


def save_config(db: Session, config: GuardrailConfig, revision: int) -> dict:
    """Compare-and-swap avoids silently overwriting another admin session."""
    current = read_config(db)
    if current["revision"] != revision:
        raise RevisionConflict("护栏配置已更新，请刷新后重试")
    row = db.get(AppSetting, SETTING_KEY)
    payload = {
        "config": config.model_dump(),
        "revision": revision + 1,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        if row is None:
            db.add(AppSetting(key=SETTING_KEY, value=encoded))
        else:
            previous = row.value
            result = db.execute(
                update(AppSetting)
                .where(AppSetting.key == SETTING_KEY, AppSetting.value == previous)
                .values(value=encoded)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                db.rollback()
                raise RevisionConflict("护栏配置已更新，请刷新后重试")
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise RevisionConflict("护栏配置已更新，请刷新后重试") from exc
    db.expire_all()
    return read_config(db)


def evaluate_tool(
    config: GuardrailConfig,
    *,
    kind: Literal["builtin", "mcp"],
    tool_name: str,
    arguments: dict,
    policy: str = approval_policy.DEFAULT,
    mutating: bool = False,
    destructive: bool = False,
    server_id: int | None = None,
) -> dict:
    selected = approval_policy.normalize(policy)
    # Match MCP's runtime classifier even when a preview supplies contradictory
    # flags: a destructive operation cannot be considered read-only.
    if kind == "mcp" and destructive:
        mutating = True
    baseline_approval = (
        approval_policy.requires_builtin_approval(selected, tool_name=tool_name, mutating=mutating, arguments=arguments)
        if kind == "builtin" else
        approval_policy.requires_external_approval(selected, mutating=mutating, destructive=destructive)
    )
    blocked: list[str] = []
    approval: list[str] = []
    if config.enabled:
        names = {tool_name}
        if kind == "mcp" and server_id is not None:
            names.add(f"mcp:{server_id}:{tool_name}")
        if names.intersection(config.blocked_tools):
            blocked.append("blocked_tools")
        if config.block_mutating_tools and mutating:
            blocked.append("block_mutating_tools")
        size = len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")))
        if config.max_argument_chars and size > config.max_argument_chars:
            blocked.append("max_argument_chars")
        high_risk = (
            approval_policy.builtin_risk(tool_name, arguments) == "high"
            if kind == "builtin" else destructive
        )
        if config.require_high_risk_approval and mutating and high_risk:
            approval.append("require_high_risk_approval")
        if config.require_external_approval and kind == "mcp" and mutating:
            approval.append("require_external_approval")
    needs_approval = bool(baseline_approval or approval) and not blocked
    decision = "block" if blocked else "require_approval" if needs_approval else "allow"
    matched = [{"id": key, "name": RULE_NAMES[key]} for key in blocked + approval]
    if baseline_approval:
        matched.append({"id": "approval_policy", "name": "任务审批策略"})
    reason = (
        "护栏已拦截：" + "、".join(RULE_NAMES[key] for key in blocked)
        if blocked else "此调用需要单次审批" if needs_approval else
        "本层未拦截；实际执行仍需通过角色、工具绑定、资源与沙盒校验"
    )
    return {
        "decision": decision,
        "allowed": not blocked,
        "requires_approval": needs_approval,
        "guardrail_requires_approval": bool(approval) and not blocked,
        "matched_rules": matched,
        "reason": reason,
        "kind": kind,
        "tool_name": tool_name,
        "approval_policy": selected,
        "guardrails_enabled": config.enabled,
    }


def runtime_decision(**kwargs) -> dict:
    """Read committed settings in a short session; unavailable policy fails closed."""
    try:
        with SessionLocal() as db:
            state = read_config(db)
        return evaluate_tool(state["config"], **kwargs)
    except Exception:  # Do not expose settings, SQL, or raw arguments to the model.
        logger.warning("Guardrail configuration could not be evaluated; tool dispatch blocked")
        return {
            "decision": "block", "allowed": False, "requires_approval": False,
            "guardrail_requires_approval": False,
            "matched_rules": [{"id": "policy_unavailable", "name": "护栏配置不可用"}],
            "reason": "护栏配置暂不可用，工具调用已阻止；请管理员检查配置服务",
            "kind": kwargs.get("kind"), "tool_name": kwargs.get("tool_name"),
            "approval_policy": kwargs.get("policy"), "guardrails_enabled": True,
        }


def event_payload(decision: dict, *, agent_id=None, execution_id=None) -> dict:
    """Audit the decision without copying tool arguments or credentials."""
    return {
        "tool": decision["tool_name"], "kind": decision["kind"],
        "decision": decision["decision"], "matched_rules": decision["matched_rules"],
        "agent_id": agent_id, "execution_id": execution_id,
    }
