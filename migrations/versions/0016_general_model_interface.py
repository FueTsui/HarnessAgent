"""以通用模型元数据替换文本/视觉/原生图片专用配置。

Revision ID: 0016_general_model_interface
Revises: 0015_provider_image_capabilities

这是用户明确要求的不兼容升级：连接、凭据和传输参数保留，但旧模型选择
不会映射到新模型字段。升级后必须为每个 Provider 重新选择模型并声明能力。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_general_model_interface"
down_revision = "0015_provider_image_capabilities"
branch_labels = None
depends_on = None


NEW_COLUMNS = {
    "model_id": sa.Column(
        "model_id", sa.String(length=256), nullable=False, server_default=""
    ),
    "model_name": sa.Column(
        "model_name", sa.String(length=256), nullable=False, server_default=""
    ),
    "model_reasoning": sa.Column(
        "model_reasoning", sa.Boolean(), nullable=False, server_default=sa.false()
    ),
    "model_input": sa.Column(
        "model_input", sa.Text(), nullable=False, server_default='["text"]'
    ),
    "context_window": sa.Column(
        "context_window", sa.Integer(), nullable=False, server_default="0"
    ),
    "max_tokens": sa.Column(
        "max_tokens", sa.Integer(), nullable=False, server_default="8192"
    ),
}

REMOVED_COLUMNS = (
    "text_model",
    "vision_model",
    "image_generation_mode",
    "image_model",
    "supports_image_edit",
    "context_tokens",
    "max_output_tokens",
)


def _columns(bind) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(bind).get_columns("model_providers")
    }


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind)
    for name, column in NEW_COLUMNS.items():
        if name not in columns:
            op.add_column("model_providers", column)

    # SQLite 3.35+ 支持原生 DROP COLUMN，不会重建被 agents 等表引用的
    # model_providers，因此外键开启时仍可安全执行。
    columns = _columns(bind)
    for name in REMOVED_COLUMNS:
        if name in columns:
            op.drop_column("model_providers", name)


def downgrade() -> None:
    raise RuntimeError("通用模型接口是不兼容升级，不支持恢复已删除的旧模型配置")
