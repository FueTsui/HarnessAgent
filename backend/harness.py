"""Harness 版本注册表。

智能体只保存身份与能力绑定；指令和运行策略保存在不可变版本中。任何更新都会
创建新版本，发布仅切换 Agent.active_version，从而支持审计与即时回滚。
"""
import json

from sqlalchemy.orm import Session

from .models import Agent, HarnessVersion, iso_utc
from .runtime.loop import AGENT_LOOP_VERSION

DEFAULT_TOOL_POLICY = {
    "mode": "allow_bound",
    "profile": "small_model",
    "max_iterations": 8,
    "max_parallel_calls": 1,
    "max_successful_calls": 4,
    "timeout_seconds": 45,
    "max_output_chars": 4000,
    "context_budget_chars": 24000,
    "compact_chars": 1200,
    "argument_repair": True,
    "router": {"enabled": True, "activation_threshold": 10, "max_candidates": 6},
}
DEFAULT_MEMORY_POLICY = {
    "enabled": True,
    "strategy": "relevant_then_recent",
    "scope": "agent",
    "top_k": 3,
    "min_relevance": .12,
    "influence": .35,
    "relevance_weight": .85,
    "recency_weight": .15,
    "max_chars": 1800,
    "exclude_current_session": True,
    "recent_messages": 6,
    "budget_ratio": .7,
    "summary_chars": 5000,
}
DEFAULT_VERIFICATION_POLICY = {
    "required": True,
    "mode": "deterministic_then_revise",
    "strict": False,
    "max_revisions": 1,
}
DEFAULT_OUTPUT_POLICY = {
    "language": "follow_user",
    "concise": True,
    "max_chars": 30000,
}
DEFAULT_LOOP = {"version": AGENT_LOOP_VERSION}


def _loads(value: str, default):
    try:
        parsed = json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def active_version(db: Session, agent: Agent) -> HarnessVersion | None:
    return (
        db.query(HarnessVersion)
        .filter(
            HarnessVersion.agent_id == agent.id,
            HarnessVersion.version == agent.active_version,
        )
        .first()
    )


def create_version(
    db: Session,
    agent: Agent,
    *,
    system_prompt: str,
    tool_policy: dict | None = None,
    memory_policy: dict | None = None,
    verification_policy: dict | None = None,
    output_policy: dict | None = None,
    change_summary: str = "",
    created_by: int | None = None,
    publish: bool = False,
) -> HarnessVersion:
    latest = (
        db.query(HarnessVersion)
        .filter(HarnessVersion.agent_id == agent.id)
        .order_by(HarnessVersion.version.desc())
        .first()
    )
    version = (latest.version if latest else 0) + 1
    row = HarnessVersion(
        agent_id=agent.id,
        version=version,
        system_prompt=system_prompt or "",
        tool_policy=json.dumps(tool_policy or DEFAULT_TOOL_POLICY, ensure_ascii=False),
        memory_policy=json.dumps(memory_policy or DEFAULT_MEMORY_POLICY, ensure_ascii=False),
        verification_policy=json.dumps(
            verification_policy or DEFAULT_VERIFICATION_POLICY, ensure_ascii=False
        ),
        output_policy=json.dumps(output_policy or DEFAULT_OUTPUT_POLICY, ensure_ascii=False),
        change_summary=change_summary or "",
        status="published" if publish else "draft",
        created_by=created_by,
    )
    if publish:
        for current in db.query(HarnessVersion).filter(
            HarnessVersion.agent_id == agent.id,
            HarnessVersion.status == "published",
        ).all():
            current.status = "archived"
        agent.active_version = version
    db.add(row)
    db.flush()
    return row


def publish_version(db: Session, agent: Agent, version: int) -> HarnessVersion:
    row = (
        db.query(HarnessVersion)
        .filter(
            HarnessVersion.agent_id == agent.id,
            HarnessVersion.version == version,
        )
        .first()
    )
    if row is None:
        raise KeyError(version)
    for current in db.query(HarnessVersion).filter(
        HarnessVersion.agent_id == agent.id,
        HarnessVersion.status == "published",
    ).all():
        current.status = "archived"
    row.status = "published"
    agent.active_version = version
    db.flush()
    return row


def as_dict(row: HarnessVersion) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "version": row.version,
        "system_prompt": row.system_prompt,
        "tool_policy": _loads(row.tool_policy, {}) or DEFAULT_TOOL_POLICY,
        "memory_policy": _loads(row.memory_policy, {}) or DEFAULT_MEMORY_POLICY,
        "verification_policy": (
            _loads(row.verification_policy, {}) or DEFAULT_VERIFICATION_POLICY
        ),
        "output_policy": _loads(row.output_policy, {}) or DEFAULT_OUTPUT_POLICY,
        "loop": dict(DEFAULT_LOOP),
        "change_summary": row.change_summary,
        "status": row.status,
        "created_at": iso_utc(row.created_at),
    }
