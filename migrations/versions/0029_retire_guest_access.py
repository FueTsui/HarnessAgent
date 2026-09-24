"""Retire visitor authentication and personal model connections, preserving history."""
from alembic import op
import sqlalchemy as sa

revision = "0029_retire_guest_access"
down_revision = "0028_runtime_durability"
branch_labels = None
depends_on = None


def upgrade():
    db = op.get_bind()
    users = sa.table("users", sa.column("id"), sa.column("role"),
                     sa.column("is_active"), sa.column("token_version"))
    guests = sa.select(users.c.id).where(users.c.role == "guest")
    sessions = sa.table("auth_sessions", sa.column("user_id"))
    db.execute(sessions.delete().where(sessions.c.user_id.in_(guests)))
    db.execute(users.update().where(users.c.role == "guest").values(
        is_active=False, token_version=sa.func.coalesce(users.c.token_version, 0) + 1))
    providers = sa.table("model_providers", sa.column("created_by"), sa.column("name"),
                         sa.column("enabled"), sa.column("is_public"))
    db.execute(providers.update().where(sa.or_(
        providers.c.created_by.in_(guests),
        providers.c.name.startswith("__personal_model_", autoescape=True),
    )).values(enabled=False, is_public=False))
    agents = sa.table("agents", sa.column("created_by"), sa.column("enabled"))
    db.execute(agents.update().where(agents.c.created_by.in_(guests)).values(enabled=False))


def downgrade():
    # Retired identities/credentials must not be silently reactivated by rollback.
    pass
