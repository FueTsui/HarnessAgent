"""记录改进提案审批人，支持职责分离。

Revision ID: 0004_improvement_gate
Revises: 0003_encrypt_credentials
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_improvement_gate"
down_revision = "0003_encrypt_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("improvement_proposals")
    }
    if "approved_by" not in columns:
        op.add_column(
            "improvement_proposals",
            sa.Column("approved_by", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    raise RuntimeError("职责分离字段不允许自动降级移除")
