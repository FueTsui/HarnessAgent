"""Add scoped custom services and execution records."""
from alembic import op
import sqlalchemy as sa
revision = "0023_service_registry"
down_revision = "0022_guardrail_policies"
branch_labels = None
depends_on = None

def upgrade():
    if not sa.inspect(op.get_bind()).has_table("custom_services"):
        op.create_table("custom_services", sa.Column("id",sa.Integer(),primary_key=True),
            sa.Column("name",sa.String(128),nullable=False), sa.Column("description",sa.Text(),nullable=False),
            sa.Column("kind",sa.String(16),nullable=False), sa.Column("config",sa.Text(),nullable=False),
            sa.Column("input_fields",sa.Text(),nullable=False), sa.Column("agent_ids",sa.Text(),nullable=False),
            sa.Column("enabled",sa.Boolean(),nullable=False), sa.Column("is_public",sa.Boolean(),nullable=False),
            sa.Column("created_by",sa.Integer(),sa.ForeignKey("users.id"),nullable=False),
            sa.Column("revision",sa.Integer(),nullable=False), sa.Column("updated_at",sa.DateTime(),nullable=False))
        op.create_index("ix_custom_services_created_by","custom_services",["created_by"])
    if not sa.inspect(op.get_bind()).has_table("service_runs"):
        op.create_table("service_runs", sa.Column("id",sa.Integer(),primary_key=True),
            sa.Column("service_id",sa.Integer(),nullable=False), sa.Column("service_name",sa.String(128),nullable=False),
            sa.Column("owner_id",sa.Integer(),sa.ForeignKey("users.id"),nullable=False),
            sa.Column("status",sa.String(16),nullable=False), sa.Column("duration_ms",sa.Integer(),nullable=False),
            sa.Column("result",sa.Text(),nullable=False), sa.Column("created_at",sa.DateTime(),nullable=False))
        op.create_index("ix_service_runs_owner_id","service_runs",["owner_id"])
        op.create_index("ix_service_runs_service_id","service_runs",["service_id"])


def downgrade():
    op.drop_table("service_runs")
    op.drop_table("custom_services")
