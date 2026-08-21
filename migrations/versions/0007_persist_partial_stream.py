"""持久化跨进程流式部分答案。

Revision ID: 0007_persist_partial_stream
Revises: 0006_shared_edge_state
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_persist_partial_stream"
down_revision = "0006_shared_edge_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("jobs")}
    if "partial_result" not in columns:
        op.add_column(
            "jobs",
            sa.Column("partial_result", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    raise RuntimeError("跨进程流式状态不允许自动降级移除")
