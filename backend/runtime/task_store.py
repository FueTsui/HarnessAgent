"""Project / Thread / Turn / Item 持久化原语。"""
from __future__ import annotations

import datetime
import json
import uuid

from sqlalchemy import func

from ..models import Item, Thread, Turn


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def ensure_thread(
    db,
    *,
    thread_id: str,
    owner_id: int,
    agent_id: int | None,
    project_id: int | None = None,
) -> Thread:
    thread = db.get(Thread, thread_id)
    if thread is None:
        thread = Thread(
            id=thread_id,
            owner_id=owner_id,
            agent_id=agent_id,
            project_id=project_id,
        )
        db.add(thread)
        db.flush()
    elif thread.owner_id != owner_id:
        raise PermissionError("Thread 不属于当前用户")
    return thread


def create_turn(
    db,
    *,
    thread: Thread,
    input_text: str,
    payload: dict,
    source: str = "web",
    turn_id: str | None = None,
) -> Turn:
    sequence = int(
        db.query(func.max(Turn.sequence)).filter(Turn.thread_id == thread.id).scalar() or 0
    ) + 1
    turn = Turn(
        id=turn_id or uuid.uuid4().hex,
        continuation_of_turn_id=(
            str(payload.get("continuation_of_turn_id") or "")[:32] or None
        ),
        thread_id=thread.id,
        owner_id=thread.owner_id,
        agent_id=thread.agent_id,
        sequence=sequence,
        status="queued",
        source=source,
        input=input_text,
        execution_snapshot=json.dumps(payload, ensure_ascii=False),
    )
    db.add(turn)
    db.flush()
    append_item(
        db,
        turn,
        kind="message",
        role="user",
        status="completed",
        content=input_text,
        payload={
            "source": source,
            "attachments": list(payload.get("attachment_context") or []),
            "continuation_of_turn_id": payload.get("continuation_of_turn_id"),
        },
    )
    return turn


def append_item(
    db,
    turn: Turn,
    *,
    kind: str,
    role: str = "",
    name: str = "",
    status: str = "completed",
    content: str = "",
    payload: dict | None = None,
) -> Item:
    sequence = int(turn.item_sequence or 0) + 1
    turn.item_sequence = sequence
    item = Item(
        id=uuid.uuid4().hex,
        thread_id=turn.thread_id,
        turn_id=turn.id,
        sequence=sequence,
        kind=(kind or "event")[:32],
        role=(role or "")[:16],
        name=(name or "")[:96],
        status=(status or "completed")[:24],
        content=content or "",
        payload=json.dumps(payload or {}, ensure_ascii=False),
    )
    db.add(item)
    return item


def append_runtime_item(
    db, turn_id: str, event_type: str, payload: dict | None = None
) -> Item | None:
    turn = db.get(Turn, turn_id)
    if turn is None:
        return None
    event = payload or {}
    kind = "event"
    role = ""
    name = event_type
    content = ""
    status = "completed"
    if event_type in {"plan.created", "plan.updated"}:
        kind = "plan"
    elif event_type.startswith("step."):
        kind = "plan_step"
        status = {
            "step.started": "started",
            "step.completed": "completed",
            "step.failed": "failed",
            "step.blocked": "blocked",
            "step.skipped": "skipped",
        }.get(event_type, "completed")
    elif event_type.startswith("tool."):
        kind = "tool_call" if event_type == "tool.called" else "tool_result"
        name = str(event.get("tool") or event_type)
        if event_type == "tool.called":
            status = "started"
        elif event_type == "tool.deferred":
            status = "deferred"
        else:
            status = "completed" if event.get("ok", True) else "failed"
    elif event_type.startswith("approval."):
        kind = "approval"
        status = "pending" if event_type == "approval.requested" else "completed"
    elif event_type.startswith("verification."):
        kind = "verification"
        status = "failed" if event_type == "verification.failed" else "completed"
    elif event_type.startswith("evaluation."):
        kind = "evaluation"
        status = (
            "started" if event_type == "evaluation.started" else
            "failed" if event.get("decision") == "failed" else "completed"
        )
    elif event_type.startswith("interaction.") or event_type.startswith("guidance."):
        kind = "control"
        status = "started" if event_type.endswith(("received", "created")) else "completed"
    elif (
        event_type.startswith("loop.")
        or event_type.startswith("turn.")
        or event_type.startswith("task.")
    ):
        kind = "lifecycle"
    return append_item(
        db,
        turn,
        kind=kind,
        role=role,
        name=name,
        status=status,
        content=content,
        payload=event,
    )


def thread_messages(db, *, owner_id: int, thread_id: str) -> list[dict]:
    thread = db.get(Thread, thread_id)
    if thread is None or thread.owner_id != owner_id:
        return []
    rows = (
        db.query(Item)
        .join(Turn, Item.turn_id == Turn.id)
        .filter(
            Item.thread_id == thread_id,
            Item.kind == "message",
            Item.role.in_(("user", "assistant")),
        )
        .order_by(Turn.sequence, Item.sequence)
        .all()
    )
    return [{"role": row.role, "content": row.content} for row in rows if row.content]


def finish_turn(
    db,
    turn: Turn,
    *,
    answer: str,
    status: str = "completed",
    error: str = "",
) -> None:
    now = _now()
    turn.status = status
    turn.final_output = answer or ""
    turn.error = error or ""
    turn.completed_at = now
    thread = db.get(Thread, turn.thread_id)
    if thread is not None:
        thread.updated_at = now
    if answer:
        append_item(db, turn, kind="message", role="assistant", content=answer)
