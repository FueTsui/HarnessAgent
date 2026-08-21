"""保留 Token 历史并增加用户周、月、总量限额。

Revision ID: 0014_token_usage_limits
Revises: 0013_personal_weixin_channels
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_token_usage_limits"
down_revision = "0013_personal_weixin_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "user_token_limits" not in tables:
        op.create_table(
            "user_token_limits",
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("weekly_limit", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("monthly_limit", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_limit", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("weekly_reset_at", sa.DateTime(), nullable=True),
            sa.Column("monthly_reset_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("user_id"),
        )

    token_columns = {
        column["name"]: column
        for column in sa.inspect(bind).get_columns("token_usages")
    }
    needs_username = "username" not in token_columns
    needs_nullable_user = not bool(token_columns["user_id"].get("nullable"))
    if needs_username or needs_nullable_user:
        # SQLite 由 batch 模式安全重建表；其它数据库直接 ALTER。原 FK 保持 NO ACTION，
        # 删除用户前由业务事务先把 user_id 置空，避免依赖数据库级联差异。
        with op.batch_alter_table("token_usages") as batch:
            if needs_username:
                batch.add_column(
                    sa.Column("username", sa.String(length=64), nullable=False, server_default="")
                )
            if needs_nullable_user:
                batch.alter_column(
                    "user_id", existing_type=sa.Integer(), nullable=True
                )

    # 既有用量补齐用户名快照；之后即使用户被删除，历史账本仍可识别原归属。
    op.execute(sa.text(
        "UPDATE token_usages SET username=("
        "SELECT users.username FROM users WHERE users.id=token_usages.user_id"
        ") WHERE COALESCE(username, '')=''"
    ))


def downgrade() -> None:
    raise RuntimeError("Token 用量历史与限额配置不允许自动降级移除")
