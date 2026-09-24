"""Persist account preferences independently of browser storage."""
from alembic import op
import sqlalchemy as sa

revision = "0025_user_preferences"
down_revision = "0024_guardrail_reviews"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "preferences" not in columns:
        op.add_column("users", sa.Column("preferences", sa.Text(), nullable=False, server_default="{}"))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("preferences")
