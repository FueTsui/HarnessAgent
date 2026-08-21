"""开放 API（第三方系统集成，X-API-Key 鉴权）。

- POST /open/v1/chat            纯 JSON 调用（无文件）
- POST /open/v1/chat/multipart  带文件调用（字段同前端 /api/v1/chat）
- GET  /open/v1/agents          可用智能体列表
"""
import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from sqlalchemy.orm import Session

from .. import jobs
from ..config import settings
from ..database import get_db
from ..models import Agent, ApiKey, User
from ..runtime import TaskInput
from ..schemas import OpenChatRequest, OpenChatResponse
from ..security import can_access_agent, verify_api_key
from .chat import (
    DOC_SUFFIXES, IMAGE_SUFFIXES, build_execution_snapshot, resolve_agent,
    save_uploads, select_chat_provider, serialize_input,
)

router = APIRouter(prefix="/open/v1", tags=["开放API（X-API-Key）"])


def _key_owner(db: Session, key: ApiKey) -> User:
    owner = db.get(User, key.created_by)
    if owner is None or not owner.is_active:
        raise HTTPException(status_code=401, detail="API Key 所属账号不存在或已禁用")
    return owner


@router.get("/agents")
def open_list_agents(key: ApiKey = Depends(verify_api_key), db: Session = Depends(get_db)):
    owner = _key_owner(db, key)
    rows = [
        agent for agent in db.query(Agent).filter(Agent.enabled.is_(True)).order_by(Agent.id).all()
        if can_access_agent(owner, agent)
    ]
    return [
        {"id": a.id, "name": a.name, "description": a.description, "active_version": a.active_version}
        for a in rows
    ]


def _enqueue_open_chat(
    db: Session,
    key: ApiKey,
    agent: Agent,
    inputs: TaskInput,
    *,
    idempotency_key: str | None = None,
) -> str:
    owner = _key_owner(db, key)
    provider = select_chat_provider(db, owner, agent, None, inputs.query)
    run_id = uuid.uuid4().hex
    payload = {
        "user_id": owner.id,
        "agent_id": agent.id,
        "harness_version": agent.active_version,
        "inputs": serialize_input(inputs),
        "template_ids": [],
        "dataset_ids": [],
        "skill_ids": [],
        "mcp_ids": [],
        "invoked_agent_ids": [],
        "provider_id": getattr(provider, "id", None),
        "attachment_images": [],
        "attachment_docs": [],
        "session_id": run_id,
        "project_id": None,
        "source": "open_api",
        "approval_tokens": [],
        "execution_snapshot": build_execution_snapshot(
            db, agent, inputs.query, provider=provider
        ),
    }
    job_id = jobs.enqueue_in_session(
        db,
        owner.id,
        agent.id,
        "chat",
        payload,
        idempotency_key=idempotency_key,
    )
    db.commit()
    return job_id


def _open_result(view: jobs.JobView) -> OpenChatResponse:
    result = view.result or {}
    completion_status = (
        str(result.get("completion_status") or "completed")
        if view.status == jobs.DONE else ""
    )
    if completion_status not in {"", "completed", "completed_with_issues"}:
        completion_status = "completed"
    raw_issues = result.get("completion_issues")
    raw_issues = raw_issues if isinstance(raw_issues, (list, tuple)) else []
    raw_summary = result.get("plan_summary")
    raw_summary = raw_summary if isinstance(raw_summary, dict) else {}
    return OpenChatResponse(
        turn_id=view.id,
        status=view.status,
        task_status=completion_status or view.status,
        completion_status=completion_status,
        completion_issues=[str(value)[:400] for value in raw_issues[:20]],
        plan_summary=dict(raw_summary),
        answer=str(result.get("answer") or ""),
        export_files=list(result.get("export_files") or []),
        thread_id=result.get("thread_id"),
    )


async def _wait_open_job(job_id: str, owner_id: int) -> OpenChatResponse:
    deadline = asyncio.get_running_loop().time() + settings.OPEN_API_SYNC_WAIT_SECONDS
    while True:
        view = await asyncio.to_thread(jobs.view, job_id, owner_id)
        if view is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if view.status in jobs._TERMINAL:
            if view.status != jobs.DONE:
                raise HTTPException(status_code=502, detail=view.error or "任务执行失败")
            return _open_result(view)
        if asyncio.get_running_loop().time() >= deadline:
            return _open_result(view)
        await asyncio.sleep(0.2)


def _idempotency(request: Request, key: ApiKey) -> str | None:
    value = (request.headers.get("idempotency-key") or "").strip()
    if len(value) > 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key 最长 128 字符")
    return f"open:{key.id}:{value}" if value else None


@router.post("/chat", response_model=OpenChatResponse)
async def open_chat(
    body: OpenChatRequest,
    request: Request,
    key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    owner = _key_owner(db, key)
    agent = resolve_agent(db, body.agent_id, owner)
    inputs = TaskInput(
        query=body.query,
        project_name=body.project_name,
        city_name=body.city_name,
        project_address=body.project_address,
        project_info=body.project_info,
        industry_structure=body.industry_structure,
        electricity_trading=body.electricity_trading,
        image_scale=body.image_scale,
    )
    job_id = _enqueue_open_chat(
        db, key, agent, inputs, idempotency_key=_idempotency(request, key)
    )
    db.close()
    return await _wait_open_job(job_id, owner.id)


@router.post("/chat/multipart", response_model=OpenChatResponse)
async def open_chat_multipart(
    request: Request,
    agent_id: Optional[int] = Form(default=None),
    query: str = Form(default=""),
    project_name: str = Form(default=""),
    city_name: str = Form(default=""),
    project_address: str = Form(default=""),
    project_info: str = Form(default=""),
    industry_structure: str = Form(default=""),
    electricity_trading: str = Form(default=""),
    image_scale: str = Form(default=""),
    satellite_images: Optional[list[UploadFile]] = File(default=None),
    drawing_images: Optional[list[UploadFile]] = File(default=None),
    bill_files: Optional[list[UploadFile]] = File(default=None),
    documents: Optional[list[UploadFile]] = File(default=None),
    key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    owner = _key_owner(db, key)
    agent = resolve_agent(db, agent_id, owner)
    inputs = TaskInput(
        query=query,
        project_name=project_name,
        city_name=city_name,
        project_address=project_address,
        project_info=project_info,
        industry_structure=industry_structure,
        electricity_trading=electricity_trading,
        image_scale=image_scale,
        satellite_images=await save_uploads(satellite_images, IMAGE_SUFFIXES, "卫星图"),
        drawing_images=await save_uploads(drawing_images, IMAGE_SUFFIXES, "屋面图纸"),
        bill_files=await save_uploads(bill_files, IMAGE_SUFFIXES, "电费单"),
        documents=await save_uploads(documents, DOC_SUFFIXES, "项目文档"),
    )
    job_id = _enqueue_open_chat(
        db, key, agent, inputs, idempotency_key=_idempotency(request, key)
    )
    db.close()
    return await _wait_open_job(job_id, owner.id)


@router.get("/chat/turns/{job_id}", response_model=OpenChatResponse)
def open_chat_job(
    job_id: str,
    key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    owner = _key_owner(db, key)
    view = jobs.view(job_id, owner.id)
    if view is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _open_result(view)
