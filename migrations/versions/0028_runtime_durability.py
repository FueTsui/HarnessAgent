"""Private execution checkpoints, invocation ledger, and exact-bound approvals."""
from alembic import op
import sqlalchemy as sa

revision = "0028_runtime_durability"
down_revision = "0027_reasoning_config"
branch_labels = None
depends_on = None


def upgrade():
    columns = {row["name"] for row in sa.inspect(op.get_bind()).get_columns("tool_approvals")}
    for name, length in (("invocation_id", 32), ("arguments_digest", 64), ("capability_revision", 128)):
        if name not in columns:
            op.add_column("tool_approvals", sa.Column(name, sa.String(length), nullable=False, server_default=""))
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "runtime_checkpoints" not in tables:
        op.create_table(
            "runtime_checkpoints",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("run_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("execution_key", sa.String(160), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("run_id", "execution_key", name="uq_runtime_checkpoint_execution"),
        )
        op.create_index("ix_runtime_checkpoints_run_id", "runtime_checkpoints", ["run_id"])
        op.create_index("ix_runtime_checkpoints_owner_id", "runtime_checkpoints", ["owner_id"])
    if "tool_invocations" not in tables:
        op.create_table(
            "tool_invocations",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("run_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("execution_key", sa.String(160), nullable=False),
            sa.Column("call_id", sa.String(160), nullable=False),
            sa.Column("tool_name", sa.String(160), nullable=False),
            sa.Column("arguments_digest", sa.String(64), nullable=False),
            sa.Column("capability_revision", sa.String(128), nullable=False),
            sa.Column("effect", sa.String(32), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("arguments", sa.Text(), nullable=False),
            sa.Column("result", sa.Text(), nullable=False),
            sa.Column("lease_token", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("run_id", "execution_key", "call_id", name="uq_tool_invocation_call"),
        )
        for column in ("run_id", "owner_id", "state"):
            op.create_index(f"ix_tool_invocations_{column}", "tool_invocations", [column])


def downgrade():
    op.drop_table("tool_invocations")
    op.drop_table("runtime_checkpoints")
    with op.batch_alter_table("tool_approvals") as batch:
        for name in ("capability_revision", "arguments_digest", "invocation_id"):
            batch.drop_column(name)
