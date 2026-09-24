"""Root-only guest erasure; database changes are atomic, files follow commit.

Unexpected business resources and references from surviving accounts fail closed.
This intentionally does not turn ordinary user deletion into a cascade operation.
"""
import json
import shutil
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import EXPORT_DIR, KNOWLEDGE_DIR, UPLOAD_DIR, WORKSPACE_DIR
from .database import Base
from .guardrail_models import GuardrailPolicy, GuardrailReview
from .memory_store import MemoryEntry
from .models import (
    Agent, ApiKey, Artifact, Attachment, AuditLog, AuthSession, HarnessVersion,
    ImprovementProposal, Item, Job, JobGuidance, ModelProvider, Project,
    ROLE_GUEST, ROLE_ROOT, ScheduledTask, Thread, TokenUsage, ToolApproval,
    Turn, User, UserTokenLimit,
)
from .service_models import CustomService, ServiceRun

ANONYMOUS_NAME = "已删除访客"
TERMINAL_JOBS = {"done", "failed", "cancelled", "dead_letter"}
_COUNTS = (
    "users", "threads", "turns", "jobs", "attachments", "artifacts",
    "personal_models", "agents", "sessions", "files", "workspaces",
    "projects", "items", "guidance", "approvals", "reviews", "memories",
    "harness_versions", "proposals", "schedules", "service_runs", "api_keys",
    "token_limits", "legacy_conversations", "legacy_events",
)


def _conflict(reason):
    raise HTTPException(409, "未删除任何访客：" + reason)


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _refs(value, agents, providers, key=""):
    """Recognize configuration references, without matching arbitrary text/IDs."""
    if isinstance(value, dict):
        for name, item in value.items():
            if name in {"agent_id", "default_agent_id"} and isinstance(item, int) and item in agents:
                return True
            if name in {"provider_id", "default_provider_id"} and isinstance(item, int) and item in providers:
                return True
            if name == "id" and isinstance(item, int) and ((key in {"agent", "sub_agents"} and item in agents)
                                 or (key in {"provider", "providers"} and item in providers)):
                return True
            if name in {"agent_ids", "provider_ids", "fallback_provider_ids"}:
                pool = agents if name == "agent_ids" else providers
                if isinstance(item, list) and any(isinstance(x, int) and x in pool for x in item):
                    return True
            if _refs(item, agents, providers, name):
                return True
    elif isinstance(value, list):
        return any(_refs(item, agents, providers, key) for item in value)
    return False


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _direct_file(root, name):
    """Only original direct children; never reinterpret traversal as a filename."""
    try:
        path = Path(name)
        candidate = path if path.is_absolute() else root / path
        if candidate.parent.resolve() != root.resolve() or candidate.is_symlink():
            return None
        resolved = candidate.resolve()
        return resolved if resolved.parent == root.resolve() else None
    except (OSError, ValueError):
        return None


def _payload_paths(value):
    """Conservative reference protection only; arbitrary text proves no ownership."""
    paths = set()
    for raw in _strings(_json(value)):
        # Plain names in message text are not file ownership evidence.
        try:
            if not Path(raw).is_absolute():
                continue
        except (OSError, ValueError):
            continue
        for root in (UPLOAD_DIR, EXPORT_DIR):
            path = _direct_file(root, raw)
            if path is not None:
                paths.add(path)
                if root == UPLOAD_DIR:
                    paths.add(path.with_suffix(".name"))
    return paths


def _owned_payload_paths(value):
    """Legacy server-populated attachment fields, never arbitrary query text."""
    payload = _json(value)
    if not isinstance(payload, dict):
        return set()
    paths = set()
    for field in ("attachment_docs", "attachment_images"):
        values = payload.get(field, [])
        if not isinstance(values, list):
            continue
        for raw in values:
            if not isinstance(raw, str):
                continue
            path = _direct_file(UPLOAD_DIR, raw)
            if path is not None:
                paths.update((path, path.with_suffix(".name")))
    return paths


def _legacy_export_paths(value):
    """Structured legacy export filenames; never scan message text for names."""
    paths = set()
    for name in _strings(_json(value)):
        path = _direct_file(EXPORT_DIR, name)
        if path is not None:
            paths.add(path)
    return paths


def _surviving_file_refs(db):
    paths = set()
    strings = []
    for row in db.query(Attachment).all():
        path = _direct_file(UPLOAD_DIR, row.storage_name)
        if path is not None:
            paths.update((path, path.with_suffix(".name")))
    for row in db.query(Artifact).all():
        path = _direct_file(EXPORT_DIR, row.filename)
        if path is not None:
            paths.add(path)
    for model, fields in ((Job, ("payload", "result")),
                          (Turn, ("execution_snapshot",)),
                          (Item, ("payload",))):
        for row in db.query(model).all():
            for field in fields:
                value = getattr(row, field)
                paths.update(_payload_paths(value))
                strings.extend(_strings(_json(value)))
                if model is Item:
                    data = _json(value)
                    if isinstance(data, dict):
                        paths.update(_legacy_export_paths(data.get("legacy_export_files")))
    connection = db.connection()
    if inspect(connection).has_table("conversations"):
        from sqlalchemy import MetaData, Table
        legacy = Table("conversations", MetaData(), autoload_with=connection)
        for row in connection.execute(select(legacy)).mappings():
            for field, value in row.items():
                paths.update(_payload_paths(value))
                if field == "export_files":
                    paths.update(_legacy_export_paths(value))
    return paths, strings


def _cleanup_files(db, candidates, user_ids, result, root_id):
    """Best effort after commit; never remove a survivor's shared physical file."""
    try:
        # Serialize the reference recheck and filesystem operation with creates.
        # This is a new transaction: metadata erasure has already committed.
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("UPDATE users SET id=id WHERE id=:id"), {"id": root_id})
        protected, references = _surviving_file_refs(db)
    except Exception:
        db.rollback()
        result["cleanup_warnings"].append("数据库已清理；文件引用核查失败，物理文件暂保留")
        return
    failed = 0
    for path in candidates - protected:
        safe = next((_direct_file(root, str(path)) for root in (UPLOAD_DIR, EXPORT_DIR)
                     if _direct_file(root, str(path)) is not None), None)
        if safe is None:
            continue
        try:
            if safe.is_file():
                safe.unlink()
                result["deleted_files"] += 1
        except OSError:
            failed += 1
    if failed:
        result["cleanup_warnings"].append(f"数据库已清理；{failed} 个文件删除失败，可稍后重试清理")
    root = WORKSPACE_DIR.resolve()
    for user_id in user_ids:
        candidate = WORKSPACE_DIR / f"user_{user_id}"
        try:
            # IDs may be reused after commit; do not erase a newly created user.
            if db.query(User.id).filter(User.id == user_id).first() is not None:
                result["cleanup_warnings"].append("用户编号已重新使用，对应工作目录暂保留")
                continue
            target = candidate.resolve()
            if candidate.is_symlink() or target.parent != root:
                result["cleanup_warnings"].append("工作目录边界异常，已保留该目录")
                continue
            shared = False
            for raw in references:
                try:
                    if Path(raw).is_absolute() and Path(raw).resolve().is_relative_to(target):
                        shared = True
                        break
                except (OSError, ValueError):
                    continue
            if shared:
                result["cleanup_warnings"].append("工作目录仍被保留任务引用，已保留共享文件")
            elif target.is_dir():
                # Both the root and the exact absolute deletion target were checked.
                shutil.rmtree(target)
                result["deleted_workspaces"] += 1
        except OSError:
            result["cleanup_warnings"].append("数据库已清理；一个工作目录删除失败")
    db.commit()


def cleanup_guest_users(db: Session, current: User, user_ids: list[int] | None = None) -> dict:
    """Erase selected guests (or all), commit once, then clean unshared files.

    Caller changes (e.g. an explicitly authorized role restoration) participate
    in this same transaction. Any preflight/database error rolls them back too.
    """
    if current.role != ROLE_ROOT or not current.is_active:
        raise HTTPException(403, "仅 root 可清理访客")
    result = {f"deleted_{name}": 0 for name in _COUNTS}
    result.update(retained_token_usage=0, retained_audit_logs=0, cleanup_warnings=[])
    candidates = set()
    try:
        # A no-op write upgrades an already-open SQLite read transaction too.
        # Unlike BEGIN IMMEDIATE this is valid after authentication queried User.
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("UPDATE users SET id=id WHERE id=:id"), {"id": current.id})
        db.flush()
        query = db.query(User).populate_existing()
        if user_ids is None:
            query = query.filter(User.role == ROLE_GUEST)
        else:
            query = query.filter(User.id.in_(set(user_ids)))
        users = query.with_for_update().all()
        if any(user.role != ROLE_GUEST for user in users):
            _conflict("包含非访客账号，普通用户仍须先处理关联数据")
        if user_ids is not None and len(users) != len(set(user_ids)):
            _conflict("目标账号已变化，请刷新后重试")
        ids = {user.id for user in users}
        if not ids:
            db.commit()
            return result

        rows = {}
        def own(model, column, values, count):
            found = db.query(model).filter(column.in_(values)).all()
            rows[model.__table__] = found
            result[f"deleted_{count}"] = len(found)
            return found

        rows[User.__table__] = users
        result["deleted_users"] = len(users)
        agents = own(Agent, Agent.created_by, ids, "agents")
        providers = own(ModelProvider, ModelProvider.created_by, ids, "personal_models")
        if any(a.name != f"__guest_agent_{a.created_by}" or a.is_public or a.is_default for a in agents):
            _conflict("访客拥有普通智能体，请先转移或删除该业务资源")
        if any(p.is_public or not p.name.startswith("__personal_model_") for p in providers):
            _conflict("访客拥有非个人私有模型，请先转移或删除该业务资源")
        agent_ids = {row.id for row in agents}
        provider_ids = {row.id for row in providers}
        threads = own(Thread, Thread.owner_id, ids, "threads")
        turns = own(Turn, Turn.owner_id, ids, "turns")
        jobs = own(Job, Job.owner_id, ids, "jobs")
        if any(job.status not in TERMINAL_JOBS for job in jobs):
            _conflict("存在排队、运行中或等待审批的任务，请待任务结束或取消完成后重试")
        if any(turn.status in {"queued", "pending", "running", "awaiting_approval"} for turn in turns):
            _conflict("存在尚未结束的对话轮次，请待任务结束后重试")
        job_ids = {row.id for row in jobs}
        turn_ids = {row.id for row in turns}
        own(Project, Project.user_id, ids, "projects")
        own(AuthSession, AuthSession.user_id, ids, "sessions")
        own(UserTokenLimit, UserTokenLimit.user_id, ids, "token_limits")
        own(MemoryEntry, MemoryEntry.owner_id, ids, "memories")
        attachments = own(Attachment, Attachment.owner_id, ids, "attachments")
        artifacts = own(Artifact, Artifact.owner_id, ids, "artifacts")
        items = own(Item, Item.turn_id, turn_ids, "items")
        own(JobGuidance, JobGuidance.owner_id, ids, "guidance")
        own(ToolApproval, ToolApproval.user_id, ids, "approvals")
        own(GuardrailReview, GuardrailReview.user_id, ids, "reviews")
        own(HarnessVersion, HarnessVersion.agent_id, agent_ids, "harness_versions")
        own(ImprovementProposal, ImprovementProposal.agent_id, agent_ids, "proposals")
        own(ScheduledTask, ScheduledTask.owner_id, ids, "schedules")
        service_runs = own(ServiceRun, ServiceRun.owner_id, ids, "service_runs")
        if any(row.status in {"pending", "running", "awaiting_approval"} for row in service_runs):
            _conflict("存在运行中的服务任务，请待任务结束后重试")
        own(ApiKey, ApiKey.created_by, ids, "api_keys")

        # Check every mapped FK before CASCADE/SET NULL can alter another owner's
        # rows. Unexpected owned business objects also block here rather than be
        # silently destroyed. Usage references are explicitly detached below.
        planned = {table: {getattr(row, next(iter(table.primary_key)).name) for row in items}
                   for table, items in rows.items()}
        for table in Base.metadata.sorted_tables:
            for fk in table.foreign_keys:
                target_ids = planned.get(fk.column.table, set())
                if not target_ids or table is TokenUsage.__table__:
                    continue
                stmt = select(fk.parent).where(fk.parent.in_(target_ids))
                if table in planned:
                    stmt = stmt.where(~next(iter(table.primary_key)).in_(planned[table]))
                if db.execute(stmt.limit(1)).first() is not None:
                    _conflict(f"存在共享或未转移的关联数据（{table.name}），请先处理关联")

        # JSON references have no database FK. Surviving configuration/history
        # may contain private frozen model credentials; do not silently leave it.
        for model, fields in ((Agent, ("routing", "agent_ids")),
                              (CustomService, ("agent_ids",)),
                              (GuardrailPolicy, ("config",)),
                              (Job, ("payload",)), (Turn, ("execution_snapshot",))):
            deleted = planned.get(model.__table__, set())
            for row in db.query(model).filter(~model.id.in_(deleted)).all():
                if model is Job and row.agent_id in agent_ids:
                    _conflict("其他账号任务引用了访客智能体")
                for field in fields:
                    value = _json(getattr(row, field))
                    if field == "agent_ids":
                        value = {field: value}
                    if _refs(value, agent_ids, provider_ids):
                        _conflict("其他资源仍引用访客模型或智能体，请先处理共享依赖")
        meta = KNOWLEDGE_DIR / "_datasets.json"
        if meta.exists():
            try:
                datasets = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _conflict("知识库归属信息无法核验")
            if isinstance(datasets, dict) and any(isinstance(value, dict) and value.get("created_by") in ids
                                                  for value in datasets.values()):
                _conflict("访客拥有知识库，请先转移或删除知识库")

        for row in attachments:
            path = _direct_file(UPLOAD_DIR, row.storage_name)
            if path is not None:
                candidates.update((path, path.with_suffix(".name")))
        for row in artifacts:
            path = _direct_file(EXPORT_DIR, row.filename)
            if path is not None:
                candidates.add(path)
        for row in jobs:
            candidates.update(_owned_payload_paths(row.payload))
        for row in items:
            payload = _json(row.payload)
            if isinstance(payload, dict):
                candidates.update(_legacy_export_paths(payload.get("legacy_export_files")))

        result["retained_token_usage"] = db.query(TokenUsage).filter(TokenUsage.user_id.in_(ids)).update(
            {TokenUsage.user_id: None, TokenUsage.username: ANONYMOUS_NAME, TokenUsage.run_id: ""},
            synchronize_session=False)
        db.query(TokenUsage).filter(TokenUsage.agent_id.in_(agent_ids)).update(
            {TokenUsage.agent_id: None}, synchronize_session=False)
        db.query(TokenUsage).filter(TokenUsage.provider_id.in_(provider_ids)).update(
            {TokenUsage.provider_id: None}, synchronize_session=False)
        result["retained_audit_logs"] = db.query(AuditLog).filter(AuditLog.user_id.in_(ids)).update(
            {AuditLog.user_id: None, AuditLog.username: ANONYMOUS_NAME, AuditLog.ip: ""},
            synchronize_session=False)

        connection = db.connection()
        inspector = inspect(connection)
        for name, column, values, count in (("conversations", "user_id", ids, "legacy_conversations"),
                                             ("run_events", "run_id", job_ids | turn_ids, "legacy_events")):
            if values and inspector.has_table(name) and column in {c["name"] for c in inspector.get_columns(name)}:
                from sqlalchemy import MetaData, Table
                legacy = Table(name, MetaData(), autoload_with=connection)
                if name == "conversations" and "export_files" in legacy.c:
                    for value in connection.execute(select(legacy.c.export_files).where(legacy.c.user_id.in_(ids))).scalars():
                        candidates.update(_legacy_export_paths(value))
                result[f"deleted_{count}"] = connection.execute(legacy.delete().where(legacy.c[column].in_(values))).rowcount
        for table in reversed(Base.metadata.sorted_tables):
            if table in planned and planned[table]:
                db.execute(table.delete().where(next(iter(table.primary_key)).in_(planned[table])))
        db.commit()
        db.expire_all()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "未删除任何访客：关联数据已变化，请刷新并处理共享依赖后重试") from exc
    except Exception:
        db.rollback()
        raise
    try:
        _cleanup_files(db, candidates, ids, result, current.id)
    except Exception:
        db.rollback()
        result["cleanup_warnings"].append("数据库已清理；部分文件处理未完成，可稍后重试清理")
    return result
