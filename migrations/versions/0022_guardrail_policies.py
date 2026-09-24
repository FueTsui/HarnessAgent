"""Persist named guardrail policies and reusable blocklists."""
from alembic import op
import sqlalchemy as sa

revision = "0022_guardrail_policies"
down_revision = "0021_memory_scopes"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for name in ("guardrail_policies", "guardrail_blocklists"):
        if sa.inspect(bind).has_table(name):
            continue
        specific = (
            [sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("config", sa.Text(), nullable=False, server_default="{}")]
            if name == "guardrail_policies" else
            [sa.Column("entries", sa.Text(), nullable=False, server_default="[]")]
        )
        op.create_table(
            name,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            *specific,
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(f"ix_{name}_created_by", name, ["created_by"])


def downgrade():
    for name in ("guardrail_policies", "guardrail_blocklists"):
        op.drop_table(name)
