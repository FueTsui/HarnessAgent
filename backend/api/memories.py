"""User-managed memory. Administrative roles do not grant cross-account access."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..memory_store import MemoryEntry, SCOPES, serialize_memory
from ..models import Agent, Project, Thread, User
from ..security import can_access_agent, require_module

router = APIRouter(prefix="/api/v1/memories", tags=["分层记忆"])
memory_user = require_module("memory")
Scope = Literal["custom", "context", "project", "global"]


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Scope = "global"
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=6000)
    source: str = Field(default="手动录入", max_length=300)
    enabled: bool = True
    agent_id: int | None = None
    thread_id: str | None = Field(default=None, max_length=40)
    project_id: int | None = None


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Scope | None = None
    title: str | None = Field(default=None, min_length=1, max_length=160)
    content: str | None = Field(default=None, min_length=1, max_length=6000)
    source: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None
    agent_id: int | None = None
    thread_id: str | None = Field(default=None, max_length=40)
    project_id: int | None = None


def owned_memory(db: Session, user: User, entry_id: int) -> MemoryEntry:
    row = db.query(MemoryEntry).filter(MemoryEntry.id == entry_id, MemoryEntry.owner_id == user.id).first()
    if row is None:
        raise HTTPException(404, "记忆不存在")
    return row


def validate_values(values: dict, db: Session, user: User) -> dict:
    values = dict(values)
    for field in ("title", "content"):
        values[field] = (values.get(field) or "").strip()
        if not values[field]:
            raise HTTPException(422, "标题和记忆内容不能为空")
    values["source"] = (values.get("source") or "手动录入").strip()
    scope = values["scope"]
    if scope == "custom":
        target = db.get(Agent, values.get("agent_id")) if values.get("agent_id") else None
        if target is None or not can_access_agent(user, target):
            raise HTTPException(404, "请选择可使用的智能体")
    elif scope == "context":
        target = db.get(Thread, values.get("thread_id")) if values.get("thread_id") else None
        if target is None or target.owner_id != user.id:
            raise HTTPException(404, "请选择当前账户的会话")
    elif scope == "project":
        target = db.get(Project, values.get("project_id")) if values.get("project_id") else None
        if target is None or target.user_id != user.id:
            raise HTTPException(404, "请选择当前账户的项目")
    values["agent_id"] = values.get("agent_id") if scope == "custom" else None
    values["thread_id"] = values.get("thread_id") if scope == "context" else None
    values["project_id"] = values.get("project_id") if scope == "project" else None
    return values


@router.get("/targets")
def memory_targets(user: User = Depends(memory_user), db: Session = Depends(get_db)):
    projects = db.query(Project).filter(Project.user_id == user.id).order_by(Project.name).all()
    threads = db.query(Thread).filter(Thread.owner_id == user.id).order_by(Thread.updated_at.desc()).limit(500).all()
    agents = [row for row in db.query(Agent).filter(Agent.enabled.is_(True)).order_by(Agent.name).all() if can_access_agent(user, row)]
    return {"projects": [{"id": row.id, "name": row.name} for row in projects],
            "threads": [{"id": row.id, "name": row.title or "未命名会话", "project_id": row.project_id, "excluded": row.memory_excluded} for row in threads],
            "agents": [{"id": row.id, "name": row.name, "memory_enabled": row.memory_enabled} for row in agents]}


@router.get("")
def list_memories(scope: Scope | None = None, q: str = Query("", max_length=200), enabled: bool | None = None,
                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  user: User = Depends(memory_user), db: Session = Depends(get_db)):
    base = db.query(MemoryEntry).filter(MemoryEntry.owner_id == user.id)
    counts = {key: 0 for key in SCOPES}
    for key, count in db.query(MemoryEntry.scope, func.count(MemoryEntry.id)).filter(MemoryEntry.owner_id == user.id).group_by(MemoryEntry.scope):
        counts[key] = count
    if scope:
        base = base.filter(MemoryEntry.scope == scope)
    if enabled is not None:
        base = base.filter(MemoryEntry.enabled.is_(enabled))
    if q.strip():
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        base = base.filter(or_(MemoryEntry.title.ilike(f"%{term}%", escape="\\"), MemoryEntry.content.ilike(f"%{term}%", escape="\\")))
    total = base.count()
    rows = base.order_by(MemoryEntry.updated_at.desc(), MemoryEntry.id.desc()).offset(offset).limit(limit).all()
    return {"items": [serialize_memory(row, db) for row in rows], "total": total, "counts": counts, "offset": offset, "limit": limit}


@router.post("", status_code=201)
def create_memory(body: MemoryCreate, user: User = Depends(memory_user), db: Session = Depends(get_db)):
    row = MemoryEntry(owner_id=user.id, **validate_values(body.model_dump(), db, user))
    db.add(row)
    db.commit()
    db.refresh(row)
    return serialize_memory(row, db)


@router.get("/{entry_id}")
def read_memory(entry_id: int, user: User = Depends(memory_user), db: Session = Depends(get_db)):
    return serialize_memory(owned_memory(db, user, entry_id), db)


@router.patch("/{entry_id}")
def update_memory(entry_id: int, body: MemoryUpdate, user: User = Depends(memory_user), db: Session = Depends(get_db)):
    row = owned_memory(db, user, entry_id)
    updates = body.model_dump(exclude_unset=True)
    if any(updates.get(key) is None for key in ("scope", "title", "content", "enabled") if key in updates):
        raise HTTPException(422, "范围、标题、内容和启用状态不能为 null")
    values = {key: getattr(row, key) for key in MemoryCreate.model_fields}
    values.update(updates)
    for key, value in validate_values(values, db, user).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return serialize_memory(row, db)


@router.delete("/{entry_id}", status_code=204)
def delete_memory(entry_id: int, user: User = Depends(memory_user), db: Session = Depends(get_db)):
    db.delete(owned_memory(db, user, entry_id))
    db.commit()
    return Response(status_code=204)
