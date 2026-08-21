"""Add release reliability constraints and resource governance tables.

Revision ID: 0019_release_reliability
Revises: 0018_memory_knowledge_projects
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid

from alembic import op
import sqlalchemy as sa


revision = "0019_release_reliability"
down_revision = "0018_memory_knowledge_projects"
branch_labels = None
depends_on = None

VERSIONS_KEY = "resource_versions_v1"
STATE_KEY = "resource_governance_state_v1"
_FORBIDDEN = {
    "authorization", "headers", "header", "token", "access_token",
    "refresh_token", "id_token", "secret", "password", "api_key",
    "apikey", "credential", "credentials", "reasoning", "raw_reasoning",
    "url", "uri", "endpoint",
}


def _sanitize(value):
    if isinstance(value, dict):
        clean = {}
        for raw_key, item in value.items():
            key = str(raw_key)[:128]
            normalized = key.lower().replace("-", "_")
            if normalized in _FORBIDDEN or any(
                marker in normalized
                for marker in (
                    "password", "secret", "credential", "authorization",
                    "token", "api_key", "apikey",
                )
            ):
                continue
            clean[key] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:500]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]


def _resource_snapshot(resource_type: str, snapshot: dict) -> dict:
    clean = _sanitize(snapshot if isinstance(snapshot, dict) else {})
    if resource_type == "mcp":
        allowed = {
            "name", "description_sha256", "transport", "risk_policy",
            "enabled", "is_public", "catalog_hash",
        }
        return {key: clean[key] for key in allowed if key in clean}
    allowed = {
        "name", "description_sha256", "instructions_sha256",
        "resources", "enabled", "is_public",
    }
    selected = {key: clean[key] for key in allowed if key in clean}
    if isinstance(selected.get("resources"), list):
        selected["resources"] = [
            {
                key: item[key]
                for key in ("name", "sha256", "size_bytes") if key in item
            }
            for item in selected["resources"] if isinstance(item, dict)
        ]
    return selected


def _parse_created_at(value) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _load_setting(bind, key: str, default):
    inspector = sa.inspect(bind)
    if "app_settings" not in inspector.get_table_names():
        return default
    raw = bind.execute(
        sa.text("SELECT value FROM app_settings WHERE key=:key"), {"key": key}
    ).scalar()
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _backfill_legacy_governance(bind) -> None:
    """Idempotently copy sanitized legacy JSON while retaining AppSetting rows."""
    counters: dict[tuple[str, int], int] = {}
    for position, item in enumerate(_load_setting(bind, VERSIONS_KEY, [])):
        if not isinstance(item, dict):
            continue
        resource_type = str(item.get("resource_type") or "")[:16]
        try:
            resource_id = int(item.get("resource_id") or 0)
        except (TypeError, ValueError):
            continue
        if resource_type not in {"skill", "mcp"} or resource_id <= 0:
            continue
        key = (resource_type, resource_id)
        counters[key] = counters.get(key, 0) + 1
        version = counters[key]
        exists = bind.execute(sa.text(
            "SELECT 1 FROM resource_versions WHERE resource_type=:resource_type "
            "AND resource_id=:resource_id AND version=:version"
        ), {"resource_type": resource_type, "resource_id": resource_id, "version": version}).first()
        if exists:
            continue
        snapshot = _resource_snapshot(
            resource_type,
            item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {},
        )
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        raw_id = str(item.get("id") or "")[:64]
        row_id = raw_id or uuid.uuid5(
            uuid.NAMESPACE_URL, f"legacy:{resource_type}:{resource_id}:{version}:{position}"
        ).hex
        if bind.execute(sa.text(
            "SELECT 1 FROM resource_versions WHERE id=:id"
        ), {"id": row_id}).first():
            row_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"legacy-duplicate:{resource_type}:{resource_id}:{version}:{position}"
            ).hex
        actor_id = item.get("actor_id")
        try:
            actor_id = int(actor_id) if actor_id is not None else None
        except (TypeError, ValueError):
            actor_id = None
        bind.execute(sa.text(
            "INSERT INTO resource_versions "
            "(id, resource_type, resource_id, version, content_hash, change, actor_id, created_at, snapshot) "
            "VALUES (:id, :resource_type, :resource_id, :version, :content_hash, :change, :actor_id, :created_at, :snapshot)"
        ), {
            "id": row_id, "resource_type": resource_type, "resource_id": resource_id,
            "version": version, "content_hash": digest,
            "change": str(item.get("change") or "legacy_import")[:64],
            "actor_id": actor_id, "created_at": _parse_created_at(item.get("created_at")),
            "snapshot": encoded,
        })

    for raw_key, item in _load_setting(bind, STATE_KEY, {}).items():
        if not isinstance(item, dict):
            continue
        resource_type, separator, raw_id = str(raw_key).partition(":")
        try:
            resource_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if not separator or resource_type not in {"skill", "mcp"} or resource_id <= 0:
            continue
        exists = bind.execute(sa.text(
            "SELECT 1 FROM resource_governance_states "
            "WHERE resource_type=:resource_type AND resource_id=:resource_id"
        ), {"resource_type": resource_type, "resource_id": resource_id}).first()
        if exists:
            continue
        acknowledged_by = item.get("acknowledged_by")
        try:
            acknowledged_by = int(acknowledged_by) if acknowledged_by is not None else None
        except (TypeError, ValueError):
            acknowledged_by = None
        try:
            tool_count = max(0, int(item.get("tool_count") or 0))
        except (TypeError, ValueError):
            tool_count = 0
        bind.execute(sa.text(
            "INSERT INTO resource_governance_states "
            "(resource_type, resource_id, catalog_hash, review_required, tool_count, "
            "acknowledged_by, acknowledged_catalog_hash, updated_at) VALUES "
            "(:resource_type, :resource_id, :catalog_hash, :review_required, :tool_count, "
            ":acknowledged_by, :acknowledged_catalog_hash, :updated_at)"
        ), {
            "resource_type": resource_type, "resource_id": resource_id,
            "catalog_hash": str(item.get("catalog_hash") or "")[:64],
            "review_required": bool(item.get("review_required")),
            "tool_count": tool_count,
            "acknowledged_by": acknowledged_by,
            "acknowledged_catalog_hash": str(item.get("acknowledged_catalog_hash") or "")[:64],
            "updated_at": _parse_created_at(item.get("updated_at")),
        })


def _create_default_project_index(bind) -> None:
    inspector = sa.inspect(bind)
    if "projects" not in inspector.get_table_names():
        return
    # Deterministically retain the oldest default and clear any race-created duplicates.
    duplicates = bind.execute(sa.text(
        "SELECT user_id FROM projects WHERE is_default=:is_default "
        "GROUP BY user_id HAVING COUNT(*) > 1"
    ), {"is_default": True}).scalars().all()
    for user_id in duplicates:
        keep = bind.execute(sa.text(
            "SELECT id FROM projects WHERE user_id=:user_id AND is_default=:is_default "
            "ORDER BY id LIMIT 1"
        ), {"user_id": user_id, "is_default": True}).scalar_one()
        bind.execute(sa.text(
            "UPDATE projects SET is_default=:false_value "
            "WHERE user_id=:user_id AND is_default=:true_value AND id<>:keep"
        ), {
            "false_value": False, "true_value": True,
            "user_id": user_id, "keep": keep,
        })
    indexes = {item.get("name") for item in sa.inspect(bind).get_indexes("projects")}
    if "uq_projects_one_default_per_user" not in indexes:
        op.create_index(
            "uq_projects_one_default_per_user", "projects", ["user_id"],
            unique=True,
            sqlite_where=sa.text("is_default = 1"),
            postgresql_where=sa.text("is_default IS TRUE"),
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "resource_versions" not in tables:
        op.create_table(
            "resource_versions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("resource_type", sa.String(length=16), nullable=False),
            sa.Column("resource_id", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("change", sa.String(length=64), nullable=False, server_default="updated"),
            sa.Column("actor_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("snapshot", sa.Text(), nullable=False, server_default="{}"),
            sa.UniqueConstraint(
                "resource_type", "resource_id", "version",
                name="uq_resource_version_number",
            ),
        )
        op.create_index(
            "ix_resource_versions_resource", "resource_versions",
            ["resource_type", "resource_id"], unique=False,
        )
    elif "ix_resource_versions_resource" not in {
        item.get("name") for item in sa.inspect(bind).get_indexes("resource_versions")
    }:
        op.create_index(
            "ix_resource_versions_resource", "resource_versions",
            ["resource_type", "resource_id"], unique=False,
        )
    if "resource_governance_states" not in tables:
        op.create_table(
            "resource_governance_states",
            sa.Column("resource_type", sa.String(length=16), primary_key=True),
            sa.Column("resource_id", sa.Integer(), primary_key=True),
            sa.Column("catalog_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("tool_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("acknowledged_by", sa.Integer(), nullable=True),
            sa.Column("acknowledged_catalog_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    _backfill_legacy_governance(bind)
    _create_default_project_index(bind)


def downgrade() -> None:
    raise RuntimeError("资源版本审计表和默认项目唯一性不允许自动降级移除")
