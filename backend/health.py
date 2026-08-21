"""数据库、迁移、Worker 与队列的可操作健康快照。"""
from __future__ import annotations

import datetime

from sqlalchemy import func, text

from . import jobs
from .config import security_config_problems, security_config_warnings, settings
from .migrations import EXPECTED_SCHEMA_REVISION
from .models import Job, SchedulerHeartbeat, WorkerHeartbeat


def collect_health(
    engine,
    session_factory,
    *,
    require_worker: bool,
    require_scheduler: bool = False,
) -> tuple[dict, bool]:
    snapshot = {
        "status": "ok",
        "deployment": {
            "environment": settings.APP_ENV,
            "security_ok": not security_config_problems(False),
            "warnings": security_config_warnings(),
        },
        "database": {"ok": False},
        "migration": {"ok": False, "expected": EXPECTED_SCHEMA_REVISION},
        "workers": {"ok": not require_worker, "active": 0, "capacity": 0},
        "scheduler": {"ok": not require_scheduler, "active": 0},
        "queue": {"pending": 0, "running": 0, "awaiting_approval": 0, "dead_letter": 0},
    }
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            snapshot["database"]["ok"] = True
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            snapshot["migration"].update({
                "current": revision,
                "ok": revision == EXPECTED_SCHEMA_REVISION,
            })
        db = session_factory()
        try:
            counts = dict(
                db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
            )
            snapshot["queue"] = {
                "pending": int(counts.get(jobs.PENDING, 0)),
                "running": int(counts.get(jobs.RUNNING, 0)),
                "awaiting_approval": int(counts.get(jobs.AWAITING_APPROVAL, 0)),
                "dead_letter": int(counts.get(jobs.DEAD_LETTER, 0)),
            }
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                seconds=max(30, int(settings.JOB_HEARTBEAT_SECONDS) * 3)
            )
            rows = db.query(WorkerHeartbeat).all()
            active = []
            for row in rows:
                seen = row.last_seen
                if seen is not None and seen.tzinfo is None:
                    seen = seen.replace(tzinfo=datetime.timezone.utc)
                if seen is not None and seen >= cutoff:
                    active.append(row)
            snapshot["workers"] = {
                "ok": bool(active) or not require_worker,
                "active": len(active),
                "capacity": sum(int(row.capacity or 0) for row in active),
            }
            scheduler_cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                seconds=max(30, int(settings.CRON_HEARTBEAT_STALE_SECONDS))
            )
            scheduler_rows = db.query(SchedulerHeartbeat).all()
            active_schedulers = []
            for row in scheduler_rows:
                seen = row.last_seen
                if seen is not None and seen.tzinfo is None:
                    seen = seen.replace(tzinfo=datetime.timezone.utc)
                if seen is not None and seen >= scheduler_cutoff:
                    active_schedulers.append(row)
            last_scheduler_success = max(
                (
                    row.last_successful_dispatch_at
                    for row in active_schedulers
                    if row.last_successful_dispatch_at is not None
                ),
                default=None,
            )
            if last_scheduler_success is not None and last_scheduler_success.tzinfo is None:
                last_scheduler_success = last_scheduler_success.replace(
                    tzinfo=datetime.timezone.utc
                )
            snapshot["scheduler"] = {
                "ok": bool(active_schedulers) or not require_scheduler,
                "active": len(active_schedulers),
                "last_successful_dispatch_at": (
                    last_scheduler_success.isoformat() if last_scheduler_success else ""
                ),
                "last_error": next(
                    (row.last_error for row in active_schedulers if row.last_error), ""
                ),
            }
            oldest_pending = db.query(func.min(Job.created_at)).filter(
                Job.status == jobs.PENDING
            ).scalar()
            if oldest_pending is not None:
                if oldest_pending.tzinfo is None:
                    oldest_pending = oldest_pending.replace(tzinfo=datetime.timezone.utc)
                snapshot["queue"]["oldest_pending_seconds"] = max(
                    0, int((datetime.datetime.now(datetime.timezone.utc) - oldest_pending).total_seconds())
                )
        finally:
            db.close()
    except Exception as exc:
        snapshot["database"]["error"] = type(exc).__name__
    ready = bool(
        snapshot["database"]["ok"]
        and snapshot["migration"]["ok"]
        and snapshot["workers"]["ok"]
        and snapshot["scheduler"]["ok"]
    )
    backlog = snapshot["queue"]["pending"] >= int(settings.JOB_QUEUE_WARN_PENDING)
    dead_letters = snapshot["queue"]["dead_letter"] > 0
    scheduler_error = bool(snapshot["scheduler"].get("last_error"))
    snapshot["status"] = "ok" if ready and not backlog and not dead_letters and not scheduler_error else (
        "degraded" if ready else "unavailable"
    )
    if backlog:
        snapshot["queue"]["warning"] = "pending backlog exceeds threshold"
    if dead_letters:
        snapshot["queue"]["dead_letter_warning"] = "dead-letter jobs require attention"
    if scheduler_error:
        snapshot["scheduler"]["warning"] = "last scheduler scan reported failures"
    return snapshot, ready
