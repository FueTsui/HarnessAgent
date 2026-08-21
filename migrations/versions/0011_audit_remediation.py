"""审计修复：父子任务关联、旧对话回填、MCP 风险策略与结果脱敏。

Revision ID: 0011_audit_remediation
Revises: 0010_persistent_attachments
"""
from __future__ import annotations

import datetime
import hashlib
import json

from alembic import op
import sqlalchemy as sa

from backend.secret_store import decrypt_secret, encrypt_secret


revision = "0011_audit_remediation"
down_revision = "0010_persistent_attachments"
branch_labels = None
depends_on = None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stable_id(prefix: str, value: object) -> str:
    return hashlib.sha256(f"{prefix}:{value}".encode("utf-8")).hexdigest()[:32]


def _as_bool(value) -> bool:
    return bool(value) and str(value).lower() not in {"0", "false", "none"}


def _backfill_legacy_conversations(bind) -> int:
    """把旧 conversations 映射到 Thread/Turn/Item；确定性 ID 保证幂等。"""
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    required = {"conversations", "users", "threads", "turns", "items"}
    if not required.issubset(tables):
        return 0
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if not {"id", "user_id", "agent_id", "query", "answer", "created_at"}.issubset(columns):
        return 0

    optional = {
        name: name if name in columns else f"NULL AS {name}"
        for name in (
            "source", "session_id", "title", "project_id", "is_pinned",
            "is_archived", "export_files",
        )
    }
    rows = bind.execute(sa.text(
        "SELECT id, user_id, agent_id, query, answer, created_at, "
        + ", ".join(optional.values())
        + " FROM conversations ORDER BY created_at, id"
    )).mappings().all()
    if not rows:
        return 0

    user_ids = {row[0] for row in bind.execute(sa.text("SELECT id FROM users")).all()}
    agent_ids = (
        {row[0] for row in bind.execute(sa.text("SELECT id FROM agents")).all()}
        if "agents" in tables else set()
    )
    project_ids = (
        {row[0] for row in bind.execute(sa.text("SELECT id FROM projects")).all()}
        if "projects" in tables else set()
    )
    existing_threads = {
        str(row.id): int(row.owner_id)
        for row in bind.execute(sa.text("SELECT id, owner_id FROM threads")).mappings()
    }
    max_sequences = {
        str(row.thread_id): int(row.sequence or 0)
        for row in bind.execute(sa.text(
            "SELECT thread_id, MAX(sequence) AS sequence FROM turns GROUP BY thread_id"
        )).mappings()
    }
    created = 0

    for legacy in rows:
        owner_id = legacy["user_id"]
        if owner_id not in user_ids:
            # 无有效所有者的数据不能安全暴露给任意用户，保留在旧表等待人工处置。
            continue
        raw_session = str(legacy.get("session_id") or "").strip()
        thread_id = raw_session[:40] if raw_session else _stable_id("legacy-thread", legacy["id"])
        if thread_id in existing_threads and existing_threads[thread_id] != owner_id:
            thread_id = _stable_id("legacy-thread-owner", f"{owner_id}:{legacy['id']}")

        agent_id = legacy.get("agent_id") if legacy.get("agent_id") in agent_ids else None
        project_id = legacy.get("project_id") if legacy.get("project_id") in project_ids else None
        created_at = legacy.get("created_at") or _now()
        query = str(legacy.get("query") or "")
        answer = str(legacy.get("answer") or "")
        title = str(legacy.get("title") or "").strip()[:80] or query.strip()[:80]

        if thread_id not in existing_threads:
            # This helper is intentionally idempotent and is also used by the
            # repair command after later schema revisions.  Include the 0018
            # memory defaults when those columns already exist, while keeping
            # the original 0011 SQL valid during a linear fresh install.
            thread_columns = {
                column["name"] for column in inspector.get_columns("threads")
            }
            memory_columns = (
                ", memory_enabled, memory_excluded"
                if {"memory_enabled", "memory_excluded"}.issubset(thread_columns)
                else ""
            )
            memory_values = ", 1, 0" if memory_columns else ""
            bind.execute(sa.text(
                "INSERT INTO threads "
                "(id, project_id, owner_id, agent_id, title, status, is_pinned, "
                "is_archived, context_summary, created_at, updated_at"
                f"{memory_columns}) "
                "VALUES (:id, :project_id, :owner_id, :agent_id, :title, 'active', "
                ":is_pinned, :is_archived, '', :created_at, :updated_at"
                f"{memory_values})"
            ), {
                "id": thread_id,
                "project_id": project_id,
                "owner_id": owner_id,
                "agent_id": agent_id,
                "title": title,
                "is_pinned": _as_bool(legacy.get("is_pinned")),
                "is_archived": _as_bool(legacy.get("is_archived")),
                "created_at": created_at,
                "updated_at": created_at,
            })
            existing_threads[thread_id] = owner_id
            max_sequences.setdefault(thread_id, 0)
        else:
            # 只合并可逆的展示元数据，不覆盖新系统已经生成的非空标题。
            bind.execute(sa.text(
                "UPDATE threads SET "
                "is_pinned = CASE WHEN :is_pinned THEN :is_pinned ELSE is_pinned END, "
                "is_archived = CASE WHEN :is_archived THEN :is_archived ELSE is_archived END, "
                "updated_at = CASE WHEN updated_at < :updated_at THEN :updated_at ELSE updated_at END "
                "WHERE id=:id"
            ), {
                "id": thread_id,
                "is_pinned": _as_bool(legacy.get("is_pinned")),
                "is_archived": _as_bool(legacy.get("is_archived")),
                "updated_at": created_at,
            })

        turn_id = _stable_id("legacy-conversation-turn", legacy["id"])
        if bind.execute(
            sa.text("SELECT 1 FROM turns WHERE id=:id"), {"id": turn_id}
        ).first():
            continue
        max_sequences[thread_id] = max_sequences.get(thread_id, 0) + 1
        source = str(legacy.get("source") or "legacy")[:16]
        snapshot = encrypt_secret(json.dumps({
            "source": "legacy_backfill",
            "legacy_conversation_id": legacy["id"],
            "legacy_session_id": raw_session,
        }, ensure_ascii=False))
        item_count = 2 if answer else 1
        bind.execute(sa.text(
            "INSERT INTO turns "
            "(id, continuation_of_turn_id, thread_id, owner_id, agent_id, sequence, "
            "status, source, input, final_output, error, execution_snapshot, item_sequence, "
            "started_at, completed_at, created_at, updated_at) "
            "VALUES (:id, NULL, :thread_id, :owner_id, :agent_id, :sequence, "
            "'completed', :source, :input, :output, '', :snapshot, :item_sequence, "
            ":created_at, :created_at, :created_at, :created_at)"
        ), {
            "id": turn_id,
            "thread_id": thread_id,
            "owner_id": owner_id,
            "agent_id": agent_id,
            "sequence": max_sequences[thread_id],
            "source": source,
            "input": query,
            "output": answer,
            "snapshot": snapshot,
            "item_sequence": item_count,
            "created_at": created_at,
        })
        base_payload = {
            "legacy_conversation_id": legacy["id"],
            "source": "legacy_backfill",
        }
        bind.execute(sa.text(
            "INSERT INTO items "
            "(id, thread_id, turn_id, sequence, kind, role, name, status, content, payload, created_at) "
            "VALUES (:id, :thread_id, :turn_id, 1, 'message', 'user', '', "
            "'completed', :content, :payload, :created_at)"
        ), {
            "id": _stable_id("legacy-conversation-user-item", legacy["id"]),
            "thread_id": thread_id,
            "turn_id": turn_id,
            "content": query,
            "payload": json.dumps(base_payload, ensure_ascii=False),
            "created_at": created_at,
        })
        if answer:
            assistant_payload = dict(base_payload)
            try:
                exports = json.loads(legacy.get("export_files") or "[]")
            except (json.JSONDecodeError, TypeError):
                exports = []
            if isinstance(exports, list) and exports:
                assistant_payload["legacy_export_files"] = exports
            bind.execute(sa.text(
                "INSERT INTO items "
                "(id, thread_id, turn_id, sequence, kind, role, name, status, content, payload, created_at) "
                "VALUES (:id, :thread_id, :turn_id, 2, 'message', 'assistant', '', "
                "'completed', :content, :payload, :created_at)"
            ), {
                "id": _stable_id("legacy-conversation-assistant-item", legacy["id"]),
                "thread_id": thread_id,
                "turn_id": turn_id,
                "content": answer,
                "payload": json.dumps(assistant_payload, ensure_ascii=False),
                "created_at": created_at,
            })
        created += 1
    return created


def _backfill_parent_jobs_and_scrub_results(bind) -> None:
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        return
    job_ids = {str(row[0]) for row in bind.execute(sa.text("SELECT id FROM jobs")).all()}
    for row in bind.execute(sa.text(
        "SELECT id, payload, result, parent_job_id FROM jobs"
    )).mappings():
        if not row.get("parent_job_id") and row.get("payload"):
            try:
                payload = json.loads(decrypt_secret(row["payload"]))
            except Exception:
                payload = {}
            parent_id = str(payload.get("parent_run_id") or "")[:32]
            if parent_id and parent_id in job_ids:
                bind.execute(sa.text(
                    "UPDATE jobs SET parent_job_id=:parent WHERE id=:id"
                ), {"parent": parent_id, "id": row["id"]})
        if row.get("result"):
            try:
                result = json.loads(row["result"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(result, dict):
                continue
            changed = False
            for key in ("reasoning", "reasoning_content", "thinking"):
                if key in result:
                    result.pop(key, None)
                    changed = True
            if changed:
                bind.execute(sa.text(
                    "UPDATE jobs SET result=:result WHERE id=:id"
                ), {
                    "result": json.dumps(result, ensure_ascii=False),
                    "id": row["id"],
                })


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "mcp_servers" in tables:
        columns = {column["name"] for column in inspector.get_columns("mcp_servers")}
        if "risk_policy" not in columns:
            op.add_column(
                "mcp_servers",
                sa.Column("risk_policy", sa.String(length=16), nullable=False, server_default="auto"),
            )
        # 当前 Tushare MCP 是纯数据查询端点；显式写入 annotations/工具名仍由运行时覆盖。
        bind.execute(sa.text(
            "UPDATE mcp_servers SET risk_policy='read_only' "
            "WHERE lower(url) LIKE 'https://api.tushare.pro/mcp%'"
        ))

    if "jobs" in tables:
        columns = {column["name"] for column in sa.inspect(bind).get_columns("jobs")}
        if "parent_job_id" not in columns:
            op.add_column("jobs", sa.Column("parent_job_id", sa.String(length=32), nullable=True))
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("jobs")}
        if "ix_jobs_parent_job_id" not in indexes:
            op.create_index("ix_jobs_parent_job_id", "jobs", ["parent_job_id"])
        if bind.dialect.name != "sqlite":
            foreign_keys = {fk.get("name") for fk in sa.inspect(bind).get_foreign_keys("jobs")}
            if "fk_jobs_parent_job_id" not in foreign_keys:
                op.create_foreign_key(
                    "fk_jobs_parent_job_id", "jobs", "jobs",
                    ["parent_job_id"], ["id"], ondelete="SET NULL",
                )

    _backfill_legacy_conversations(bind)
    _backfill_parent_jobs_and_scrub_results(bind)


def downgrade() -> None:
    raise RuntimeError("审计、对话回填和脱敏数据不允许自动降级移除")
