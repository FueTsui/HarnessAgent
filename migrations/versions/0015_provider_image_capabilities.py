"""为模型提供商增加原生图片生成与编辑能力声明。

Revision ID: 0015_provider_image_capabilities
Revises: 0014_token_usage_limits
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_provider_image_capabilities"
down_revision = "0014_token_usage_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("model_providers")
    }
    additions = {
        "image_generation_mode": sa.Column(
            "image_generation_mode", sa.String(length=24),
            nullable=False, server_default="disabled",
        ),
        "image_model": sa.Column(
            "image_model", sa.String(length=128),
            nullable=False, server_default="",
        ),
        "supports_image_edit": sa.Column(
            "supports_image_edit", sa.Boolean(),
            nullable=False, server_default=sa.false(),
        ),
    }
    # 这里只增加带默认值的新列，SQLite 原生 ADD COLUMN 已足够。不要使用 batch
    # 重建 model_providers：生产库中 agents 等表会通过外键引用它，启用
    # PRAGMA foreign_keys 时 DROP TABLE 会被正确拒绝。
    for name, column in additions.items():
        if name not in columns:
            op.add_column("model_providers", column)


def downgrade() -> None:
    raise RuntimeError("图片能力配置已进入执行快照，不允许自动降级移除")
