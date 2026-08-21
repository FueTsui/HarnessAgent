"""个人微信消息渠道：账号路由、加密同步游标与运行状态。

Revision ID: 0013_personal_weixin_channels
Revises: 0012_repair_codex_chatgpt_models
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from backend.secret_store import EncryptedText


revision = "0013_personal_weixin_channels"
down_revision = "0012_repair_codex_chatgpt_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 基线为接管无版本数据库而保留了 create_all 兼容路径；全新数据库可能
    # 已由当前 ORM 带出这些列，因此本增量迁移必须逐项幂等。
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("channels")}
    additions = {
        "account_id": sa.Column("account_id", sa.String(length=128), nullable=False, server_default=""),
        "account_user_id": sa.Column("account_user_id", sa.String(length=128), nullable=False, server_default=""),
        "base_url": sa.Column("base_url", sa.String(length=512), nullable=False, server_default=""),
        "sync_buf": sa.Column("sync_buf", EncryptedText(), nullable=False, server_default=""),
        "connection_status": sa.Column("connection_status", sa.String(length=24), nullable=False, server_default="unbound"),
        "last_error": sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        "last_inbound_at": sa.Column("last_inbound_at", sa.DateTime(), nullable=True),
        "last_outbound_at": sa.Column("last_outbound_at", sa.DateTime(), nullable=True),
    }
    missing = [column for name, column in additions.items() if name not in columns]
    if missing:
        with op.batch_alter_table("channels") as batch:
            for column in missing:
                batch.add_column(column)

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("channels")}
    wanted = {
        "ix_channels_account_id": ["account_id"],
        "ix_channels_account_user_id": ["account_user_id"],
        "ix_channels_connection_status": ["connection_status"],
    }
    with op.batch_alter_table("channels") as batch:
        for name, fields in wanted.items():
            if name not in indexes:
                batch.create_index(name, fields, unique=False)


def downgrade() -> None:
    with op.batch_alter_table("channels") as batch:
        batch.drop_index("ix_channels_connection_status")
        batch.drop_index("ix_channels_account_user_id")
        batch.drop_index("ix_channels_account_id")
        for name in (
            "last_outbound_at", "last_inbound_at", "last_error", "connection_status",
            "sync_buf", "base_url", "account_user_id", "account_id",
        ):
            batch.drop_column(name)
