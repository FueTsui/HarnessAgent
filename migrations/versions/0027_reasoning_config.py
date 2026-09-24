"""Freeze explicit provider reasoning capabilities separately from request extras."""
from alembic import op
import sqlalchemy as sa

revision = "0027_reasoning_config"
down_revision = "0026_browser_sessions"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("model_providers")}
    if "reasoning_config" not in columns:
        op.add_column("model_providers", sa.Column("reasoning_config", sa.Text(), nullable=False, server_default="{}"))


def downgrade():
    with op.batch_alter_table("model_providers") as batch:
        batch.drop_column("reasoning_config")
