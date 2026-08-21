"""补齐队列、Harness、会话与认证安全字段。

Revision ID: 0002_security_hardening
Revises: 0001_baseline
"""
from alembic import op
import sqlalchemy as sa

from backend.database import Base
import backend.models  # noqa: F401

revision = "0002_security_hardening"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _add_missing(table: str, definitions: dict[str, sa.Column]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = _columns(inspector, table)
    for name, column in definitions.items():
        if name not in existing:
            op.add_column(table, column)


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    indexes = {row["name"] for row in inspector.get_indexes(table)}
    constraints = {
        row["name"] for row in inspector.get_unique_constraints(table)
        if row.get("name")
    }
    return name in indexes or name in constraints


def upgrade() -> None:
    # 为较早的无版本数据库创建后来新增的整张表，再逐列补齐旧表。
    Base.metadata.create_all(bind=op.get_bind())
    _add_missing("users", {
        "token_version": sa.Column("token_version", sa.Integer(), nullable=False,
                                   server_default="0"),
    })
    _add_missing("agents", {
        "active_version": sa.Column("active_version", sa.Integer(), nullable=False,
                                    server_default="1"),
        "builtin_tools": sa.Column("builtin_tools", sa.Text(), nullable=False,
                                   server_default="[]"),
    })
    _add_missing("jobs", {
        "idempotency_key": sa.Column("idempotency_key", sa.String(128)),
        "session_key": sa.Column("session_key", sa.String(128), server_default=""),
        "lease_token": sa.Column("lease_token", sa.String(32), server_default=""),
        "attempt_count": sa.Column("attempt_count", sa.Integer(), server_default="0"),
        "max_attempts": sa.Column("max_attempts", sa.Integer(), server_default="3"),
        "event_sequence": sa.Column("event_sequence", sa.Integer(), server_default="0"),
    })
    _add_missing("conversations", {
        "project_id": sa.Column("project_id", sa.Integer()),
        "is_pinned": sa.Column("is_pinned", sa.Boolean(), server_default=sa.false()),
        "is_archived": sa.Column("is_archived", sa.Boolean(), server_default=sa.false()),
    })
    _add_missing("projects", {
        "is_pinned": sa.Column("is_pinned", sa.Boolean(), server_default=sa.false()),
        "is_archived": sa.Column("is_archived", sa.Boolean(), server_default=sa.false()),
    })
    _add_missing("scheduled_tasks", {
        "session_id": sa.Column("session_id", sa.String(40), server_default=""),
    })
    _add_missing("model_providers", {
        "wire_api": sa.Column("wire_api", sa.String(32), server_default="chat_completions"),
        "auth_type": sa.Column("auth_type", sa.String(16), server_default="bearer"),
        "auth_header": sa.Column("auth_header", sa.String(64), server_default=""),
        "api_version": sa.Column("api_version", sa.String(64), server_default=""),
        "api_version_mode": sa.Column("api_version_mode", sa.String(16), server_default="none"),
        "custom_headers": sa.Column("custom_headers", sa.Text(), server_default="{}"),
        "extra_body": sa.Column("extra_body", sa.Text(), server_default="{}"),
        "model_list_path": sa.Column("model_list_path", sa.String(128), server_default="/models"),
        "reasoning_effort": sa.Column("reasoning_effort", sa.String(16), server_default=""),
        "max_output_tokens": sa.Column("max_output_tokens", sa.Integer(), server_default="4096"),
        "max_tokens_param": sa.Column("max_tokens_param", sa.String(32), server_default="auto"),
        "timeout_ms": sa.Column("timeout_ms", sa.Integer(), server_default="120000"),
        "max_retries": sa.Column("max_retries", sa.Integer(), server_default="3"),
        "stream_max_retries": sa.Column("stream_max_retries", sa.Integer(), server_default="3"),
        "stream_idle_timeout_ms": sa.Column("stream_idle_timeout_ms", sa.Integer(),
                                            server_default="300000"),
        "supports_temperature": sa.Column("supports_temperature", sa.Boolean(),
                                          server_default=sa.true()),
    })
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "jobs" in inspector.get_table_names() and not _has_index(
        "jobs", "uq_job_owner_idempotency"
    ):
        op.create_index(
            "uq_job_owner_idempotency", "jobs",
            ["owner_id", "idempotency_key"], unique=True,
        )
    if "run_events" in inspector.get_table_names() and not _has_index(
        "run_events", "uq_run_event_sequence"
    ):
        # 历史重复事件保留第一条，随后建立确定性唯一约束。
        bind.execute(sa.text(
            "DELETE FROM run_events WHERE id NOT IN ("
            "SELECT MIN(id) FROM run_events GROUP BY run_id, sequence)"
        ))
        op.create_index(
            "uq_run_event_sequence", "run_events",
            ["run_id", "sequence"], unique=True,
        )


def downgrade() -> None:
    raise RuntimeError("安全迁移不允许自动降级并重新引入已修复缺陷")
