"""加入 Worker 心跳表与资源治理基线。

Revision ID: 0005_resource_governance
Revises: 0004_improvement_gate
"""
from alembic import op

from backend.database import Base
import backend.models  # noqa: F401

revision = "0005_resource_governance"
down_revision = "0004_improvement_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["worker_heartbeats"].create(
        bind=op.get_bind(), checkfirst=True
    )


def downgrade() -> None:
    raise RuntimeError("资源治理表不允许自动降级移除")
