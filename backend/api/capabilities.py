"""内置能力目录与 Cron 管理接口。"""
from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import scheduler
from ..database import get_db
from ..models import Agent, User
from ..runtime.builtin_tools import (
    capability_catalog,
    set_global_enabled,
)
from ..security import can_access_agent, get_current_user, is_root, require_module, require_root

router = APIRouter(prefix="/api/v1", tags=["内置能力"])


class ScheduleCreate(BaseModel):
    name: str = Field(default="定时任务", max_length=128)
    agent_id: int
    cron: str = Field(min_length=1, max_length=128)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    query: str = Field(min_length=1, max_length=30000)
    session_id: str | None = Field(default=None, max_length=40)


class ScheduleEnabled(BaseModel):
    enabled: bool


@router.get("/capabilities")
def list_capabilities(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {
        "builtin_tools": capability_catalog(db),
        "can_manage": is_root(user),
    }


@router.get("/capabilities/manage")
def manage_capabilities(
    root: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    return capability_catalog(db)


@router.patch("/capabilities/{name}")
def update_capability(
    name: str,
    body: ScheduleEnabled,
    root: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    try:
        set_global_enabled(db, name, body.enabled)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "内置能力不存在") from exc
    db.commit()
    return next(row for row in capability_catalog(db) if row["name"] == name)


@router.get("/schedules")
def list_schedules(
    agent_id: int | None = None,
    session_id: str | None = None,
    user: User = Depends(require_module("schedules")),
):
    return scheduler.list_tasks(user.id, agent_id, session_id)


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
def create_schedule(
    body: ScheduleCreate,
    user: User = Depends(require_module("schedules")),
    db: Session = Depends(get_db),
):
    agent = db.get(Agent, body.agent_id)
    if not can_access_agent(user, agent):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "智能体不存在或不可用")
    try:
        return scheduler.create(
            user.id, body.agent_id, body.name, body.cron, body.timezone,
            body.query, body.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.patch("/schedules/{task_id}")
def update_schedule(
    task_id: str,
    body: ScheduleEnabled,
    user: User = Depends(require_module("schedules")),
):
    result = scheduler.set_enabled(user.id, task_id, body.enabled)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
    return result


@router.delete("/schedules/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    task_id: str,
    user: User = Depends(require_module("schedules")),
):
    if not scheduler.delete(user.id, task_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
