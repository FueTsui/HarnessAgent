"""持久化运行中消息引导。

Revision ID: 0008_job_guidance
Revises: 0007_persist_partial_stream
"""
from alembic import op
import sqlalchemy as sa


revision = "0008_job_guidance"
down_revision = "0007_persist_partial_stream"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "job_guidance" in inspector.get_table_names():
        return
    op.create_table(
        "job_guidance",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_job_guidance_job_id", "job_guidance", ["job_id"])
    op.create_index("ix_job_guidance_owner_id", "job_guidance", ["owner_id"])
    op.create_index("ix_job_guidance_status", "job_guidance", ["status"])
    op.create_index("ix_job_guidance_created_at", "job_guidance", ["created_at"])


def downgrade() -> None:
    raise RuntimeError("运行中消息引导记录不允许自动降级移除")
