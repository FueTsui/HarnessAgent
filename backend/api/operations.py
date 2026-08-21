"""Root-only operations center APIs.

The operations center deliberately exposes an allowlisted, reasoning-free view of
runtime state.  Recovery never rewrites a failed Job/Turn into success: a retry is
always a new Turn/Job with fresh approval state and an explicit lineage pointer.
All mutating endpoints live below ``/api/v1`` so the global audit middleware records
the acting root, path and outcome.
"""
from __future__ import annotations

import copy
import datetime
import json
import re
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import jobs
from ..config import settings
from ..database import get_db
from ..logging_utils import redact_log_value
from ..models import (
    Agent,
    Channel,
    Item,
    Job,
    McpServer,
    ModelProvider,
    SchedulerHeartbeat,
    TokenUsage,
    Turn,
    User,
    WorkerHeartbeat,
    iso_utc,
)
from ..runtime import task_store
from ..security import require_root
from ..weixin_channel import CHANNEL_TYPE as WEIXIN_CHANNEL_TYPE
from ..weixin_channel import manager as weixin_manager


router = APIRouter(
    prefix="/api/v1/admin/operations",
    tags=["运行中心（root）"],
)

_FAILURE_STATUSES = {jobs.FAILED, jobs.DEAD_LETTER}
_RESOURCE_MODELS = {
    "provider": (ModelProvider, "模型提供商"),
    "mcp": (McpServer, "MCP 服务"),
    "channel": (Channel, "消息渠道"),
}
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|"
    r"token|secret|password)\b\s*([:=])\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SAFE_EVENT_FIELDS = {
    "status",
    "reason",
    "error",
    "error_type",
    "error_code",
    "tool",
    "ok",
    "timeout",
    "attempt_count",
    "max_attempts",
    "retry_at",
    "source",
    "completion_status",
    "completion_issues",
    "scope",
    "policy",
}


class ResourceStateRequest(BaseModel):
    enabled: bool


class JobPriorityRequest(BaseModel):
    priority: int


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _public_error(value: object, limit: int = 1200) -> str:
    """Return a bounded error message without common credential forms."""
    text = redact_log_value(str(value or ""))
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
    return text[:limit]


def _json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _status_counts(rows) -> dict[str, int]:
    return {str(key): int(count or 0) for key, count in rows}


def _job_identity_maps(db: Session, rows: list[Job]) -> tuple[dict, dict, dict]:
    owner_ids = {row.owner_id for row in rows if row.owner_id is not None}
    agent_ids = {row.agent_id for row in rows if row.agent_id is not None}
    users = {
        row.id: row.username
        for row in db.query(User).filter(User.id.in_(owner_ids)).all()
    } if owner_ids else {}
    agents = {
        row.id: row.name
        for row in db.query(Agent).filter(Agent.id.in_(agent_ids)).all()
    } if agent_ids else {}
    turn_ids = [row.id for row in rows]
    turns = {
        row.id: row
        for row in db.query(Turn).filter(Turn.id.in_(turn_ids)).all()
    } if turn_ids else {}
    return users, agents, turns


def _job_out(
    row: Job,
    *,
    users: dict | None = None,
    agents: dict | None = None,
    turns: dict | None = None,
) -> dict:
    users = users or {}
    agents = agents or {}
    turns = turns or {}
    turn = turns.get(row.id)
    payload = _json_dict(row.payload)
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    retry_of = str(payload.get("operations_retry_of") or "")[:32]
    return {
        "id": row.id,
        "parent_job_id": row.parent_job_id or "",
        "retry_of_job_id": retry_of,
        "owner_id": row.owner_id,
        "username": users.get(row.owner_id, ""),
        "agent_id": row.agent_id,
        "agent_name": agents.get(row.agent_id, ""),
        "kind": row.kind,
        "source": str(payload.get("source") or "web")[:24],
        "status": row.status,
        "progress": str(row.progress or "")[:256],
        "error": _public_error(row.error),
        "error_class": str(row.error_class or "unclassified")[:32],
        "priority": int(row.priority or 0),
        "attempt_count": int(row.attempt_count or 0),
        "max_attempts": int(row.max_attempts or 0),
        "worker_id": str(row.worker_id or "")[:128],
        "cancel_requested": bool(row.cancel_requested),
        "next_attempt_at": iso_utc(row.next_attempt_at),
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
        "turn": {
            "thread_id": turn.thread_id if turn is not None else "",
            "status": turn.status if turn is not None else "",
            "input": str(
                (turn.input if turn is not None else "")
                or payload.get("query")
                or inputs.get("query")
                or ""
            )[:500],
            "created_at": iso_utc(turn.created_at) if turn is not None else "",
        },
    }


def _heartbeat_active(value: datetime.datetime | None, cutoff: datetime.datetime) -> bool:
    aware = _aware(value)
    return aware is not None and aware >= cutoff


def _percentile(values: list[float], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(max(0.0, value) for value in values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index])


@router.get("/summary")
def operations_summary(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    """Return a bounded operational snapshot without credentials or reasoning."""
    now = _now()
    cutoff = now - datetime.timedelta(hours=hours)

    queue_counts = _status_counts(
        db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    )
    oldest_pending = db.query(func.min(Job.created_at)).filter(
        Job.status == jobs.PENDING
    ).scalar()
    oldest_pending_aware = _aware(oldest_pending)
    recent_turns = db.query(Turn).filter(Turn.created_at >= cutoff).all()
    queue_waits = [
        (_aware(row.started_at) - _aware(row.created_at)).total_seconds()
        for row in recent_turns if row.started_at is not None and row.created_at is not None
    ]
    run_durations = [
        (_aware(row.completed_at) - _aware(row.started_at)).total_seconds()
        for row in recent_turns if row.completed_at is not None and row.started_at is not None
    ]

    worker_cutoff = now - datetime.timedelta(
        seconds=max(30, int(settings.JOB_HEARTBEAT_SECONDS * 3))
    )
    worker_rows = db.query(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_id).all()
    worker_items = [
        {
            "worker_id": row.worker_id,
            "capacity": int(row.capacity or 0),
            "active": _heartbeat_active(row.last_seen, worker_cutoff),
            "last_seen": iso_utc(row.last_seen),
        }
        for row in worker_rows
    ]
    active_workers = [row for row in worker_items if row["active"]]

    scheduler_cutoff = now - datetime.timedelta(
        seconds=max(30, int(settings.CRON_HEARTBEAT_STALE_SECONDS))
    )
    scheduler_rows = db.query(SchedulerHeartbeat).order_by(
        SchedulerHeartbeat.scheduler_id
    ).all()
    scheduler_items = [
        {
            "scheduler_id": row.scheduler_id,
            "active": _heartbeat_active(row.last_seen, scheduler_cutoff),
            "last_seen": iso_utc(row.last_seen),
            "last_successful_dispatch_at": iso_utc(row.last_successful_dispatch_at),
            "last_error": _public_error(row.last_error),
            "can_acknowledge_error": bool(row.last_error),
        }
        for row in scheduler_rows
    ]

    providers = db.query(ModelProvider).order_by(ModelProvider.name, ModelProvider.id).all()
    provider_usage = {
        int(provider_id): {
            "request_count": int(request_count or 0),
            "total_tokens": int(total_tokens or 0),
            "last_used_at": iso_utc(last_used_at),
        }
        for provider_id, request_count, total_tokens, last_used_at in db.query(
            TokenUsage.provider_id,
            func.count(TokenUsage.id),
            func.sum(TokenUsage.total_tokens),
            func.max(TokenUsage.created_at),
        ).filter(
            TokenUsage.created_at >= cutoff,
            TokenUsage.provider_id.is_not(None),
        ).group_by(TokenUsage.provider_id).all()
    }
    bound_agent_counts = Counter(
        row.provider_id
        for row in db.query(Agent.provider_id).filter(Agent.provider_id.is_not(None)).all()
    )
    provider_items = [
        {
            "id": row.id,
            "name": row.name,
            "provider_type": row.provider_type,
            "model_id": row.model_id,
            "enabled": bool(row.enabled),
            "is_public": bool(row.is_public),
            "bound_agents": int(bound_agent_counts.get(row.id, 0)),
            **provider_usage.get(row.id, {
                "request_count": 0, "total_tokens": 0, "last_used_at": "",
            }),
        }
        for row in providers
    ]

    agent_rows = db.query(Agent).order_by(Agent.name, Agent.id).all()
    agent_status_rows = db.query(
        Job.agent_id, Job.status, func.count(Job.id)
    ).filter(
        Job.created_at >= cutoff,
        Job.agent_id.is_not(None),
    ).group_by(Job.agent_id, Job.status).all()
    agent_status: dict[int, dict[str, int]] = defaultdict(dict)
    for agent_id, job_status, count in agent_status_rows:
        agent_status[int(agent_id)][str(job_status)] = int(count or 0)
    agent_items = []
    for row in agent_rows:
        counts = agent_status.get(row.id, {})
        terminal = sum(counts.get(key, 0) for key in (
            jobs.DONE, jobs.FAILED, jobs.CANCELLED, jobs.DEAD_LETTER,
        ))
        agent_items.append({
            "id": row.id,
            "name": row.name,
            "enabled": bool(row.enabled),
            "provider_id": row.provider_id,
            "jobs": sum(counts.values()),
            "status_counts": counts,
            "success_rate": (
                round(counts.get(jobs.DONE, 0) / terminal, 4) if terminal else None
            ),
        })

    tool_rows = db.query(Item.name, Item.status, func.count(Item.id)).filter(
        Item.created_at >= cutoff,
        Item.kind == "tool_result",
    ).group_by(Item.name, Item.status).all()
    tool_status: dict[str, dict[str, int]] = defaultdict(dict)
    for name, item_status, count in tool_rows:
        tool_status[str(name or "unknown")][str(item_status)] = int(count or 0)
    tool_items = [
        {
            "name": name,
            "calls": sum(counts.values()),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "failure_rate": round(
                counts.get("failed", 0) / max(1, sum(counts.values())), 4
            ),
        }
        for name, counts in sorted(
            tool_status.items(), key=lambda item: (-sum(item[1].values()), item[0])
        )[:50]
    ]

    channel_rows = db.query(Channel).order_by(Channel.name, Channel.id).all()
    channel_items = [
        {
            "id": row.id,
            "name": row.name,
            "type": row.type,
            "agent_id": row.agent_id,
            "enabled": bool(row.enabled),
            "connection_status": row.connection_status,
            "last_error": _public_error(row.last_error),
            "last_inbound_at": iso_utc(row.last_inbound_at),
            "last_outbound_at": iso_utc(row.last_outbound_at),
        }
        for row in channel_rows
    ]
    mcp_rows = db.query(McpServer).order_by(McpServer.name, McpServer.id).all()
    mcp_items = [
        {
            "id": row.id,
            "name": row.name,
            "enabled": bool(row.enabled),
            "risk_policy": row.risk_policy,
            "is_public": bool(row.is_public),
        }
        for row in mcp_rows
    ]

    error_classes = {
        (str(error_class or "unclassified")): int(count or 0)
        for error_class, count in db.query(
            Job.error_class, func.count(Job.id)
        ).filter(
            Job.updated_at >= cutoff,
            Job.status.in_(_FAILURE_STATUSES),
        ).group_by(Job.error_class).all()
    }
    failed_tools = [
        {"name": item["name"], "count": item["failed"]}
        for item in tool_items if item["failed"]
    ]
    alerts = []
    if not active_workers and settings.JOB_WORKER_ENABLED:
        alerts.append({"severity": "critical", "code": "worker_unavailable", "message": "没有活跃 Worker"})
    if queue_counts.get(jobs.DEAD_LETTER, 0):
        alerts.append({"severity": "critical", "code": "dead_letter", "message": f"存在 {queue_counts.get(jobs.DEAD_LETTER, 0)} 个死信任务"})
    if oldest_pending_aware is not None and (now - oldest_pending_aware).total_seconds() >= 300:
        alerts.append({"severity": "warning", "code": "queue_delay", "message": "最早待处理任务已等待超过 5 分钟"})
    if any(not row["active"] or row["last_error"] for row in scheduler_items):
        alerts.append({"severity": "warning", "code": "scheduler_degraded", "message": "Scheduler 心跳或最近派发存在异常"})
    if any(row["last_error"] for row in channel_items):
        alerts.append({"severity": "warning", "code": "channel_error", "message": "存在消息渠道错误"})

    return {
        "generated_at": now.isoformat(),
        "window": {"hours": hours, "from": cutoff.isoformat(), "to": now.isoformat()},
        "queue": {
            "status_counts": queue_counts,
            "total": sum(queue_counts.values()),
            "in_flight": sum(queue_counts.get(key, 0) for key in (
                jobs.PENDING, jobs.RUNNING, jobs.AWAITING_APPROVAL,
            )),
            "dead_letter": queue_counts.get(jobs.DEAD_LETTER, 0),
            "oldest_pending_seconds": (
                max(0, int((now - oldest_pending_aware).total_seconds()))
                if oldest_pending_aware is not None else 0
            ),
            "wait_seconds": {
                "samples": len(queue_waits),
                "p50": _percentile(queue_waits, .50),
                "p95": _percentile(queue_waits, .95),
            },
            "run_seconds": {
                "samples": len(run_durations),
                "p50": _percentile(run_durations, .50),
                "p95": _percentile(run_durations, .95),
            },
        },
        "workers": {
            "total": len(worker_items),
            "active": len(active_workers),
            "capacity": sum(row["capacity"] for row in active_workers),
            "items": worker_items,
        },
        "schedulers": {
            "total": len(scheduler_items),
            "active": sum(1 for row in scheduler_items if row["active"]),
            "items": scheduler_items,
        },
        "providers": {
            "total": len(provider_items),
            "enabled": sum(1 for row in provider_items if row["enabled"]),
            "items": provider_items,
        },
        "agents": {
            "total": len(agent_items),
            "enabled": sum(1 for row in agent_items if row["enabled"]),
            "items": agent_items,
        },
        "tools": {
            "calls": sum(row["calls"] for row in tool_items),
            "failed": sum(row["failed"] for row in tool_items),
            "items": tool_items,
        },
        "channels": {
            "total": len(channel_items),
            "enabled": sum(1 for row in channel_items if row["enabled"]),
            "items": channel_items,
        },
        "mcp_servers": {
            "total": len(mcp_items),
            "enabled": sum(1 for row in mcp_items if row["enabled"]),
            "items": mcp_items,
        },
        "errors": {
            "job_error_classes": error_classes,
            "failed_tools": failed_tools,
            "scheduler_errors": sum(1 for row in scheduler_items if row["last_error"]),
            "channel_errors": sum(1 for row in channel_items if row["last_error"]),
        },
        "alerts": alerts,
        "actions": {
            "retry_mode": "copy_only",
            "can_acknowledge_scheduler_error": True,
            "pausable_resources": sorted(_RESOURCE_MODELS),
        },
    }


@router.get("/failures")
def list_failed_jobs(
    job_status: str = Query(default="failed,dead_letter", alias="status"),
    error_class: str = Query(default="", max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    requested = {
        value.strip() for value in job_status.split(",") if value.strip()
    }
    if not requested or not requested.issubset(_FAILURE_STATUSES):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "status 只支持 failed 和 dead_letter",
        )
    query = db.query(Job).filter(Job.status.in_(requested))
    if error_class.strip():
        query = query.filter(Job.error_class == error_class.strip())
    total = query.count()
    rows = query.order_by(Job.updated_at.desc(), Job.id.desc()).offset(offset).limit(limit).all()
    users, agents, turns = _job_identity_maps(db, rows)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            _job_out(row, users=users, agents=agents, turns=turns) for row in rows
        ],
    }


def _safe_event_payload(raw: object) -> dict:
    payload = _json_dict(raw)
    result = {}
    for key in _SAFE_EVENT_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            value = _public_error(value, 500)
        elif isinstance(value, list):
            value = [_public_error(item, 300) for item in value[:20]]
        elif not isinstance(value, (bool, int, float, type(None))):
            continue
        result[key] = value
    return result


@router.get("/jobs/{job_id}")
def operation_job_detail(
    job_id: str,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    row = db.get(Job, job_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    users, agents, turns = _job_identity_maps(db, [row])
    events = db.query(Item).filter(
        Item.turn_id == job_id,
        Item.kind.in_(("lifecycle", "tool_result", "verification", "approval", "event")),
    ).order_by(Item.sequence.desc()).limit(80).all()
    return {
        "job": _job_out(row, users=users, agents=agents, turns=turns),
        "events": [
            {
                "id": item.id,
                "sequence": int(item.sequence or 0),
                "kind": item.kind,
                "name": item.name,
                "status": item.status,
                "payload": _safe_event_payload(item.payload),
                "created_at": iso_utc(item.created_at),
            }
            for item in reversed(events)
        ],
        "lineage": {
            "parent_job_id": row.parent_job_id or "",
            "child_job_ids": [
                value for (value,) in db.query(Job.id).filter(
                    Job.parent_job_id == row.id
                ).order_by(Job.created_at, Job.id).limit(200).all()
            ],
        },
    }


@router.get("/jobs/{job_id}/tree")
def operation_job_tree(
    job_id: str,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    focus = db.get(Job, job_id)
    if focus is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")

    root = focus
    seen_ancestors = {focus.id}
    for _depth in range(32):
        if not root.parent_job_id:
            break
        parent = db.get(Job, root.parent_job_id)
        if parent is None or parent.id in seen_ancestors:
            break
        seen_ancestors.add(parent.id)
        root = parent

    rows = [root]
    seen = {root.id}
    frontier = [root.id]
    truncated = False
    for _depth in range(32):
        if not frontier:
            break
        remaining = 500 - len(rows)
        if remaining <= 0:
            truncated = True
            break
        children = db.query(Job).filter(
            Job.parent_job_id.in_(frontier)
        ).order_by(Job.created_at, Job.id).limit(remaining + 1).all()
        if len(children) > remaining:
            children = children[:remaining]
            truncated = True
        frontier = []
        for child in children:
            if child.id in seen:
                continue
            seen.add(child.id)
            rows.append(child)
            frontier.append(child.id)
    if frontier:
        truncated = True

    users, agents, turns = _job_identity_maps(db, rows)
    nodes = {
        row.id: {
            **_job_out(row, users=users, agents=agents, turns=turns),
            "children": [],
        }
        for row in rows
    }
    for row in rows:
        if row.parent_job_id in nodes and row.id != root.id:
            nodes[row.parent_job_id]["children"].append(nodes[row.id])
    return {
        "focus_job_id": focus.id,
        "root_job_id": root.id,
        "node_count": len(nodes),
        "truncated": truncated,
        "tree": nodes[root.id],
    }


def _force_fresh_approval(payload: dict) -> None:
    payload["approval_policy"] = "ask"
    payload["approval_tokens"] = []
    for key in list(payload):
        if key.startswith("_approval_"):
            payload.pop(key, None)
    snapshot = payload.get("execution_snapshot")
    if isinstance(snapshot, dict):
        snapshot["approval_policy"] = "ask"


def _filter_paused_snapshot_mcp(db: Session, node: object) -> None:
    if not isinstance(node, dict):
        return
    servers = node.get("mcp_servers")
    if isinstance(servers, list):
        ids = {
            int(item.get("id"))
            for item in servers
            if isinstance(item, dict) and item.get("id") is not None
        }
        enabled_ids = {
            row.id for row in db.query(McpServer.id).filter(
                McpServer.id.in_(ids), McpServer.enabled.is_(True)
            ).all()
        } if ids else set()
        node["mcp_servers"] = [
            item for item in servers
            if not isinstance(item, dict)
            or item.get("id") is None
            or int(item.get("id")) in enabled_ids
        ]
    for child in node.get("sub_agents") or []:
        _filter_paused_snapshot_mcp(db, child)


@router.post("/jobs/{job_id}/retry-copy", status_code=status.HTTP_201_CREATED)
def retry_failed_job_as_copy(
    job_id: str,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    """Create a new task from a failure; never mutate the failed terminal fact."""
    source = db.get(Job, job_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if source.status not in _FAILURE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "只有失败或死信任务可以复制重试")
    if source.kind != "chat":
        raise HTTPException(status.HTTP_409_CONFLICT, "当前仅支持复制重试 chat 任务")
    owner = db.get(User, source.owner_id) if source.owner_id is not None else None
    agent = db.get(Agent, source.agent_id) if source.agent_id is not None else None
    if owner is None or not owner.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "任务所有者不存在或已禁用")
    if agent is None or not agent.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "任务智能体不存在或已停用")

    payload = copy.deepcopy(_json_dict(source.payload))
    if not payload:
        raise HTTPException(status.HTTP_409_CONFLICT, "任务快照无效，无法安全复制")
    snapshot = payload.get("execution_snapshot")
    provider_data = snapshot.get("provider") if isinstance(snapshot, dict) else None
    provider_id = provider_data.get("id") if isinstance(provider_data, dict) else None
    if provider_id is not None:
        provider = db.get(ModelProvider, int(provider_id))
        if provider is None or not provider.enabled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "原任务模型提供商已停用或不存在，请恢复后再复制重试",
            )

    payload.pop("turn_id", None)
    payload.pop("idempotency_key", None)
    payload["continuation_of_turn_id"] = source.id
    payload["operations_retry_of"] = source.id
    # Existing attachment rows are inherited by reference.  They must not be
    # re-declared as newly uploaded records for the new Turn.
    payload["attachment_records"] = []
    payload["attachments_inherited"] = bool(payload.get("attachment_context"))
    _force_fresh_approval(payload)
    _filter_paused_snapshot_mcp(db, snapshot)

    try:
        new_job_id = jobs.enqueue_in_session(
            db,
            source.owner_id,
            source.agent_id,
            source.kind,
            payload,
        )
        task_store.append_runtime_item(db, source.id, "operations.retry_copied", {
            "new_job_id": new_job_id,
            "approval_policy": "ask",
        })
        task_store.append_runtime_item(db, new_job_id, "operations.retry_copy_created", {
            "source_job_id": source.id,
            "approval_policy": "ask",
        })
        db.commit()
    except jobs.JobQuotaExceeded as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    created = db.get(Job, new_job_id)
    users, agents, turns = _job_identity_maps(db, [created])
    return {
        "message": "已复制为新任务；原失败记录保持不变，写操作需要重新批准",
        "source_job_id": source.id,
        "job": _job_out(created, users=users, agents=agents, turns=turns),
    }


@router.patch("/jobs/{job_id}/priority")
def update_pending_job_priority(
    job_id: str,
    body: JobPriorityRequest,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    row = db.get(Job, job_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if row.status != jobs.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, "只有待处理任务可调整优先级")
    if body.priority < -100 or body.priority > 100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "priority 必须在 -100 到 100 之间")
    row.priority = int(body.priority)
    db.commit()
    return {"job_id": row.id, "priority": row.priority, "status": row.status}


@router.post("/schedulers/{scheduler_id}/ack-error")
def acknowledge_scheduler_error(
    scheduler_id: str,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    row = db.get(SchedulerHeartbeat, scheduler_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduler 不存在")
    had_error = bool(row.last_error)
    row.last_error = ""
    db.commit()
    return {
        "scheduler_id": scheduler_id,
        "acknowledged": had_error,
        "message": "已确认当前 Scheduler 心跳错误；定时任务历史未被修改",
    }


@router.patch("/resources/{resource_type}/{resource_id}/state")
async def set_operational_resource_state(
    resource_type: str,
    resource_id: int,
    body: ResourceStateRequest,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    descriptor = _RESOURCE_MODELS.get(resource_type)
    if descriptor is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "resource_type 只支持 provider、mcp、channel",
        )
    model, label = descriptor
    row = db.get(model, resource_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label}不存在")
    changed = bool(row.enabled) != bool(body.enabled)
    row.enabled = bool(body.enabled)
    db.commit()
    db.refresh(row)

    if resource_type == "channel" and row.type == WEIXIN_CHANNEL_TYPE:
        if not row.enabled:
            weixin_manager.stop_monitor(row.id)
        elif row.connection_status == "connected":
            weixin_manager.start_monitor(row.id)
    return {
        "resource_type": resource_type,
        "id": row.id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "changed": changed,
        "scope": (
            "新任务使用；已领取或已固化快照的任务不会被改写"
            if resource_type in {"provider", "mcp"}
            else "新的渠道入口和后台监听"
        ),
    }
