"""建立当前 Harness 架构基线。

Revision ID: 0001_baseline
Revises:
"""
from alembic import op

from backend.database import Base
import backend.models  # noqa: F401

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 新安装从完整声明式元数据建库；已有部署由运行器 stamp 到本基线，
    # 再通过后续增量迁移补齐字段，避免对生产表重复 CREATE。
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError("基线迁移不允许自动降级删除全部业务数据")
