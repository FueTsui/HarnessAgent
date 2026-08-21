"""Turn 的持久化调度队列。

长 Turn 不占用 HTTP 请求与数据库会话：Thread、Turn 和首个消息 Item 创建后，同时写入
一行 `jobs` 调度记录；由 worker（进程内 asyncio 任务，或独立 `python run.py worker`）
领取执行。前端通过 turn_id 观察、引导或取消。

相对早期「内存级 JobManager」的增量收益：
- 跨重启不丢：任务状态落库；心跳超时的 running 任务会被重新入队（requeue_stale）。
- 可被独立 worker 进程消费：为多副本/横向扩展铺路（claim_next 用「带条件 UPDATE」原子领取，
  SQLite 与 Postgres 均安全）。
- 取消跨进程可用：协作式取消（置 cancel_requested，运行任务在检查点感知并中止）；
  进程内另保留 task 句柄以便即时取消。

本模块只依赖 models / database，不引入运行时编排器，避免循环导入。
"""
import asyncio
import datetime
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from .config import UPLOAD_DIR, settings
from .database import SessionLocal
from .models import Attachment, Item, Job, JobGuidance, Turn, WorkerHeartbeat
from .runtime import task_store

logger = logging.getLogger(__name__)

# 任务状态
PENDING, RUNNING, AWAITING_APPROVAL, DONE, FAILED, CANCELLED, DEAD_LETTER = (
    "pending", "running", "awaiting_approval", "done", "failed", "cancelled",
    "dead_letter",
)
_TERMINAL = {DONE, FAILED, CANCELLED, DEAD_LETTER}
_INFLIGHT = {PENDING, RUNNING, AWAITING_APPROVAL}
GUIDANCE_PENDING, GUIDANCE_CLAIMED, GUIDANCE_APPLIED, GUIDANCE_CANCELLED, GUIDANCE_QUEUED = (
    "pending", "claimed", "applied", "cancelled", "queued",
)


class JobQuotaExceeded(RuntimeError):
    """队列或用户在途任务超过部署方配置的硬上限。"""


def _now():
    # 与 models._now 一致：带时区的 UTC
    return datetime.datetime.now(datetime.timezone.utc)


def _event_meta(item: Item | None) -> dict:
    """把持久 Item 转成所有实时/回放事件共用的公开信封。"""
    if item is None:
        return {}
    created = item.created_at or _now()
    if created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    return {
        "task_id": item.turn_id,
        "event_id": item.id,
        "timestamp": created.astimezone(datetime.timezone.utc).isoformat(),
        "revision": int(item.sequence or 0),
    }


@dataclass
class JobView:
    """任务的只读快照（脱离 ORM 会话，便于跨短会话/跨 await 传递）。"""
    id: str
    owner_id: Optional[int]
    agent_id: Optional[int]
    parent_job_id: Optional[str]
    kind: str
    payload: dict
    status: str
    progress: str
    result: Optional[dict]
    error: str
    cancel_requested: bool
    worker_id: str
    lease_token: str
    attempt_count: int
    max_attempts: int
    priority: int
    error_class: str


def _view(job: Job) -> JobView:
    try:
        payload = json.loads(job.payload) if job.payload else {}
    except json.JSONDecodeError:
        payload = {}
    try:
        result = json.loads(job.result) if job.result else None
    except json.JSONDecodeError:
        result = None
    return JobView(
        id=job.id, owner_id=job.owner_id, agent_id=job.agent_id,
        parent_job_id=job.parent_job_id, kind=job.kind,
        payload=payload if isinstance(payload, dict) else {},
        status=job.status, progress=job.progress, result=result,
        error=job.error or "", cancel_requested=bool(job.cancel_requested),
        worker_id=job.worker_id or "", lease_token=job.lease_token or "",
        attempt_count=int(job.attempt_count or 0),
        max_attempts=int(job.max_attempts or 3),
        priority=int(getattr(job, "priority", 0) or 0),
        error_class=str(getattr(job, "error_class", "") or ""),
    )


# ---------- 进程内运行任务句柄（即时取消用；跨进程取消仍靠 cancel_requested） ----------
_local_tasks: "dict[str, asyncio.Task]" = {}


def register_local(job_id: str, task: "asyncio.Task") -> None:
    _local_tasks[job_id] = task


def unregister_local(job_id: str) -> None:
    _local_tasks.pop(job_id, None)


# ---------- 进程内「流式部分答案」缓冲 + 事件发布订阅（边生成边显示） ----------
# 仅进程内 worker（默认）有效：执行任务时把 LLM 增量累积在内存，并向订阅者（流式接口）实时
# 推送事件（不写 DB，避免把每个 token 都写到网络共享上的 SQLite）。独立 worker 进程不共享此内存，
# 流式接口靠 DB 终态收尾、前端自动退化为轮询，任务完成后从 DB 拿到完整答案。
_partials: "dict[str, str]" = {}
_partial_flush: "dict[str, tuple[float, int]]" = {}
_subscribers: "dict[str, set[asyncio.Queue]]" = {}


def _publish(job_id: str, event: dict) -> None:
    """向某 job 的所有进程内订阅者推送事件；无订阅者时无操作。"""
    for q in list(_subscribers.get(job_id, ())):
        try:
            q.put_nowait(event)
        except Exception:  # noqa: BLE001 - 单个订阅者异常不影响其它
            pass


def subscribe(job_id: str) -> "asyncio.Queue":
    """订阅某 job 的流式事件（delta/progress/end）。用完务必 unsubscribe。"""
    q: "asyncio.Queue" = asyncio.Queue()
    _subscribers.setdefault(job_id, set()).add(q)
    return q


def unsubscribe(job_id: str, q: "asyncio.Queue") -> None:
    subs = _subscribers.get(job_id)
    if subs is not None:
        subs.discard(q)
        if not subs:
            _subscribers.pop(job_id, None)


def append_partial(job_id: str, delta: str) -> None:
    if delta:
        value = _partials.get(job_id, "") + delta
        _partials[job_id] = value
        now = time.monotonic()
        last_time, last_length = _partial_flush.get(job_id, (now, 0))
        if (
            len(value) - last_length >= max(1, settings.JOB_PARTIAL_FLUSH_CHARS)
            or now - last_time >= max(0.1, settings.JOB_PARTIAL_FLUSH_SECONDS)
        ):
            db = SessionLocal()
            try:
                db.query(Job).filter(
                    Job.id == job_id,
                    Job.status == RUNNING,
                ).update(
                    {Job.partial_result: value},
                    synchronize_session=False,
                )
                db.commit()
                _partial_flush[job_id] = (now, len(value))
            finally:
                db.close()
        # 仅通知「有新内容」，内容以 get_partial 为准（基于长度发增量，天然幂等、不丢不重）
        _publish(job_id, {"type": "delta"})


def publish_progress(job_id: str, text: str, event_meta: dict | None = None) -> None:
    _publish(job_id, {
        "type": "progress",
        "text": (text or "")[:256],
        **(event_meta or {}),
    })


def publish_runtime_event(
    job_id: str,
    event_type: str,
    payload: dict | None = None,
    event_meta: dict | None = None,
) -> None:
    """从任务事件循环实时发布安全的结构化执行事件。"""
    _publish(job_id, {
        "type": "runtime",
        "event_type": (event_type or "runtime.event")[:32],
        "payload": payload or {},
        **(event_meta or {}),
    })


def get_partial(job_id: str) -> str:
    local = _partials.get(job_id)
    if local is not None:
        return local
    db = SessionLocal()
    try:
        row = db.query(Job.partial_result).filter(Job.id == job_id).first()
        return str(row[0] or "") if row else ""
    finally:
        db.close()


def clear_partial(job_id: str) -> None:
    _partials.pop(job_id, None)
    _partial_flush.pop(job_id, None)


def latest_event_meta(job_id: str, names: tuple[str, ...] = ()) -> dict:
    """读取最近一个持久事件的公开身份，供断线后的终态/审批流补发。"""
    db = SessionLocal()
    try:
        query = db.query(Item).filter(Item.turn_id == job_id)
        if names:
            query = query.filter(Item.name.in_(names))
        item = query.order_by(Item.sequence.desc()).first()
        return _event_meta(item)
    finally:
        db.close()


# ---------- 提交 / 查询（API 侧） ----------

def _append_event_in_session(
    db, job_id: str, event_type: str, payload: dict | None = None
) -> Item | None:
    """把 Turn 的运行过程追加为 Item。"""
    return task_store.append_runtime_item(db, job_id, event_type, payload)


def enqueue_in_session(
    db,
    owner_id: Optional[int],
    agent_id: Optional[int],
    kind: str,
    payload: dict,
    *,
    idempotency_key: str | None = None,
) -> str:
    key = (idempotency_key or "").strip()[:128] or None
    if key:
        existing = db.query(Job.id).filter(
            Job.owner_id == owner_id,
            Job.idempotency_key == key,
        ).first()
        if existing:
            return existing[0]
    if owner_id is not None:
        # 所有网页、开放 API、渠道、定时任务和子智能体均汇聚到此入口，
        # 因而在创建 Thread/Turn 前统一拒绝已超额用户，避免产生失败空会话。
        from .token_usage import token_limit_violation
        violation = token_limit_violation(db, owner_id)
        if violation:
            raise JobQuotaExceeded(violation)
    global_limit = max(0, int(settings.JOB_MAX_QUEUED_GLOBAL))
    if global_limit and db.query(Job.id).filter(Job.status.in_(_INFLIGHT)).count() >= global_limit:
        raise JobQuotaExceeded("系统任务队列已满，请稍后重试")
    user_limit = max(0, int(settings.JOB_MAX_INFLIGHT_PER_USER))
    if (
        owner_id is not None
        and user_limit
        and db.query(Job.id).filter(
            Job.owner_id == owner_id,
            Job.status.in_(_INFLIGHT),
        ).count() >= user_limit
    ):
        raise JobQuotaExceeded("当前用户在途任务已达上限，请等待已有任务完成")
    session_id = str(payload.get("session_id") or "").strip()[:40]
    turn = None
    job_id = uuid.uuid4().hex
    if kind == "chat" and owner_id is not None:
        session_id = session_id or uuid.uuid4().hex
        payload["session_id"] = session_id
        current_attachment_ids = {
            str(item.get("id") or "")
            for item in (payload.get("attachment_records") or [])
            if isinstance(item, dict)
        }
        payload["attachment_context"] = [
            {
                **item,
                "source_turn_id": (
                    job_id if str(item.get("id") or "") in current_attachment_ids
                    else item.get("source_turn_id")
                ),
            }
            for item in (payload.get("attachment_context") or [])
            if isinstance(item, dict)
        ]
        thread = task_store.ensure_thread(
            db,
            thread_id=session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            project_id=payload.get("project_id"),
        )
        inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
        turn = task_store.create_turn(
            db,
            thread=thread,
            input_text=str(payload.get("query") or inputs.get("query") or ""),
            payload=payload,
            source=str(payload.get("source") or "web"),
            turn_id=job_id,
        )
        payload["turn_id"] = turn.id
        from .attachments import persist_pending_records
        persist_pending_records(
            db,
            owner_id=owner_id,
            thread_id=session_id,
            turn_id=turn.id,
            records=list(payload.get("attachment_records") or []),
        )
    job = Job(
        id=job_id,
        owner_id=owner_id,
        agent_id=agent_id,
        parent_job_id=(str(payload.get("parent_run_id") or "")[:32] or None),
        kind=kind,
        payload=json.dumps(payload, ensure_ascii=False),
        status=PENDING,
        idempotency_key=key,
        session_key=f"{owner_id}:{session_id}" if owner_id and session_id else "",
        priority=int(payload.get("priority") or {
            "cron": 20,
            "channel": 10,
            "wechat": 10,
            "weixin": 10,
            "web": 0,
            "subagent": -5,
        }.get(str(payload.get("source") or "web"), 0)),
    )
    db.add(job)
    db.flush()
    _append_event_in_session(db, job.id, "turn.queued", {
        "agent_id": agent_id,
        "harness_version": payload.get("harness_version"),
        "source": payload.get("source", "web"),
        "parent_run_id": payload.get("parent_run_id"),
        "parent_agent_id": payload.get("parent_agent_id"),
        "continuation_of_turn_id": payload.get("continuation_of_turn_id"),
        "attachment_count": len(payload.get("attachment_context") or []),
    })
    _append_event_in_session(db, job.id, "task.queued", {"status": "queued"})
    return job.id


def enqueue(
    owner_id: Optional[int],
    agent_id: Optional[int],
    kind: str,
    payload: dict,
    *,
    idempotency_key: str | None = None,
) -> str:
    """登记 pending 任务；相同 owner+幂等键始终返回同一任务。"""
    db = SessionLocal()
    try:
        try:
            job_id = enqueue_in_session(
                db,
                owner_id,
                agent_id,
                kind,
                payload,
                idempotency_key=idempotency_key,
            )
            db.commit()
            return job_id
        except IntegrityError:
            db.rollback()
            if idempotency_key:
                existing = db.query(Job.id).filter(
                    Job.owner_id == owner_id,
                    Job.idempotency_key == idempotency_key[:128],
                ).first()
                if existing:
                    return existing[0]
            raise
    finally:
        db.close()


def view_by_idempotency(owner_id: int, idempotency_key: str) -> Optional[JobView]:
    """在处理上传等昂贵输入前查询已存在的幂等任务。"""
    key = (idempotency_key or "").strip()[:128]
    if not key:
        return None
    db = SessionLocal()
    try:
        row = db.query(Job).filter(
            Job.owner_id == owner_id,
            Job.idempotency_key == key,
        ).first()
        return _view(row) if row is not None else None
    finally:
        db.close()


def _payload_with_query(
    job: Job,
    content: str,
    *,
    session_id: str | None = None,
    project_id: int | None = None,
) -> dict:
    """复制任务快照并仅替换用户消息及可选线程归属。"""
    try:
        payload = json.loads(job.payload or "{}")
    except json.JSONDecodeError:
        payload = {}
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    else:
        inputs = dict(inputs)
    inputs["query"] = content
    payload["inputs"] = inputs
    payload.pop("_approval_scope", None)
    payload.pop("_approval_description", None)
    payload.pop("_approval_agent_id", None)
    payload.pop("_approval_execution_context", None)
    payload["approval_tokens"] = []
    if session_id is not None:
        payload["session_id"] = session_id
        payload["project_id"] = project_id
    return payload


def _create_followup_in_session(
    db,
    source: Job,
    content: str,
    *,
    session_id: str | None = None,
    project_id: int | None = None,
    control_context: dict | None = None,
) -> Job:
    payload = _payload_with_query(
        source,
        content,
        session_id=session_id,
        project_id=project_id,
    )
    if session_id is None:
        payload["continuation_of_turn_id"] = source.id
        payload["attachment_records"] = []
        payload["attachments_inherited"] = bool(payload.get("attachment_context"))
        payload["attachment_context"] = [
            {**item, "inherited": True}
            for item in (payload.get("attachment_context") or [])
            if isinstance(item, dict)
        ]
    else:
        # “移动到新对话”是明确的新任务边界，不隐式泄漏原线程附件和能力引用。
        for key in (
            "attachment_images", "attachment_docs", "attachment_ids",
            "attachment_records", "attachment_context",
        ):
            payload[key] = []
        payload["attachments_inherited"] = False
        payload["continuation_of_turn_id"] = None
    if isinstance(control_context, dict) and control_context:
        payload["duplex_control"] = {
            "mode": str(control_context.get("mode") or "")[:24],
            "interrupted_turn_id": str(
                control_context.get("interrupted_turn_id") or ""
            )[:32],
        }
    followup_session = str(payload.get("session_id") or "").strip()[:40]
    turn_id = uuid.uuid4().hex
    if source.owner_id is not None:
        followup_session = followup_session or uuid.uuid4().hex
        payload["session_id"] = followup_session
        thread = task_store.ensure_thread(
            db,
            thread_id=followup_session,
            owner_id=source.owner_id,
            agent_id=source.agent_id,
            project_id=payload.get("project_id"),
        )
        task_store.create_turn(
            db,
            thread=thread,
            input_text=content,
            payload=payload,
            source=str(payload.get("source") or "web"),
            turn_id=turn_id,
        )
        payload["turn_id"] = turn_id
    row = Job(
        id=turn_id,
        owner_id=source.owner_id,
        agent_id=source.agent_id,
        kind=source.kind,
        payload=json.dumps(payload, ensure_ascii=False),
        status=PENDING,
        session_key=(
            f"{source.owner_id}:{followup_session}"
            if source.owner_id and followup_session else ""
        ),
    )
    db.add(row)
    db.flush()
    _append_event_in_session(db, row.id, "turn.queued", {
        "agent_id": source.agent_id,
        "harness_version": payload.get("harness_version"),
        "source": payload.get("source", "web"),
        "promoted_from_guidance": True,
    })
    return row


def _promote_guidance_in_session(
    db,
    job: Job,
    statuses: tuple[str, ...],
) -> str | None:
    """把无法再由当前尝试消费的引导原子提升为同线程排队任务。"""
    rows = db.query(JobGuidance).filter(
        JobGuidance.job_id == job.id,
        JobGuidance.status.in_(statuses),
    ).order_by(JobGuidance.created_at, JobGuidance.id).all()
    if not rows:
        return None
    content = "\n\n".join(row.content for row in rows if (row.content or "").strip())
    if not content:
        for row in rows:
            row.status = GUIDANCE_CANCELLED
        return None
    followup = _create_followup_in_session(db, job, content)
    now = _now()
    for row in rows:
        row.status = GUIDANCE_QUEUED
        row.applied_at = now
    _append_event_in_session(db, job.id, "guidance.promoted", {
        "guidance_ids": [row.id for row in rows],
        "queued_job_id": followup.id,
        "count": len(rows),
    })
    return followup.id


def view(job_id: str, owner_id: Optional[int] = None) -> Optional[JobView]:
    """按归属取任务快照：owner_id 给定时仅返回属于本人的任务（None 表示放行，仅内部用）。"""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return None
        if owner_id is not None and job.owner_id != owner_id:
            return None
        return _view(job)
    finally:
        db.close()


def add_guidance(job_id: str, owner_id: int, content: str) -> dict:
    """给尚在执行链路中的任务追加一条可撤回引导。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("引导消息不能为空")
    if len(text) > 4000:
        raise ValueError("引导消息最长 4000 字符")
    db = SessionLocal()
    try:
        exists_for_owner = db.query(Job.id).filter(
            Job.id == job_id,
            Job.owner_id == owner_id,
            Job.kind == "chat",
        ).first()
        if not exists_for_owner:
            raise LookupError("任务不存在")
        # 条件空更新锁住任务行，使“接收引导”和终态转换形成确定先后关系。
        open_job = db.query(Job).filter(
            Job.id == job_id,
            Job.owner_id == owner_id,
            Job.kind == "chat",
            Job.status.in_((PENDING, RUNNING)),
        ).update({Job.updated_at: Job.updated_at}, synchronize_session=False)
        if not open_job:
            raise RuntimeError("当前任务已不能接收引导，请加入队列")
        pending_count = db.query(JobGuidance.id).filter(
            JobGuidance.job_id == job_id,
            JobGuidance.status == GUIDANCE_PENDING,
        ).count()
        if pending_count >= 20:
            raise RuntimeError("待处理引导过多，请加入队列")
        row = JobGuidance(
            id=uuid.uuid4().hex,
            job_id=job_id,
            owner_id=owner_id,
            content=text,
            status=GUIDANCE_PENDING,
        )
        db.add(row)
        _append_event_in_session(db, job_id, "guidance.queued", {
            "guidance_id": row.id,
            "chars": len(text),
        })
        db.commit()
        return {"id": row.id, "job_id": job_id, "content": text, "status": row.status}
    finally:
        db.close()


def _queue_redirect_in_session(db, source: Job, owner_id: int, text: str) -> Job:
    """Create the successor and its audit events inside the caller's transaction."""
    open_job = db.query(Job).filter(
        Job.id == source.id,
        Job.owner_id == owner_id,
        Job.kind == "chat",
        Job.status.in_((PENDING, RUNNING, AWAITING_APPROVAL)),
    ).update({Job.updated_at: Job.updated_at}, synchronize_session=False)
    if not open_job:
        raise RuntimeError("当前任务已不能重定向，请发送新消息")

    followup = _create_followup_in_session(
        db,
        source,
        text,
        control_context={
            "mode": "redirect",
            "interrupted_turn_id": source.id,
        },
    )
    # 同一 Thread 的 session_key 会阻止新旧 Turn 并行执行；提高优先级只保证
    # 旧 Turn 完成协作取消后，重定向任务优先于普通排队消息被领取。
    followup.priority = max(1, int(source.priority or 0) + 1)
    cancelled_guidance = db.query(JobGuidance).filter(
        JobGuidance.job_id == source.id,
        JobGuidance.owner_id == owner_id,
        JobGuidance.status.in_((GUIDANCE_PENDING, GUIDANCE_CLAIMED)),
    ).update(
        {JobGuidance.status: GUIDANCE_CANCELLED},
        synchronize_session=False,
    )
    _append_event_in_session(db, source.id, "interaction.interrupt.received", {
        "mode": "redirect",
        "successor_turn_id": followup.id,
        "chars": len(text),
        "cancelled_guidance": int(cancelled_guidance or 0),
    })
    _append_event_in_session(db, source.id, "interaction.redirect.queued", {
        "mode": "redirect",
        "successor_turn_id": followup.id,
    })
    _append_event_in_session(db, followup.id, "interaction.redirect.created", {
        "mode": "redirect",
        "interrupted_turn_id": source.id,
    })
    return followup


async def redirect_job(job_id: str, owner_id: int, content: str) -> dict:
    """Interrupt an open Turn and atomically queue a same-thread replacement.

    The old Turn remains immutable audit history.  The replacement gets a fresh
    execution attempt and explicit continuation/control metadata; current tools
    are cancelled through the existing bounded parent-child cancellation path.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("重定向消息不能为空")
    if len(text) > 4000:
        raise ValueError("重定向消息最长 4000 字符")
    db = SessionLocal()
    try:
        source = db.get(Job, job_id)
        if (
            source is None
            or source.owner_id != owner_id
            or source.kind != "chat"
        ):
            raise LookupError("任务不存在")
        followup = _queue_redirect_in_session(db, source, owner_id, text)
        db.commit()
        successor = _view(followup)
    finally:
        db.close()

    # This also cancels active descendants.  A local worker is interrupted
    # immediately; a separate worker observes the durable flag at its next safe
    # heartbeat/checkpoint, without pretending that a blocking external side
    # effect was rolled back.
    await request_cancel(job_id, owner_id, cascade=True)
    return {
        "interrupted_turn_id": job_id,
        "successor": successor,
    }


def pending_guidance(job_id: str, owner_id: Optional[int] = None) -> list[dict]:
    db = SessionLocal()
    try:
        query = db.query(JobGuidance).filter(
            JobGuidance.job_id == job_id,
            JobGuidance.status == GUIDANCE_PENDING,
        )
        if owner_id is not None:
            query = query.filter(JobGuidance.owner_id == owner_id)
        return [
            {"id": row.id, "job_id": row.job_id, "content": row.content, "status": row.status}
            for row in query.order_by(JobGuidance.created_at, JobGuidance.id).all()
        ]
    finally:
        db.close()


def update_guidance(
    guidance_id: str,
    owner_id: int,
    content: str,
    *,
    job_id: str | None = None,
) -> dict:
    text = (content or "").strip()
    if not text:
        raise ValueError("引导消息不能为空")
    if len(text) > 4000:
        raise ValueError("引导消息最长 4000 字符")
    db = SessionLocal()
    try:
        existing = db.get(JobGuidance, guidance_id)
        if existing is None or existing.owner_id != owner_id or (
            job_id is not None and existing.job_id != job_id
        ):
            raise LookupError("引导消息不存在")
        open_job = db.query(Job).filter(
            Job.id == existing.job_id,
            Job.owner_id == owner_id,
            Job.status.in_((PENDING, RUNNING)),
        ).update({Job.updated_at: Job.updated_at}, synchronize_session=False)
        if not open_job:
            raise RuntimeError("当前任务已不能接收引导")
        query = db.query(JobGuidance).filter(
            JobGuidance.id == guidance_id,
            JobGuidance.owner_id == owner_id,
            JobGuidance.status == GUIDANCE_PENDING,
        )
        if job_id is not None:
            query = query.filter(JobGuidance.job_id == job_id)
        updated = query.update(
            {JobGuidance.content: text},
            synchronize_session=False,
        )
        if not updated:
            row = db.get(JobGuidance, guidance_id)
            if row is None or row.owner_id != owner_id or (
                job_id is not None and row.job_id != job_id
            ):
                raise LookupError("引导消息不存在")
            raise RuntimeError("引导已被任务接收，无法编辑")
        db.commit()
        row = db.get(JobGuidance, guidance_id)
        if row is None:
            raise LookupError("引导消息不存在")
        return {"id": row.id, "job_id": row.job_id, "content": text, "status": row.status}
    finally:
        db.close()


def cancel_guidance(
    guidance_id: str,
    owner_id: int,
    *,
    job_id: str | None = None,
) -> bool:
    db = SessionLocal()
    try:
        existing = db.get(JobGuidance, guidance_id)
        if existing is None or existing.owner_id != owner_id or (
            job_id is not None and existing.job_id != job_id
        ):
            return False
        open_job = db.query(Job).filter(
            Job.id == existing.job_id,
            Job.owner_id == owner_id,
            Job.status.in_((PENDING, RUNNING)),
        ).update({Job.updated_at: Job.updated_at}, synchronize_session=False)
        if not open_job:
            return False
        query = db.query(JobGuidance).filter(
            JobGuidance.id == guidance_id,
            JobGuidance.owner_id == owner_id,
            JobGuidance.status == GUIDANCE_PENDING,
        )
        if job_id is not None:
            query = query.filter(JobGuidance.job_id == job_id)
        row = query.first()
        if row is None:
            return False
        updated = query.update(
            {JobGuidance.status: GUIDANCE_CANCELLED},
            synchronize_session=False,
        )
        if not updated:
            db.rollback()
            return False
        _append_event_in_session(db, row.job_id, "guidance.cancelled", {
            "guidance_id": row.id,
        })
        db.commit()
        return True
    finally:
        db.close()


def take_guidance(job_id: str, worker_id: str, lease_token: str) -> list[dict]:
    """worker 在编辑宽限期后、模型调用之间租领引导。

    宽限期让前端刚提交的引导能可靠完成「更多 → 编辑/排队/移除」操作，避免
    Worker 检查点在一次点击过程中把消息抢走。成功终态前仍不会永久确认。
    """
    db = SessionLocal()
    try:
        active_lease = db.query(Job).filter(
            Job.id == job_id,
            Job.status == RUNNING,
            Job.worker_id == worker_id,
            Job.lease_token == lease_token,
        ).update({Job.updated_at: Job.updated_at}, synchronize_session=False)
        if not active_lease:
            return []
        grace_seconds = max(0.0, float(settings.JOB_GUIDANCE_GRACE_SECONDS))
        eligible_before = _now() - datetime.timedelta(seconds=grace_seconds)
        rows = db.query(JobGuidance).filter(
            JobGuidance.job_id == job_id,
            JobGuidance.status == GUIDANCE_PENDING,
            JobGuidance.created_at <= eligible_before,
        ).order_by(JobGuidance.created_at, JobGuidance.id).all()
        values = []
        for row in rows:
            claimed = db.query(JobGuidance).filter(
                JobGuidance.id == row.id,
                JobGuidance.status == GUIDANCE_PENDING,
            ).update(
                {JobGuidance.status: GUIDANCE_CLAIMED},
                synchronize_session=False,
            )
            if not claimed:
                continue
            values.append({"id": row.id, "content": row.content})
            _append_event_in_session(db, job_id, "guidance.claimed", {
                "guidance_id": row.id,
                "chars": len(row.content),
            })
        if values:
            db.commit()
        return values
    finally:
        db.close()


def update_queued_message(job_id: str, owner_id: int, content: str) -> dict:
    """原地编辑 pending 消息，只替换 query，保留附件与能力快照。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("排队消息不能为空")
    if len(text) > 4000:
        raise ValueError("排队消息最长 4000 字符")
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.owner_id != owner_id or job.kind != "chat":
            raise LookupError("排队消息不存在")
        payload = _payload_with_query(job, text)
        updated = db.query(Job).filter(
            Job.id == job_id,
            Job.owner_id == owner_id,
            Job.kind == "chat",
            Job.status == PENDING,
        ).update(
            {Job.payload: json.dumps(payload, ensure_ascii=False)},
            synchronize_session=False,
        )
        if not updated:
            raise RuntimeError("消息已开始执行，无法编辑")
        turn = db.get(Turn, job_id)
        if turn is not None:
            turn.input = text
            turn.execution_snapshot = json.dumps(payload, ensure_ascii=False)
            message = db.query(Item).filter(
                Item.turn_id == turn.id,
                Item.kind == "message",
                Item.role == "user",
            ).order_by(Item.sequence).first()
            if message is not None:
                message.content = text
        db.commit()
        return {"id": job_id, "job_id": job_id, "content": text, "kind": "queue"}
    finally:
        db.close()


def move_queued_message_to_new(job_id: str, owner_id: int) -> dict:
    """原地移动 pending 消息到新线程，完整保留原任务快照。"""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.owner_id != owner_id or job.kind != "chat":
            raise LookupError("排队消息不存在")
        session_id = uuid.uuid4().hex
        try:
            current_payload = json.loads(job.payload or "{}")
        except json.JSONDecodeError:
            current_payload = {}
        payload = _payload_with_query(
            job,
            str((current_payload.get("inputs") or {}).get("query") or ""),
            session_id=session_id,
            project_id=None,
        )
        updated = db.query(Job).filter(
            Job.id == job_id,
            Job.owner_id == owner_id,
            Job.kind == "chat",
            Job.status == PENDING,
        ).update({
            Job.payload: json.dumps(payload, ensure_ascii=False),
            Job.session_key: f"{owner_id}:{session_id}",
        }, synchronize_session=False)
        if not updated:
            raise RuntimeError("消息已开始执行，无法移动")
        turn = db.get(Turn, job_id)
        if turn is not None:
            thread = task_store.ensure_thread(
                db,
                thread_id=session_id,
                owner_id=owner_id,
                agent_id=turn.agent_id,
                project_id=None,
            )
            turn.thread_id = thread.id
            turn.execution_snapshot = json.dumps(payload, ensure_ascii=False)
            db.query(Item).filter(Item.turn_id == turn.id).update(
                {Item.thread_id: thread.id}, synchronize_session=False
            )
            db.query(Attachment).filter(Attachment.turn_id == turn.id).update(
                {Attachment.thread_id: thread.id}, synchronize_session=False
            )
        db.commit()
        return {"job_id": job_id, "session_id": session_id, "kind": "queue"}
    finally:
        db.close()


def convert_guidance_to_queue(
    guidance_id: str,
    owner_id: int,
    *,
    job_id: str,
    new_conversation: bool = False,
) -> dict:
    """在单一事务内把 pending 引导转换为排队任务。"""
    db = SessionLocal()
    try:
        guidance = db.query(JobGuidance).filter(
            JobGuidance.id == guidance_id,
            JobGuidance.job_id == job_id,
            JobGuidance.owner_id == owner_id,
            JobGuidance.status == GUIDANCE_PENDING,
        ).first()
        source = db.get(Job, job_id)
        if guidance is None or source is None or source.owner_id != owner_id:
            raise LookupError("引导消息不存在")
        open_job = db.query(Job).filter(
            Job.id == job_id,
            Job.owner_id == owner_id,
            Job.status.in_((PENDING, RUNNING)),
        ).update({Job.updated_at: Job.updated_at}, synchronize_session=False)
        if not open_job:
            raise RuntimeError("当前任务已不能转换引导")
        session_id = uuid.uuid4().hex if new_conversation else None
        followup = _create_followup_in_session(
            db,
            source,
            guidance.content,
            session_id=session_id,
            project_id=None,
        )
        updated = db.query(JobGuidance).filter(
            JobGuidance.id == guidance_id,
            JobGuidance.status == GUIDANCE_PENDING,
        ).update(
            {JobGuidance.status: GUIDANCE_QUEUED, JobGuidance.applied_at: _now()},
            synchronize_session=False,
        )
        if not updated:
            db.rollback()
            raise RuntimeError("引导已被任务接收，无法转换")
        db.commit()
        payload = _view(followup).payload
        return {
            "job_id": followup.id,
            "session_id": payload.get("session_id"),
            "kind": "queue",
        }
    finally:
        db.close()


def _queued_plain_text(job: Job) -> tuple[str, dict]:
    """Return a queued Turn's text only when no structured context would be lost."""
    try:
        payload = json.loads(job.payload or "{}")
    except json.JSONDecodeError:
        payload = {}
    input_values = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    structured = any(payload.get(key) for key in (
        "attachment_images", "attachment_docs", "dataset_ids", "template_ids",
        "skill_ids", "mcp_ids", "invoked_agent_ids",
    )) or any(value for key, value in input_values.items() if key != "query")
    if structured:
        raise RuntimeError("该消息包含附件或能力上下文，只能保留在队列中")
    content = str(input_values.get("query") or "").strip()
    if not content:
        raise ValueError("排队消息不能为空")
    return content, payload


def convert_queued_message_to_guidance(
    job_id: str,
    target_job_id: str,
    owner_id: int,
) -> dict:
    """原子取消 pending 消息并把其纯文本内容转为当前任务引导。"""
    if job_id == target_job_id:
        raise ValueError("不能把当前任务转换为自身引导")
    db = SessionLocal()
    try:
        source = db.get(Job, job_id)
        target = db.get(Job, target_job_id)
        if source is None or source.owner_id != owner_id or source.kind != "chat":
            raise LookupError("排队消息不存在")
        if target is None or target.owner_id != owner_id or target.kind != "chat":
            raise LookupError("目标任务不存在")
        try:
            content, _ = _queued_plain_text(source)
        except RuntimeError as exc:
            raise RuntimeError("该消息包含附件或能力上下文，不能改为纯文本引导") from exc
        # 对目标行做条件空更新以建立与终态转换之间的事务顺序。
        target_open = db.query(Job).filter(
            Job.id == target_job_id,
            Job.owner_id == owner_id,
            Job.status.in_((PENDING, RUNNING)),
        ).update({Job.updated_at: Job.updated_at}, synchronize_session=False)
        if not target_open:
            raise RuntimeError("当前任务已不能接收引导")
        cancelled = db.query(Job).filter(
            Job.id == job_id,
            Job.owner_id == owner_id,
            Job.status == PENDING,
        ).update({
            Job.status: CANCELLED,
            Job.cancel_requested: True,
            Job.updated_at: _now(),
        }, synchronize_session=False)
        if not cancelled:
            raise RuntimeError("消息已开始执行，无法转换")
        source_turn = db.get(Turn, job_id)
        if source_turn is not None:
            task_store.finish_turn(
                db, source_turn, answer="", status="cancelled"
            )
            task_store.append_runtime_item(db, source_turn.id, "turn.cancelled", {
                "reason": "converted_to_guidance",
                "target_turn_id": target_job_id,
            })
        guidance = JobGuidance(
            id=uuid.uuid4().hex,
            job_id=target_job_id,
            owner_id=owner_id,
            content=content,
            status=GUIDANCE_PENDING,
        )
        db.add(guidance)
        _append_event_in_session(db, target_job_id, "guidance.queued", {
            "guidance_id": guidance.id,
            "chars": len(content),
        })
        db.commit()
        return {
            "id": guidance.id,
            "job_id": target_job_id,
            "content": content,
            "status": GUIDANCE_PENDING,
            "kind": "guidance",
        }
    finally:
        db.close()


async def redirect_staged_message(
    kind: str,
    item_id: str,
    target_job_id: str,
    owner_id: int,
) -> dict:
    """Promote a pending guidance/queue item to the current goal atomically.

    Guidance is consumed by the redirect transaction.  A queued Turn becomes a
    preserved cancelled Turn before the successor is created, so refreshes and
    concurrent workers cannot duplicate or silently lose the staged message.
    """
    if kind not in {"guidance", "queue"}:
        raise ValueError("未知的暂存消息类型")
    if not target_job_id:
        raise ValueError("缺少当前任务")
    db = SessionLocal()
    try:
        target = db.get(Job, target_job_id)
        if target is None or target.owner_id != owner_id or target.kind != "chat":
            raise LookupError("当前任务不存在")

        if kind == "guidance":
            guidance = db.query(JobGuidance).filter(
                JobGuidance.id == item_id,
                JobGuidance.job_id == target_job_id,
                JobGuidance.owner_id == owner_id,
                JobGuidance.status == GUIDANCE_PENDING,
            ).first()
            if guidance is None:
                raise LookupError("引导消息不存在或已被任务接收")
            content = guidance.content
        else:
            if item_id == target_job_id:
                raise ValueError("不能把当前任务转换为自身目标")
            queued = db.get(Job, item_id)
            if queued is None or queued.owner_id != owner_id or queued.kind != "chat":
                raise LookupError("排队消息不存在")
            content, queued_payload = _queued_plain_text(queued)
            try:
                target_payload = json.loads(target.payload or "{}")
            except json.JSONDecodeError:
                target_payload = {}
            queued_session_id = str(queued_payload.get("session_id") or "").strip()
            target_session_id = str(target_payload.get("session_id") or "").strip()
            if not queued_session_id or queued_session_id != target_session_id:
                raise LookupError("排队消息不属于当前对话")
            cancelled = db.query(Job).filter(
                Job.id == item_id,
                Job.owner_id == owner_id,
                Job.kind == "chat",
                Job.status == PENDING,
            ).update({
                Job.status: CANCELLED,
                Job.cancel_requested: True,
                Job.updated_at: _now(),
            }, synchronize_session=False)
            if not cancelled:
                raise RuntimeError("消息已开始执行，无法设为当前目标")
            queued_turn = db.get(Turn, item_id)
            if queued_turn is not None:
                task_store.finish_turn(db, queued_turn, answer="", status="cancelled")
                task_store.append_runtime_item(
                    db,
                    queued_turn.id,
                    "turn.cancelled",
                    {
                        "reason": "converted_to_redirect",
                        "target_turn_id": target_job_id,
                    },
                )

        text = (content or "").strip()
        if not text:
            raise ValueError("重定向消息不能为空")
        if len(text) > 4000:
            raise ValueError("重定向消息最长 4000 字符")
        followup = _queue_redirect_in_session(db, target, owner_id, text)
        db.commit()
        successor = _view(followup)
    finally:
        db.close()

    await request_cancel(target_job_id, owner_id, cascade=True)
    return {
        "interrupted_turn_id": target_job_id,
        "source_kind": kind,
        "source_id": item_id,
        "successor": successor,
    }


async def request_cancel(
    job_id: str,
    owner_id: Optional[int],
    *,
    cascade: bool = True,
) -> bool:
    """取消任务；默认有界级联全部未终态后代，避免父任务停止后子任务继续执行。"""
    db = SessionLocal()
    cancelled_ids: list[str] = []
    try:
        root = db.get(Job, job_id)
        if root is None or (owner_id is not None and root.owner_id != owner_id):
            return False
        ordered = [root]
        if cascade:
            seen = {root.id}
            cursor = 0
            limit = max(1, int(settings.JOB_CANCEL_CASCADE_LIMIT))
            while cursor < len(ordered) and len(ordered) < limit:
                parent = ordered[cursor]
                cursor += 1
                children = db.query(Job).filter(
                    Job.parent_job_id == parent.id,
                    Job.owner_id == root.owner_id,
                ).order_by(Job.created_at, Job.id).all()
                for child in children:
                    if child.id in seen:
                        continue
                    seen.add(child.id)
                    ordered.append(child)
                    if len(ordered) >= limit:
                        break
        for target in ordered:
            if target.status in _TERMINAL:
                continue
            target.cancel_requested = True
            cancelled_ids.append(target.id)
            reason = "user_requested" if target.id == root.id else "parent_cancelled"
            if target.status in (PENDING, AWAITING_APPROVAL):
                target.status = CANCELLED
                target.worker_id = ""
                target.lease_token = ""
                target.heartbeat_at = None
                target.next_attempt_at = None
                turn = db.get(Turn, target.id)
                if turn is not None:
                    task_store.finish_turn(db, turn, answer="", status="cancelled")
                    task_store.append_runtime_item(
                        db, turn.id, "turn.cancelled", {"reason": reason}
                    )
                    task_store.append_runtime_item(
                        db, turn.id, "task.cancelled", {"status": "cancelled", "reason": reason}
                    )
                _promote_guidance_in_session(
                    db,
                    target,
                    (GUIDANCE_PENDING, GUIDANCE_CLAIMED),
                )
        db.commit()
    finally:
        db.close()
    for target_id in cancelled_ids:
        task = _local_tasks.get(target_id)
        if task is not None and not task.done():
            task.cancel()
    return True


def is_cancel_requested(job_id: str) -> bool:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        return bool(job and job.cancel_requested)
    finally:
        db.close()


# ---------- 领取 / 状态流转（worker 侧，均为短会话） ----------

def claim_next(worker_id: str) -> Optional[JobView]:
    """原子领取最早的 pending 任务（pending→running）。

    用「带 status 条件的 UPDATE」实现：rowcount==1 表示本 worker 抢到。该写法在 SQLite
    （写串行）与 Postgres（行级原子）下均正确；多 worker 抢同一行时仅一个成功，其余返回 None
    由调用方重试。
    """
    db = SessionLocal()
    try:
        now = _now()
        active = aliased(Job)
        owner_active = aliased(Job)
        owner_running = (
            select(func.count(owner_active.id))
            .where(
                owner_active.owner_id == Job.owner_id,
                owner_active.status.in_((RUNNING, AWAITING_APPROVAL)),
            )
            .correlate(Job)
            .scalar_subquery()
        )
        per_user_limit = max(1, int(settings.JOB_MAX_RUNNING_PER_USER))
        row = (
            db.query(Job.id)
            .filter(
                Job.status == PENDING,
                or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
                or_(Job.owner_id.is_(None), owner_running < per_user_limit),
                or_(
                    Job.session_key == "",
                    ~exists().where(
                        active.session_key == Job.session_key,
                        active.id != Job.id,
                        active.status.in_((RUNNING, AWAITING_APPROVAL)),
                    ),
                ),
            )
            # 优先服务当前占用运行槽更少的用户，避免单一用户填满全部 Worker。
            .order_by(Job.priority.desc(), owner_running, Job.created_at)
            .first()
        )
        if row is None:
            return None
        job_id = row[0]
        lease_token = uuid.uuid4().hex
        updated = (
            db.query(Job)
            .filter(Job.id == job_id, Job.status == PENDING)
            .update(
                {
                    Job.status: RUNNING,
                    Job.worker_id: worker_id,
                    Job.lease_token: lease_token,
                    Job.attempt_count: Job.attempt_count + 1,
                    Job.heartbeat_at: now,
                    Job.next_attempt_at: None,
                    Job.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated:
            turn = db.get(Turn, job_id)
            if turn is not None:
                turn.status = "running"
                turn.started_at = now
                task_store.append_runtime_item(
                    db, turn.id, "turn.claimed", {"worker_id": worker_id}
                )
                task_store.append_runtime_item(
                    db, turn.id, "task.started", {"status": "planning"}
                )
        db.commit()
        if not updated:
            return None  # 抢占失败，调用方下一轮重试
        return _view(db.get(Job, job_id))
    finally:
        db.close()


def touch_worker(worker_id: str, capacity: int) -> None:
    db = SessionLocal()
    try:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is None:
            row = WorkerHeartbeat(worker_id=worker_id)
            db.add(row)
        row.capacity = max(1, int(capacity))
        row.last_seen = _now()
        db.commit()
    finally:
        db.close()


def remove_worker(worker_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is not None:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def cleanup_worker_heartbeats(ttl_seconds: int) -> int:
    """回收异常退出遗留的 Worker 心跳行，避免运维状态长期积累噪声。"""
    cutoff = _now() - datetime.timedelta(seconds=max(60, int(ttl_seconds)))
    db = SessionLocal()
    try:
        deleted = db.query(WorkerHeartbeat).filter(
            WorkerHeartbeat.last_seen < cutoff
        ).delete(synchronize_session=False)
        if deleted:
            db.commit()
        return int(deleted or 0)
    finally:
        db.close()


def set_progress(
    job_id: str,
    worker_id: str,
    lease_token: str,
    text: str,
    *,
    include_event: bool = False,
) -> bool | tuple[bool, dict]:
    """更新进度文本、续租心跳，并在同一连接内返回是否已被请求取消。

    进度回调在每个流程阶段都会触发；DB 若位于网络共享（SMB）上，单次连接的握手/锁
    开销很高。合并「写进度 + 读取消标志」为一次往返，把每阶段的 DB 连接数从 2 降到 1。
    返回 True 表示该任务已被请求取消，调用方据此中止。
    """
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if (
            job is None
            or job.status != RUNNING
            or job.worker_id != worker_id
            or job.lease_token != lease_token
        ):
            return (False, {}) if include_event else False
        job.progress = (text or "")[:256]
        job.heartbeat_at = _now()
        item = _append_event_in_session(
            db, job_id, "turn.progress", {"text": job.progress}
        )
        cancel = bool(job.cancel_requested)
        db.flush()
        event_meta = _event_meta(item)
        db.commit()
        return (cancel, event_meta) if include_event else cancel
    finally:
        db.close()


def append_event(
    job_id: str,
    worker_id: str,
    lease_token: str,
    event_type: str,
    payload: dict | None = None,
) -> dict:
    """追加结构化 Item，供评估、故障分析与回放使用。"""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if (
            job is None
            or job.status != RUNNING
            or job.worker_id != worker_id
            or job.lease_token != lease_token
        ):
            return {}
        item = _append_event_in_session(db, job_id, event_type, payload)
        db.flush()
        event_meta = _event_meta(item)
        db.commit()
        return event_meta
    finally:
        db.close()


def heartbeat(job_id: str, worker_id: str, lease_token: str) -> bool:
    """续租心跳。返回 False 表示已被请求取消（worker 据此中止）或任务已不属于本 worker。"""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if (
            job is None
            or job.status != RUNNING
            or job.worker_id != worker_id
            or job.lease_token != lease_token
        ):
            return False
        if job.cancel_requested:
            return False
        job.heartbeat_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def wait_for_approval(
    job_id: str, worker_id: str, lease_token: str,
    scope: str, description: str,
    *,
    approval_agent_id: int | None = None,
    execution_context: dict | None = None,
) -> bool:
    """仅当前租约持有者可把任务暂停为待用户批准。"""
    event_meta = {}
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if (
            job is None
            or job.status != RUNNING
            or job.worker_id != worker_id
            or job.lease_token != lease_token
        ):
            return False
        payload = json.loads(job.payload or "{}")
        payload["_approval_scope"] = scope
        payload["_approval_description"] = description
        payload["_approval_agent_id"] = approval_agent_id
        payload["_approval_execution_context"] = dict(execution_context or {})
        job.payload = json.dumps(payload, ensure_ascii=False)
        job.status = AWAITING_APPROVAL
        turn = db.get(Turn, job_id)
        if turn is not None:
            turn.status = "awaiting_approval"
            item = task_store.append_runtime_item(db, turn.id, "approval.requested", {
                "scope": scope,
                "description": description,
                "agent_id": approval_agent_id or job.agent_id,
                **dict(execution_context or {}),
            })
            task_store.append_runtime_item(db, turn.id, "task.status", {
                "status": "waiting_approval",
            })
            db.flush()
            event_meta = _event_meta(item)
        job.progress = f"等待用户批准：{scope}"[:256]
        job.worker_id = ""
        job.lease_token = ""
        job.heartbeat_at = None
        job.updated_at = _now()
        db.query(JobGuidance).filter(
            JobGuidance.job_id == job_id,
            JobGuidance.status == GUIDANCE_CLAIMED,
        ).update(
            {JobGuidance.status: GUIDANCE_PENDING, JobGuidance.applied_at: None},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()
    _publish(job_id, {
        "type": "approval", "scope": scope, "description": description,
        **event_meta,
    })
    return True


def approve_waiting(job_id: str, owner_id: int) -> bool:
    """签发一次性批准并用条件更新把等待任务重新入队。"""
    from .approvals import issue

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if (
            job is None
            or job.owner_id != owner_id
            or job.status != AWAITING_APPROVAL
        ):
            return False
        payload = json.loads(job.payload or "{}")
        scope = str(payload.get("_approval_scope") or "")
        if not scope:
            return False
        approval_agent_id = payload.get("_approval_agent_id")
        if approval_agent_id is None:
            approval_agent_id = job.agent_id
        token = issue(job.id, owner_id, approval_agent_id, scope)
        tokens = payload.get("approval_tokens")
        if not isinstance(tokens, list):
            tokens = []
        tokens.append(token)
        payload["approval_tokens"] = tokens[-20:]
        payload.pop("_approval_scope", None)
        payload.pop("_approval_description", None)
        execution_context = payload.pop("_approval_execution_context", {})
        payload.pop("_approval_agent_id", None)
        job.payload = json.dumps(payload, ensure_ascii=False)
        job.status = PENDING
        turn = db.get(Turn, job_id)
        if turn is not None:
            turn.status = "queued"
            task_store.append_runtime_item(db, turn.id, "approval.granted", {
                "scope": scope,
                "agent_id": approval_agent_id,
                **(execution_context if isinstance(execution_context, dict) else {}),
            })
            task_store.append_runtime_item(db, turn.id, "task.queued", {
                "status": "queued",
                "reason": "approval_granted",
            })
        job.progress = "用户已批准，等待重新执行"
        job.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def finish(job_id: str, worker_id: str, lease_token: str, result: dict) -> bool:
    return _terminal(
        job_id, DONE, worker_id=worker_id, lease_token=lease_token, result=result
    )


def fail(
    job_id: str,
    worker_id: str,
    lease_token: str,
    error: str,
    *,
    error_class: str = "persistent",
) -> bool:
    return _terminal(
        job_id, FAILED, worker_id=worker_id, lease_token=lease_token,
        error=error, error_class=error_class,
    )


def retry_or_dead_letter(
    job_id: str,
    worker_id: str,
    lease_token: str,
    error: str,
    *,
    error_class: str = "transient",
) -> str:
    """对明确瞬时错误执行有界退避；达到上限后进入死信。"""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if (
            job is None
            or job.status != RUNNING
            or job.worker_id != worker_id
            or job.lease_token != lease_token
        ):
            return "unchanged"
        attempts = int(job.attempt_count or 0)
        maximum = int(job.max_attempts or 3)
        if attempts >= maximum:
            pass
        else:
            delay = min(
                max(0.1, float(settings.JOB_RETRY_MAX_SECONDS)),
                max(0.1, float(settings.JOB_RETRY_BASE_SECONDS)) * (2 ** max(0, attempts - 1)),
            )
            retry_at = _now() + datetime.timedelta(seconds=delay)
            job.status = PENDING
            job.progress = f"瞬时失败，{delay:g} 秒后重试"[:256]
            job.error = error[:2000]
            job.error_class = error_class[:32]
            job.next_attempt_at = retry_at
            job.worker_id = ""
            job.lease_token = ""
            job.heartbeat_at = None
            job.updated_at = _now()
            turn = db.get(Turn, job_id)
            if turn is not None:
                turn.status = "queued"
                task_store.append_runtime_item(db, turn.id, "turn.requeued", {
                    "attempt_count": attempts,
                    "reason": error_class,
                    "retry_at": retry_at.isoformat(),
                })
                task_store.append_runtime_item(db, turn.id, "task.queued", {
                    "status": "queued",
                    "reason": "transient_retry",
                })
            db.query(JobGuidance).filter(
                JobGuidance.job_id == job_id,
                JobGuidance.status == GUIDANCE_CLAIMED,
            ).update({
                JobGuidance.status: GUIDANCE_PENDING,
                JobGuidance.applied_at: None,
            }, synchronize_session=False)
            db.commit()
            return PENDING
    finally:
        db.close()
    changed = _terminal(
        job_id,
        DEAD_LETTER,
        worker_id=worker_id,
        lease_token=lease_token,
        error=f"任务达到最大重试次数：{error}",
        error_class=error_class,
    )
    return DEAD_LETTER if changed else "unchanged"


def mark_cancelled(job_id: str, worker_id: str, lease_token: str) -> bool:
    return _terminal(
        job_id, CANCELLED, worker_id=worker_id, lease_token=lease_token
    )


def _terminal(
    job_id: str,
    status: str,
    *,
    worker_id: str,
    lease_token: str,
    result: Optional[dict] = None,
    error: str = "",
    error_class: str = "",
) -> bool:
    changed = False
    terminal_event_meta = {}
    terminal_result = result

    def nonnegative_int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    result_value = result if isinstance(result, dict) else {}
    completion_status = (
        "completed_with_issues"
        if status == DONE
        and result_value.get("completion_status") == "completed_with_issues"
        else "completed"
    )
    raw_completion_issues = result_value.get("completion_issues")
    raw_completion_issues = (
        raw_completion_issues
        if isinstance(raw_completion_issues, (list, tuple)) else []
    )
    completion_issues = [
        str(value)[:400]
        for value in raw_completion_issues[:20]
        if str(value).strip()
    ]
    raw_plan_summary = result_value.get("plan_summary")
    raw_plan_summary = raw_plan_summary if isinstance(raw_plan_summary, dict) else {}
    plan_summary = {
        key: nonnegative_int(raw_plan_summary.get(key))
        for key in (
            "total", "completed", "failed", "blocked", "skipped",
            "pending", "in_progress",
        )
    }
    plan_summary.update({
        "terminalized": bool(raw_plan_summary.get("terminalized")),
        "all_completed": bool(raw_plan_summary.get("all_completed")),
    })
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if (
            job is not None
            and job.status == RUNNING
            and job.worker_id == worker_id
            and job.lease_token == lease_token
        ):
            job.status = status
            job.partial_result = ""
            job.progress = {
                DONE: (
                    "已完成（有未完成项）"
                    if completion_status == "completed_with_issues"
                    else "已完成"
                ),
                FAILED: "执行失败",
                CANCELLED: "已取消",
                DEAD_LETTER: "执行失败",
            }.get(status, status)[:256]
            if result is not None:
                safe_result = dict(result)
                for private_key in ("reasoning", "reasoning_content", "thinking"):
                    safe_result.pop(private_key, None)
                if status == DONE:
                    safe_result.update({
                        "completion_status": completion_status,
                        "completion_issues": completion_issues,
                        "plan_summary": plan_summary,
                    })
                job.result = json.dumps(safe_result, ensure_ascii=False)
                terminal_result = safe_result
            if error:
                job.error = error[:2000]
            if error_class:
                job.error_class = error_class[:32]
            job.worker_id = ""
            job.lease_token = ""
            job.heartbeat_at = None
            job.next_attempt_at = None
            job.updated_at = _now()
            if status == DONE:
                db.query(JobGuidance).filter(
                    JobGuidance.job_id == job_id,
                    JobGuidance.status == GUIDANCE_CLAIMED,
                ).update({
                    JobGuidance.status: GUIDANCE_APPLIED,
                    JobGuidance.applied_at: _now(),
                }, synchronize_session=False)
                _promote_guidance_in_session(
                    db,
                    job,
                    (GUIDANCE_PENDING,),
                )
            else:
                _promote_guidance_in_session(
                    db,
                    job,
                    (GUIDANCE_PENDING, GUIDANCE_CLAIMED),
                )
            turn = db.get(Turn, job_id)
            if turn is not None:
                answer = str((result or {}).get("answer") or "")
                task_store.finish_turn(
                    db,
                    turn,
                    answer=answer,
                    status={
                        DONE: completion_status,
                        FAILED: "failed",
                        CANCELLED: "cancelled",
                        DEAD_LETTER: "failed",
                    }.get(status, status),
                    error=error,
                )
                _append_event_in_session(
                    db,
                    job_id,
                    f"turn.{turn.status}",
                    {"error": error[:2000]} if error else {
                        "has_result": result is not None
                    },
                )
                task_event = {
                    DONE: (
                        "task.completed_with_issues"
                        if completion_status == "completed_with_issues"
                        else "task.completed"
                    ),
                    FAILED: "task.failed",
                    CANCELLED: "task.cancelled",
                    DEAD_LETTER: "task.failed",
                }.get(status, "task.failed")
                task_item = _append_event_in_session(
                    db,
                    job_id,
                    task_event,
                    {
                        "status": {
                            DONE: completion_status,
                            FAILED: "failed",
                            CANCELLED: "cancelled",
                            DEAD_LETTER: "failed",
                        }.get(status, "failed"),
                        **({
                            "completion_status": completion_status,
                            "completion_issues": completion_issues,
                            "plan_summary": plan_summary,
                        } if status == DONE else {}),
                        **({"error": error[:2000]} if error else {}),
                    },
                )
                db.flush()
                terminal_event_meta = _event_meta(task_item)
            db.commit()
            changed = True
    finally:
        db.close()
    # 通知流式订阅者收尾（携带终态与结果，供前端用清洗后的最终答案整体替换流式文本）
    if changed:
        _publish(job_id, {
            "type": "end", "status": status, "result": terminal_result, "error": error,
            **terminal_event_meta,
        })
    return changed


# ---------- 维护：陈旧任务重入队 + 过期清理 ----------

def requeue_stale(lease_seconds: int) -> int:
    """心跳超时（worker 崩溃/重启）的 running 任务：被取消的判为 cancelled，否则重新入队为 pending。

    启动时与周期性调用，实现「任务跨重启不丢」。返回处理的任务数。
    """
    import datetime
    cutoff = _now() - datetime.timedelta(seconds=lease_seconds)
    db = SessionLocal()
    try:
        n = 0
        stale = db.query(Job).filter(Job.status == RUNNING).all()
        for job in stale:
            hb = job.heartbeat_at
            # SQLite 取回的 datetime 可能为 naive，按 UTC 处理以便比较
            if hb is not None and hb.tzinfo is None:
                hb = hb.replace(tzinfo=datetime.timezone.utc)
            if hb is not None and hb >= cutoff:
                continue
            if job.cancel_requested:
                job.status = CANCELLED
                turn = db.get(Turn, job.id)
                if turn is not None:
                    task_store.finish_turn(db, turn, answer="", status="cancelled")
                    task_store.append_runtime_item(db, turn.id, "turn.cancelled", {
                        "reason": "stale_worker",
                    })
                    task_store.append_runtime_item(db, turn.id, "task.cancelled", {
                        "status": "cancelled", "reason": "stale_worker",
                    })
                _promote_guidance_in_session(
                    db,
                    job,
                    (GUIDANCE_PENDING, GUIDANCE_CLAIMED),
                )
            elif int(job.attempt_count or 0) >= int(job.max_attempts or 3):
                job.status = DEAD_LETTER
                job.error = "任务超过最大重试次数，已进入死信状态"
                job.error_class = "stale_worker"
                turn = db.get(Turn, job.id)
                if turn is not None:
                    task_store.finish_turn(
                        db, turn, answer="", status="failed", error=job.error
                    )
                    task_store.append_runtime_item(db, turn.id, "turn.failed", {
                        "reason": "max_attempts",
                    })
                    task_store.append_runtime_item(db, turn.id, "task.failed", {
                        "status": "failed", "reason": "max_attempts",
                        "error": job.error,
                    })
                _promote_guidance_in_session(
                    db,
                    job,
                    (GUIDANCE_PENDING, GUIDANCE_CLAIMED),
                )
            else:
                job.status = PENDING
                job.error_class = "stale_worker"
                job.next_attempt_at = None
                turn = db.get(Turn, job.id)
                if turn is not None:
                    turn.status = "queued"
                    task_store.append_runtime_item(db, turn.id, "turn.requeued", {
                        "attempt_count": int(job.attempt_count or 0),
                    })
                    task_store.append_runtime_item(db, turn.id, "task.queued", {
                        "status": "queued", "reason": "stale_worker_requeue",
                    })
                db.query(JobGuidance).filter(
                    JobGuidance.job_id == job.id,
                    JobGuidance.status == GUIDANCE_CLAIMED,
                ).update({
                    JobGuidance.status: GUIDANCE_PENDING,
                    JobGuidance.applied_at: None,
                }, synchronize_session=False)
            job.worker_id = ""
            job.lease_token = ""
            job.heartbeat_at = None
            if job.status in _TERMINAL:
                job.next_attempt_at = None
            job.updated_at = _now()
            n += 1
        if n:
            db.commit()
            logger.info("requeue_stale：处理 %s 个陈旧任务", n)
        return n
    finally:
        db.close()


def cleanup_old(ttl_seconds: int, max_keep: int = 500) -> int:
    """删除超过 TTL 的终态任务；并在总量超限时按更新时间淘汰最旧的终态任务。"""
    import datetime
    cutoff = _now() - datetime.timedelta(seconds=ttl_seconds)
    db = SessionLocal()
    try:
        deleted = 0
        old = db.query(Job).filter(Job.status.in_(tuple(_TERMINAL))).all()
        kept = []
        for job in old:
            ts = job.updated_at
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            if ts is not None and ts < cutoff:
                _delete_job_lifecycle(db, job)
                deleted += 1
            else:
                kept.append((ts, job))
        if len(kept) > max_keep:
            kept.sort(key=lambda x: (x[0] is not None, x[0]))
            for _, job in kept[: len(kept) - max_keep]:
                _delete_job_lifecycle(db, job)
                deleted += 1
        if deleted:
            db.commit()
        return deleted
    finally:
        db.close()


def _delete_job_lifecycle(db, job: Job) -> None:
    """清理队列传输记录；Thread / Turn / Item 作为任务历史继续保留。"""
    from .models import Artifact
    db.query(Artifact).filter(Artifact.run_id == job.id).update(
        {Artifact.run_id: None}, synchronize_session=False
    )
    db.delete(job)


def _upload_paths(value) -> set[str]:
    """从任务 JSON 中提取位于 UPLOAD_DIR 的文件绝对路径。"""
    from pathlib import Path

    found: set[str] = set()
    root = UPLOAD_DIR.resolve()

    def walk(node):
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, str):
            try:
                path = Path(node).resolve()
            except (OSError, ValueError):
                return
            if path.parent == root:
                found.add(str(path))
                found.add(str(path.with_suffix(".name")))

    walk(value)
    return found


def cleanup_uploads(ttl_seconds: int) -> int:
    """清理超过 TTL 且未被 pending/running 任务引用的上传件。"""
    if ttl_seconds <= 0:
        return 0
    protected: set[str] = set()
    db = SessionLocal()
    try:
        active = db.query(Job.payload).filter(Job.status.in_((PENDING, RUNNING))).all()
        for (payload_text,) in active:
            try:
                payload = json.loads(payload_text or "{}")
            except json.JSONDecodeError:
                continue
            protected.update(_upload_paths(payload))
        for (storage_name,) in db.query(Attachment.storage_name).all():
            path = (UPLOAD_DIR / Path(str(storage_name or "")).name).resolve()
            if path.parent == UPLOAD_DIR.resolve():
                protected.add(str(path))
                protected.add(str(path.with_suffix(".name")))
    finally:
        db.close()

    cutoff = time.time() - ttl_seconds
    deleted = 0
    for path in UPLOAD_DIR.iterdir():
        try:
            resolved = path.resolve()
            if (
                path.is_file()
                and resolved.parent == UPLOAD_DIR.resolve()
                and str(resolved) not in protected
                and path.stat().st_mtime < cutoff
            ):
                path.unlink()
                deleted += 1
        except OSError:
            logger.warning("清理上传件失败：%s", path, exc_info=True)
    return deleted
