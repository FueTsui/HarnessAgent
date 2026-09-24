"""Explicit, account-owned memory records and scope-safe retrieval."""
import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, and_, or_
from sqlalchemy.orm import Mapped, Session, mapped_column

from .database import Base
from .models import Agent, Project, Thread, _now, iso_utc

SCOPES = ("custom", "context", "project", "global")
SCOPE_LABELS = {"custom": "自定义记忆", "context": "上下文记忆", "project": "项目记忆", "global": "全局记忆"}


class MemoryEntry(Base):
    __tablename__ = "memory_entries"
    __table_args__ = (
        CheckConstraint("scope IN ('custom','context','project','global')", name="ck_memory_scope"),
        CheckConstraint(
            "(scope = 'custom' AND agent_id IS NOT NULL AND thread_id IS NULL AND project_id IS NULL) OR "
            "(scope = 'context' AND thread_id IS NOT NULL AND agent_id IS NULL AND project_id IS NULL) OR "
            "(scope = 'project' AND project_id IS NOT NULL AND agent_id IS NULL AND thread_id IS NULL) OR "
            "(scope = 'global' AND agent_id IS NULL AND thread_id IS NULL AND project_id IS NULL)",
            name="ck_memory_scope_target",
        ),
        Index("ix_memory_owner_scope_enabled", "owner_id", "scope", "enabled"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(300), default="手动录入")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    agent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("threads.id", ondelete="CASCADE"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


def serialize_memory(row: MemoryEntry, db: Session) -> dict:
    target = db.get(Agent, row.agent_id) if row.agent_id else db.get(Thread, row.thread_id) if row.thread_id else db.get(Project, row.project_id) if row.project_id else None
    return {
        "id": row.id, "scope": row.scope, "title": row.title, "content": row.content,
        "source": row.source, "enabled": row.enabled, "agent_id": row.agent_id,
        "thread_id": row.thread_id, "project_id": row.project_id,
        "target_name": (getattr(target, "name", None) or getattr(target, "title", None) or "未命名") if target else "当前账户全部项目",
        "created_at": iso_utc(row.created_at), "updated_at": iso_utc(row.updated_at),
    }


def explicit_candidates(db: Session, owner_id: int, *, session_id: str = "", agent_id: int | None = None, limit: int = 150) -> list[dict]:
    """Resolve scopes from an owned Thread, never from a caller-supplied project ID.

    Root receives exactly the same owner constraint as every other account.
    Deleted/disabled records and excluded contexts are not recall candidates.
    """
    if not owner_id:
        return []
    thread = db.get(Thread, session_id) if session_id else None
    if thread is not None and thread.owner_id != owner_id:
        return []
    if thread is not None and not thread.memory_enabled:
        return []
    scopes = [MemoryEntry.scope == "global"]
    if agent_id is not None:
        scopes.append(and_(MemoryEntry.scope == "custom", MemoryEntry.agent_id == agent_id))
    if thread is not None:
        if not thread.memory_excluded:
            scopes.append(and_(MemoryEntry.scope == "context", MemoryEntry.thread_id == thread.id))
        project = db.get(Project, thread.project_id) if thread.project_id else None
        if project is not None and project.user_id == owner_id:
            scopes.append(and_(MemoryEntry.scope == "project", MemoryEntry.project_id == project.id))
    rows = db.query(MemoryEntry).filter(
        MemoryEntry.owner_id == owner_id, MemoryEntry.enabled.is_(True), or_(*scopes),
    ).order_by(MemoryEntry.updated_at.desc(), MemoryEntry.id.desc()).limit(max(1, min(limit, 500))).all()
    return [{
        "id": f"memory:{row.id}", "memory_id": row.id, "scope": row.scope,
        "session_id": row.thread_id or "", "thread_title": "",
        "query": row.title, "answer": row.content, "created_at": row.updated_at,
        "source": row.source, "source_type": "explicit", "weight_multiplier": .75,
    } for row in rows]
