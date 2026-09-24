"""Persist guardrail review notifications and decisions."""
from alembic import op
import sqlalchemy as sa

revision = "0024_guardrail_reviews"
down_revision = "0023_service_registry"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("guardrail_reviews"):
        return
    op.create_table("guardrail_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.create_index("ix_guardrail_reviews_user_id", "guardrail_reviews", ["user_id"])
    op.create_index("ix_guardrail_reviews_status", "guardrail_reviews", ["status"])


def downgrade():
    op.drop_table("guardrail_reviews")
