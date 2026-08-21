"""将限流与渠道去重状态迁入共享数据库。

Revision ID: 0006_shared_edge_state
Revises: 0005_resource_governance
"""
from alembic import op

from backend.database import Base
import backend.models  # noqa: F401

revision = "0006_shared_edge_state"
down_revision = "0005_resource_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("rate_limit_buckets", "channel_dispatches"):
        Base.metadata.tables[name].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("共享边缘状态表不允许自动降级移除")
