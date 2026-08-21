"""持久化附件并显式记录 Turn 延续关系。

Revision ID: 0010_persistent_attachments
Revises: 0009_thread_turn_item
"""
import json

from alembic import op
import sqlalchemy as sa

from backend.secret_store import EncryptedText


revision = "0010_persistent_attachments"
down_revision = "0009_thread_turn_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    turn_columns = {
        column["name"] for column in inspector.get_columns("turns")
    }
    if "continuation_of_turn_id" not in turn_columns:
        op.add_column(
            "turns",
            sa.Column("continuation_of_turn_id", sa.String(length=32), nullable=True),
        )
        op.create_index(
            "ix_turns_continuation_of_turn_id",
            "turns",
            ["continuation_of_turn_id"],
        )
        if bind.dialect.name != "sqlite":
            op.create_foreign_key(
                "fk_turns_continuation_of_turn_id",
                "turns",
                "turns",
                ["continuation_of_turn_id"],
                ["id"],
                ondelete="SET NULL",
            )

    if "attachments" not in tables:
        op.create_table(
            "attachments",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("thread_id", sa.String(length=40), nullable=False),
            sa.Column("turn_id", sa.String(length=32), nullable=True),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("storage_name", sa.String(length=255), nullable=False),
            sa.Column("original_name", sa.String(length=255), nullable=False),
            sa.Column(
                "media_type", sa.String(length=128), nullable=False,
                server_default="application/octet-stream",
            ),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="document"),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sha256", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("extracted_text", EncryptedText(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        )
        for column in (
            "thread_id", "turn_id", "owner_id", "storage_name", "kind", "sha256", "created_at",
        ):
            op.create_index(f"ix_attachments_{column}", "attachments", [column])

    # 文档工具只访问每轮隔离工作区中的附件副本，适合安全地加入既有 Agent。
    if "agents" in tables:
        rows = bind.execute(sa.text("SELECT id, builtin_tools FROM agents")).all()
        for agent_id, raw in rows:
            try:
                names = json.loads(raw or "[]")
            except (json.JSONDecodeError, TypeError):
                names = []
            if not isinstance(names, list):
                names = []
            merged = sorted({str(name) for name in names} | {
                "document_inspect", "document_format",
            })
            bind.execute(
                sa.text("UPDATE agents SET builtin_tools=:tools WHERE id=:agent_id"),
                {"tools": json.dumps(merged, ensure_ascii=False), "agent_id": agent_id},
            )


def downgrade() -> None:
    raise RuntimeError("持久附件和 Turn 延续审计数据不允许自动降级移除")
