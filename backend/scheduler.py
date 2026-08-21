"""持久化 Cron 调度器。

支持标准五字段 ``minute hour day month weekday``，字段可使用 ``*``、逗号、
范围和步长（如 ``*/15``、``1-5``）。调度命中后仅创建统一 chat Job。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import socket
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_

from . import jobs
from .config import settings
from .database import SessionLocal
from .models import Agent, Job, ScheduledTask, SchedulerHeartbeat, Thread
from .runtime.contracts import TaskInput

logger = logging.getLogger(__name__)

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _values(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Cron 字段包含空项")
        base, slash, step_text = part.partition("/")
        step = int(step_text) if slash else 1
        if step < 1:
            raise ValueError("Cron 步长必须大于 0")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            start, end = int(left), int(right)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"Cron 值超出范围 {minimum}-{maximum}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expression: str) -> tuple[set[int], ...]:
    fields = str(expression or "").split()
    if len(fields) != 5:
        raise ValueError("Cron 必须包含五个字段：minute hour day month weekday")
    parsed = tuple(_values(field, *bounds) for field, bounds in zip(fields, _RANGES))
    # 经典 Cron 同时接受 0 和 7 表示周日，内部统一为 0。
    weekday = {0 if value == 7 else value for value in parsed[4]}
    return (*parsed[:4], weekday)


def next_run(expression: str, timezone: str, after: dt.datetime | None = None) -> dt.datetime:
    parsed = parse_cron(expression)
    fields = str(expression or "").split()
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        # Windows Python 常未附带 IANA tzdata；为平台默认时区提供无依赖回退。
        fixed = {
            "UTC": dt.timezone.utc,
            "Etc/UTC": dt.timezone.utc,
            "Asia/Shanghai": dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai"),
        }
        zone = fixed.get(timezone)
        if zone is None:
            raise ValueError(
                f"未知时区：{timezone}；安装 tzdata 可启用完整 IANA 时区库"
            ) from exc
    base = after or dt.datetime.now(dt.timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=dt.timezone.utc)
    local_base = base.astimezone(zone)
    first_date = local_base.date()
    # 按“日期 → 小时 → 分钟”枚举允许值；稀疏表达式（如闰日）无需逐分钟扫描。
    for day_offset in range(366 * 5 + 1):
        current_date = first_date + dt.timedelta(days=day_offset)
        if current_date.month not in parsed[3]:
            continue
        cron_weekday = (current_date.weekday() + 1) % 7  # Python 周一=0；Cron 周日=0
        day_of_month_match = current_date.day in parsed[2]
        day_of_week_match = cron_weekday in parsed[4]
        # POSIX/Vixie Cron：当“日”和“星期”都不是 * 时，二者按 OR 命中；
        # 任一字段为 * 时，则由另一个受限字段决定。旧实现错误地始终按 AND。
        if fields[2] == "*":
            day_match = day_of_week_match
        elif fields[4] == "*":
            day_match = day_of_month_match
        else:
            day_match = day_of_month_match or day_of_week_match
        if not day_match:
            continue
        for hour in sorted(parsed[1]):
            for minute in sorted(parsed[0]):
                candidate = dt.datetime(
                    current_date.year, current_date.month, current_date.day,
                    hour, minute, tzinfo=zone,
                )
                if candidate <= local_base:
                    continue
                candidate_utc = candidate.astimezone(dt.timezone.utc)
                # 跳过夏令时切换产生的不存在本地时间。
                roundtrip = candidate_utc.astimezone(zone)
                if (
                    roundtrip.year, roundtrip.month, roundtrip.day,
                    roundtrip.hour, roundtrip.minute,
                ) != (
                    candidate.year, candidate.month, candidate.day,
                    candidate.hour, candidate.minute,
                ):
                    continue
                return candidate_utc.replace(tzinfo=None)
    raise ValueError("未来 5 年内没有符合 Cron 的时间")


def _iso_utc(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _task_dict(task: ScheduledTask, db=None) -> dict:
    last_job = db.get(Job, task.last_job_id) if db is not None and task.last_job_id else None
    runtime_status = (
        last_job.status if last_job is not None
        else (task.last_dispatch_status or "scheduled") if task.enabled
        else "paused"
    )
    return {
        "id": task.id,
        "name": task.name,
        "owner_id": task.owner_id,
        "agent_id": task.agent_id,
        "session_id": task.session_id or "",
        "cron": task.cron,
        "timezone": task.timezone,
        "query": task.query,
        "enabled": task.enabled,
        "status": runtime_status,
        "next_run_at": _iso_utc(task.next_run_at),
        "last_run_at": _iso_utc(task.last_run_at),
        "last_job_id": task.last_job_id,
        "last_error": task.last_error or (
            last_job.error
            if last_job is not None and last_job.status in {"failed", "dead_letter"}
            else ""
        ),
        "last_dispatch_status": task.last_dispatch_status or "scheduled",
        "consecutive_failures": int(task.consecutive_failures or 0),
        "retry_at": _iso_utc(task.retry_at),
    }


def create(
    owner_id: int,
    agent_id: int,
    name: str,
    expression: str,
    timezone: str,
    query: str,
    session_id: str | None = None,
    allow_unpersisted_session: bool = False,
) -> dict:
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        if agent is None or not agent.enabled:
            raise ValueError("智能体不存在或已停用")
        bound_session = str(session_id or "").strip()[:40]
        if bound_session:
            existing = (
                db.query(Thread)
                .filter(
                    Thread.owner_id == owner_id,
                    Thread.id == bound_session,
                )
                .first()
            )
            if existing is not None and existing.agent_id != agent_id:
                raise ValueError("当前对话与执行智能体不匹配")
            if existing is None and not allow_unpersisted_session:
                raise ValueError("指定的当前对话不存在")
        else:
            existing = (
                db.query(Thread)
                .filter(
                    Thread.owner_id == owner_id,
                    Thread.agent_id == agent_id,
                )
                .order_by(Thread.updated_at.desc())
                .first()
            )
            bound_session = existing.id if existing else ""
        if not bound_session:
            raise ValueError("请先在当前智能体对话中完成一轮消息，再创建定时提醒")
        task = ScheduledTask(
            id=uuid.uuid4().hex,
            owner_id=owner_id,
            agent_id=agent_id,
            session_id=bound_session,
            name=(name or "定时任务").strip()[:128],
            cron=expression.strip(),
            timezone=(timezone or "Asia/Shanghai").strip(),
            query=query.strip(),
            enabled=True,
            next_run_at=next_run(expression, timezone or "Asia/Shanghai"),
        )
        if not task.query:
            raise ValueError("query 不能为空")
        db.add(task)
        db.commit()
        db.refresh(task)
        return _task_dict(task, db)
    finally:
        db.close()


def list_tasks(
    owner_id: int,
    agent_id: int | None = None,
    session_id: str | None = None,
) -> list[dict]:
    db = SessionLocal()
    try:
        query = db.query(ScheduledTask).filter(ScheduledTask.owner_id == owner_id)
        if agent_id is not None:
            query = query.filter(ScheduledTask.agent_id == agent_id)
        if session_id is not None:
            query = query.filter(ScheduledTask.session_id == session_id)
        return [
            _task_dict(row, db)
            for row in query.order_by(ScheduledTask.created_at.desc()).all()
        ]
    finally:
        db.close()


def delete(owner_id: int, task_id: str) -> bool:
    db = SessionLocal()
    try:
        task = db.get(ScheduledTask, task_id)
        if task is None or task.owner_id != owner_id:
            return False
        db.delete(task)
        db.commit()
        return True
    finally:
        db.close()


def set_enabled(owner_id: int, task_id: str, enabled: bool) -> dict | None:
    db = SessionLocal()
    try:
        task = db.get(ScheduledTask, task_id)
        if task is None or task.owner_id != owner_id:
            return None
        task.enabled = enabled
        if enabled:
            task.next_run_at = next_run(task.cron, task.timezone)
            task.retry_at = None
            task.last_error = ""
            task.last_dispatch_status = "scheduled"
            task.consecutive_failures = 0
        else:
            task.retry_at = None
            task.last_dispatch_status = "paused"
        db.commit()
        db.refresh(task)
        return _task_dict(task, db)
    finally:
        db.close()


def _normalized_now(now: dt.datetime | None = None) -> dt.datetime:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is not None:
        current = current.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return current


def _record_dispatch_failure(
    task_id: str,
    due_at: dt.datetime | None,
    current: dt.datetime,
    exc: Exception,
) -> None:
    """独立事务记录单条失败；若另一 Scheduler 已成功推进则不覆盖成功状态。"""
    db = SessionLocal()
    try:
        task = db.get(ScheduledTask, task_id)
        if task is None or not task.enabled or task.next_run_at != due_at:
            return
        failures = int(task.consecutive_failures or 0) + 1
        delay = min(
            max(1.0, float(settings.CRON_RETRY_MAX_SECONDS)),
            max(1.0, float(settings.CRON_RETRY_BASE_SECONDS)) * (2 ** max(0, failures - 1)),
        )
        task.consecutive_failures = failures
        task.retry_at = current + dt.timedelta(seconds=delay)
        task.last_dispatch_status = "retrying"
        task.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        db.commit()
    finally:
        db.close()


def _dispatch_one(task_id: str, current: dt.datetime) -> bool:
    """每条 Cron 使用独立事务；单条失败不会毒化同批其它任务。"""
    db = SessionLocal()
    due_at = None
    try:
        task = db.query(ScheduledTask).filter(
            ScheduledTask.id == task_id,
            ScheduledTask.enabled.is_(True),
            ScheduledTask.next_run_at <= current,
            or_(ScheduledTask.retry_at.is_(None), ScheduledTask.retry_at <= current),
        ).first()
        if task is None:
            return False
        due_at = task.next_run_at
        agent = db.get(Agent, task.agent_id)
        if agent is None or not agent.enabled:
            task.enabled = False
            task.retry_at = None
            task.last_dispatch_status = "blocked"
            task.last_error = "智能体不存在或已停用"
            db.commit()
            return False
        inputs = TaskInput(query=task.query)
        next_at = next_run(
            task.cron,
            task.timezone,
            current.replace(tzinfo=dt.timezone.utc),
        )
        claimed = db.query(ScheduledTask).filter(
            ScheduledTask.id == task.id,
            ScheduledTask.enabled.is_(True),
            ScheduledTask.next_run_at == due_at,
        ).update({
            ScheduledTask.last_run_at: current,
            ScheduledTask.next_run_at: next_at,
            ScheduledTask.retry_at: None,
        }, synchronize_session=False)
        if claimed != 1:
            db.rollback()
            return False
        from .api.chat import build_execution_snapshot
        payload = {
            "user_id": task.owner_id,
            "agent_id": task.agent_id,
            "harness_version": agent.active_version,
            "inputs": {
                "query": inputs.query,
                "project_name": "", "city_name": "", "project_address": "",
                "project_info": "", "industry_structure": "",
                "electricity_trading": "", "image_scale": "",
                "satellite_images": [], "drawing_images": [], "bill_files": [],
                "documents": [], "custom_vars": {}, "custom_files": {},
                "custom_var_labels": {},
            },
            "template_ids": [], "dataset_ids": [], "skill_ids": [], "mcp_ids": [],
            "invoked_agent_ids": [], "provider_id": None,
            "attachment_images": [], "attachment_docs": [],
            "session_id": task.session_id,
            "source": "cron",
            "priority": 20,
            "scheduled_task_id": task.id,
            "approval_tokens": [],
            "execution_snapshot": build_execution_snapshot(db, agent, task.query),
        }
        due_key = due_at.isoformat() if due_at else str(current)
        job_id = jobs.enqueue_in_session(
            db, task.owner_id, task.agent_id, "chat", payload,
            idempotency_key=f"cron:{task.id}:{due_key}",
        )
        db.query(ScheduledTask).filter(ScheduledTask.id == task.id).update({
            ScheduledTask.last_job_id: job_id,
            ScheduledTask.last_error: "",
            ScheduledTask.last_dispatch_status: "dispatched",
            ScheduledTask.consecutive_failures: 0,
            ScheduledTask.retry_at: None,
        }, synchronize_session=False)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        _record_dispatch_failure(task_id, due_at, current, exc)
        raise
    finally:
        db.close()


def dispatch_due_report(now: dt.datetime | None = None) -> tuple[int, int, str]:
    current = _normalized_now(now)
    db = SessionLocal()
    try:
        task_ids = [row[0] for row in db.query(ScheduledTask.id).filter(
            ScheduledTask.enabled.is_(True),
            ScheduledTask.next_run_at <= current,
            or_(ScheduledTask.retry_at.is_(None), ScheduledTask.retry_at <= current),
        ).order_by(ScheduledTask.next_run_at).limit(50).all()]
    finally:
        db.close()
    dispatched = 0
    failures = 0
    last_error = ""
    for task_id in task_ids:
        try:
            if _dispatch_one(task_id, current):
                dispatched += 1
        except Exception as exc:  # 单任务失败已持久化，继续处理本批其它任务
            failures += 1
            last_error = f"{type(exc).__name__}: {exc}"[:2000]
            logger.warning("Cron 单任务派发失败（task=%s）：%s", task_id, exc)
    return dispatched, failures, last_error


def dispatch_due(now: dt.datetime | None = None) -> int:
    """兼容入口：返回成功派发数，单任务失败已隔离并记录。"""
    return dispatch_due_report(now)[0]


def _scheduler_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:cron"


def touch_scheduler(scheduler_id: str, *, successful: bool, error: str = "") -> None:
    db = SessionLocal()
    try:
        row = db.get(SchedulerHeartbeat, scheduler_id)
        if row is None:
            row = SchedulerHeartbeat(scheduler_id=scheduler_id)
            db.add(row)
        now = dt.datetime.now(dt.timezone.utc)
        row.last_seen = now
        row.last_error = (error or "")[:2000]
        if successful:
            row.last_successful_dispatch_at = now
        db.commit()
    finally:
        db.close()


def remove_scheduler(scheduler_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(SchedulerHeartbeat, scheduler_id)
        if row is not None:
            db.delete(row)
            db.commit()
    finally:
        db.close()


async def run_scheduler(stop_event: asyncio.Event) -> None:
    logger.info("Cron 调度器启动")
    scheduler_id = _scheduler_id()
    await asyncio.to_thread(touch_scheduler, scheduler_id, successful=True)
    try:
        while not stop_event.is_set():
            try:
                count, failures, last_error = await asyncio.to_thread(dispatch_due_report)
                await asyncio.to_thread(
                    touch_scheduler,
                    scheduler_id,
                    successful=failures == 0,
                    error=last_error,
                )
                if count:
                    logger.info("Cron 已派发 %s 个任务", count)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cron 调度失败：%s", exc)
                await asyncio.to_thread(
                    touch_scheduler,
                    scheduler_id,
                    successful=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
    finally:
        await asyncio.to_thread(remove_scheduler, scheduler_id)
        logger.info("Cron 调度器停止")
