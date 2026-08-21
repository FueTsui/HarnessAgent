"""修复 Codex ChatGPT 登录提供商的类型、线路与旧模型名。

Revision ID: 0012_repair_codex_chatgpt_models
Revises: 0011_audit_remediation
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_repair_codex_chatgpt_models"
down_revision = "0011_audit_remediation"
branch_labels = None
depends_on = None


def _repair_codex_chatgpt_providers(bind) -> None:
    inspector = sa.inspect(bind)
    if "model_providers" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("model_providers")}
    if not {"base_url", "provider_type", "wire_api", "text_model"}.issubset(columns):
        return
    bind.execute(sa.text(
        "UPDATE model_providers SET "
        "provider_type='chatgpt', wire_api='responses', "
        "text_model=CASE "
        "WHEN text_model IS NULL OR trim(text_model)='' "
        "OR text_model IN ('gpt-5','gpt-5-codex','gpt-5-mini','gpt-5.1','gpt-5.1-codex') "
        "THEN 'gpt-5.6-sol' ELSE text_model END "
        "WHERE lower(rtrim(base_url, '/'))='https://chatgpt.com/backend-api/codex'"
    ))


def upgrade() -> None:
    _repair_codex_chatgpt_providers(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Codex ChatGPT 提供商兼容性修复不允许自动回退")
