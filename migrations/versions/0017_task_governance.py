"""Harden parent-child jobs, retries, Cron isolation, and scheduler health.

Revision ID: 0017_task_governance
Revises: 0016_general_model_interface
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_task_governance"
down_revision = "0016_general_model_interface"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    job_columns = _columns(bind, "jobs")
    if "next_attempt_at" not in job_columns:
        op.add_column("jobs", sa.Column("next_attempt_at", sa.DateTime(), nullable=True))
    if "priority" not in job_columns:
        op.add_column(
            "jobs",
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        )
    if "error_class" not in job_columns:
        op.add_column(
            "jobs",
            sa.Column("error_class", sa.String(length=32), nullable=False, server_default=""),
        )
    job_indexes = _indexes(bind, "jobs")
    if "ix_jobs_next_attempt_at" not in job_indexes:
        op.create_index("ix_jobs_next_attempt_at", "jobs", ["next_attempt_at"])
    if "ix_jobs_priority" not in job_indexes:
        op.create_index("ix_jobs_priority", "jobs", ["priority"])

    task_columns = _columns(bind, "scheduled_tasks")
    if "last_error" not in task_columns:
        op.add_column(
            "scheduled_tasks",
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        )
    if "last_dispatch_status" not in task_columns:
        op.add_column(
            "scheduled_tasks",
            sa.Column(
                "last_dispatch_status", sa.String(length=24),
                nullable=False, server_default="scheduled",
            ),
        )
    if "consecutive_failures" not in task_columns:
        op.add_column(
            "scheduled_tasks",
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        )
    if "retry_at" not in task_columns:
        op.add_column("scheduled_tasks", sa.Column("retry_at", sa.DateTime(), nullable=True))
    task_indexes = _indexes(bind, "scheduled_tasks")
    if "ix_scheduled_tasks_retry_at" not in task_indexes:
        op.create_index("ix_scheduled_tasks_retry_at", "scheduled_tasks", ["retry_at"])

    if "scheduler_heartbeats" not in tables:
        op.create_table(
            "scheduler_heartbeats",
            sa.Column("scheduler_id", sa.String(length=128), nullable=False),
            sa.Column("last_seen", sa.DateTime(), nullable=False),
            sa.Column("last_successful_dispatch_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.PrimaryKeyConstraint("scheduler_id"),
        )
        op.create_index(
            "ix_scheduler_heartbeats_last_seen",
            "scheduler_heartbeats", ["last_seen"],
        )


def downgrade() -> None:
    raise RuntimeError("任务治理审计字段不允许自动降级移除")
