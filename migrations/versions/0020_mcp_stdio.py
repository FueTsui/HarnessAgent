"""Add root-authorized local MCP stdio configuration.

Revision ID: 0020_mcp_stdio
Revises: 0019_release_reliability
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_mcp_stdio"
down_revision = "0019_release_reliability"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("mcp_servers")}
    for name, default in (("command", ""), ("args", "[]"), ("env", "{}"), ("cwd", "")):
        if name not in columns:
            op.add_column("mcp_servers", sa.Column(name, sa.Text(), nullable=False, server_default=default))
    if "stdio_authorized" not in columns:
        op.add_column("mcp_servers", sa.Column("stdio_authorized", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("mcp_servers") as batch:
        for name in ("stdio_authorized", "cwd", "env", "args", "command"):
            batch.drop_column(name)
