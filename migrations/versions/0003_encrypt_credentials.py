"""将历史凭据与执行快照迁移为信封密文。

Revision ID: 0003_encrypt_credentials
Revises: 0002_security_hardening
"""
from alembic import op

from backend.secret_store import migrate_plaintext_secrets

revision = "0003_encrypt_credentials"
down_revision = "0002_security_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrate_plaintext_secrets(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("凭据密文不允许自动降级为明文")
