"""引入 Project / Thread / Turn / Item 核心任务模型。

Revision ID: 0009_thread_turn_item
Revises: 0008_job_guidance
"""
from alembic import op
import sqlalchemy as sa

from backend.secret_store import EncryptedText


revision = "0009_thread_turn_item"
down_revision = "0008_job_guidance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "threads" not in tables:
        op.create_table(
            "threads",
            sa.Column("id", sa.String(length=40), primary_key=True),
            sa.Column("project_id", sa.Integer(), nullable=True),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
            sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("context_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        )
        for column in (
            "project_id", "owner_id", "agent_id", "status", "is_pinned",
            "is_archived", "created_at", "updated_at",
        ):
            op.create_index(f"ix_threads_{column}", "threads", [column])

    if "turns" not in tables:
        op.create_table(
            "turns",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("thread_id", sa.String(length=40), nullable=False),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=True),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column("source", sa.String(length=16), nullable=False, server_default="web"),
            sa.Column("input", sa.Text(), nullable=False, server_default=""),
            sa.Column("final_output", sa.Text(), nullable=False, server_default=""),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("execution_snapshot", EncryptedText(), nullable=False),
            sa.Column("item_sequence", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("thread_id", "sequence", name="uq_turn_thread_sequence"),
        )
        for column in (
            "thread_id", "owner_id", "agent_id", "status", "created_at",
        ):
            op.create_index(f"ix_turns_{column}", "turns", [column])

    if "items" not in tables:
        op.create_table(
            "items",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("thread_id", sa.String(length=40), nullable=False),
            sa.Column("turn_id", sa.String(length=32), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False, server_default=""),
            sa.Column("name", sa.String(length=96), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="completed"),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("turn_id", "sequence", name="uq_item_turn_sequence"),
        )
        for column in ("thread_id", "turn_id", "kind", "created_at"):
            op.create_index(f"ix_items_{column}", "items", [column])

    artifact_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("artifacts")
    }
    if "turn_id" not in artifact_columns:
        op.add_column("artifacts", sa.Column("turn_id", sa.String(length=32), nullable=True))
        op.create_index("ix_artifacts_turn_id", "artifacts", ["turn_id"])


def downgrade() -> None:
    raise RuntimeError("Thread / Turn / Item 审计数据不允许自动降级移除")
