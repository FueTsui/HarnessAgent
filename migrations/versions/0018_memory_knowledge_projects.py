"""Add reversible memory governance and project defaults/context.

Revision ID: 0018_memory_knowledge_projects
Revises: 0017_task_governance
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_memory_knowledge_projects"
down_revision = "0017_task_governance"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {
        index["name"] for index in sa.inspect(bind).get_indexes(table)
        if index.get("name")
    }


def _add_column(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    if column.name not in _columns(bind, table):
        op.add_column(table, column)


def _create_index(table: str, name: str, columns: list[str]) -> None:
    bind = op.get_bind()
    if name not in _indexes(bind, table):
        op.create_index(name, table, columns)


def _backfill_default_projects(bind) -> None:
    """每个已有用户选定一个默认项目；没有项目时创建，不覆盖已有项目。"""
    tables = set(sa.inspect(bind).get_table_names())
    if not {"users", "projects"}.issubset(tables):
        return
    user_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM users")).all()]
    for user_id in user_ids:
        current = bind.execute(sa.text(
            "SELECT id FROM projects "
            "WHERE user_id=:user_id AND is_default=:is_default ORDER BY id LIMIT 1"
        ), {"user_id": user_id, "is_default": True}).scalar()
        if current is not None:
            continue
        first = bind.execute(sa.text(
            "SELECT id FROM projects WHERE user_id=:user_id "
            "ORDER BY is_archived ASC, id ASC LIMIT 1"
        ), {"user_id": user_id}).scalar()
        if first is not None:
            bind.execute(sa.text(
                "UPDATE projects SET is_default=:is_default WHERE id=:project_id"
            ), {"project_id": first, "is_default": True})
            continue
        bind.execute(sa.text(
            "INSERT INTO projects "
            "(user_id, name, description, context_text, default_agent_id, dataset_ids, "
            "is_default, is_pinned, is_archived, created_at) VALUES "
            "(:user_id, :name, '', '', NULL, '[]', :is_default, :is_pinned, :is_archived, CURRENT_TIMESTAMP)"
        ), {
            "user_id": user_id, "name": "默认项目", "is_default": True,
            "is_pinned": False, "is_archived": False,
        })


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "projects" in tables:
        _add_column("projects", sa.Column(
            "description", sa.Text(), nullable=False, server_default=""
        ))
        _add_column("projects", sa.Column(
            "context_text", sa.Text(), nullable=False, server_default=""
        ))
        # SQLite 不支持 ALTER TABLE ADD CONSTRAINT。存量 SQLite 使用应用层 owner/
        # Agent 校验；新安装由声明式元数据创建完整外键。
        _add_column("projects", sa.Column("default_agent_id", sa.Integer(), nullable=True))
        _add_column("projects", sa.Column(
            "dataset_ids", sa.Text(), nullable=False, server_default="[]"
        ))
        _add_column("projects", sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ))
        _create_index("projects", "ix_projects_default_agent_id", ["default_agent_id"])
        _create_index("projects", "ix_projects_is_default", ["is_default"])

    if "threads" in tables:
        _add_column("threads", sa.Column(
            "memory_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ))
        _add_column("threads", sa.Column(
            "memory_excluded", sa.Boolean(), nullable=False, server_default=sa.false()
        ))
        _add_column("threads", sa.Column("memory_excluded_at", sa.DateTime(), nullable=True))
        _create_index("threads", "ix_threads_memory_enabled", ["memory_enabled"])
        _create_index("threads", "ix_threads_memory_excluded", ["memory_excluded"])

    _backfill_default_projects(bind)


def downgrade() -> None:
    raise RuntimeError("记忆排除墓碑与项目上下文字段不允许自动降级移除")
