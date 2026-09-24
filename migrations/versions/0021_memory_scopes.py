"""Persistent account-owned memory with explicit scope targets.

Revision ID: 0021_memory_scopes
Revises: 0020_mcp_stdio
"""
from alembic import op

revision = "0021_memory_scopes"
down_revision = "0020_mcp_stdio"
branch_labels = None
depends_on = None


def upgrade():
    from backend.memory_store import MemoryEntry
    MemoryEntry.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    from backend.memory_store import MemoryEntry
    MemoryEntry.__table__.drop(bind=op.get_bind(), checkfirst=True)
