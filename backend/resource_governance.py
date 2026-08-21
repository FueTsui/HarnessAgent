"""Database-backed lifecycle metadata for Skills and MCP servers.

Version rows are append-only and snapshots are sanitized before hashing/storage.
Mutable MCP catalog review state is one row per resource and uses atomic upserts,
so unrelated resources no longer overwrite one shared AppSetting JSON document.
The legacy AppSetting rows remain read-only historical reconciliation evidence.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import ResourceGovernanceState, ResourceVersion


VERSIONS_KEY = "resource_versions_v1"
STATE_KEY = "resource_governance_state_v1"

_FORBIDDEN_KEYS = {
    "authorization", "headers", "header", "token", "access_token",
    "refresh_token", "id_token", "secret", "password", "api_key",
    "apikey", "credential", "credentials", "reasoning", "raw_reasoning",
    "url", "uri", "endpoint",
}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def sanitize_snapshot(value):
    """Return bounded JSON data with credential-bearing fields removed."""
    if isinstance(value, dict):
        clean = {}
        for raw_key, item in value.items():
            key = str(raw_key)[:128]
            normalized = key.lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or any(
                marker in normalized
                for marker in (
                    "password", "secret", "credential", "authorization",
                    "token", "api_key", "apikey",
                )
            ):
                continue
            clean[key] = sanitize_snapshot(item)
        return clean
    if isinstance(value, list):
        return [sanitize_snapshot(item) for item in value[:500]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]


def _resource_snapshot(resource_type: str | None, snapshot: dict) -> dict:
    clean = sanitize_snapshot(snapshot if isinstance(snapshot, dict) else {})
    if resource_type == "mcp":
        allowed = {
            "name", "description_sha256", "transport", "risk_policy",
            "enabled", "is_public", "catalog_hash",
        }
        return {key: clean[key] for key in allowed if key in clean}
    if resource_type == "skill":
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
    return clean


def _encoded_snapshot(
    snapshot: dict, resource_type: str | None = None
) -> tuple[dict, str, str]:
    clean = _resource_snapshot(resource_type, snapshot)
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return clean, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def content_hash(snapshot: dict) -> str:
    return _encoded_snapshot(snapshot)[2]


def _version_dict(row: ResourceVersion) -> dict:
    try:
        snapshot = json.loads(row.snapshot or "{}")
    except (json.JSONDecodeError, TypeError):
        snapshot = {}
    created = row.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    return {
        "id": row.id,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "version": row.version,
        "content_hash": row.content_hash,
        "change": row.change,
        "actor_id": row.actor_id,
        "created_at": created.isoformat() if created is not None else "",
        "snapshot": snapshot if isinstance(snapshot, dict) else {},
    }


def versions(db: Session, resource_type: str, resource_id: int) -> list[dict]:
    rows = db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == str(resource_type),
            ResourceVersion.resource_id == int(resource_id),
        ).order_by(ResourceVersion.version, ResourceVersion.id)
    ).scalars().all()
    return [_version_dict(row) for row in rows]


def record_version(
    db: Session,
    resource_type: str,
    resource_id: int,
    snapshot: dict,
    *,
    actor_id: int | None,
    change: str,
) -> dict:
    """Append one immutable version, retrying a concurrent number collision."""
    resource_type = str(resource_type)[:16]
    resource_id = int(resource_id)
    clean, encoded, digest = _encoded_snapshot(snapshot, resource_type)
    for _attempt in range(5):
        latest = db.execute(
            select(ResourceVersion).where(
                ResourceVersion.resource_type == resource_type,
                ResourceVersion.resource_id == resource_id,
            ).order_by(ResourceVersion.version.desc(), ResourceVersion.id.desc()).limit(1)
        ).scalar_one_or_none()
        if latest is not None and latest.content_hash == digest:
            return _version_dict(latest)
        next_version = (latest.version if latest is not None else 0) + 1
        try:
            with db.begin_nested():
                row = ResourceVersion(
                    id=uuid.uuid4().hex,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    version=next_version,
                    content_hash=digest,
                    change=str(change or "updated")[:64],
                    actor_id=int(actor_id) if actor_id is not None else None,
                    created_at=_utcnow(),
                    snapshot=encoded,
                )
                db.add(row)
                db.flush()
            return {**_version_dict(row), "snapshot": clean}
        except IntegrityError:
            continue
    raise RuntimeError("资源版本并发写入冲突，请重试")


def public_lifecycle(db: Session, resource_type: str, resource_id: int) -> dict:
    latest = db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == str(resource_type),
            ResourceVersion.resource_id == int(resource_id),
        ).order_by(ResourceVersion.version.desc(), ResourceVersion.id.desc()).limit(1)
    ).scalar_one_or_none()
    public = _version_dict(latest) if latest is not None else {}
    return {
        "version": int(public.get("version") or 0),
        "content_hash": str(public.get("content_hash") or ""),
        "last_change": str(public.get("change") or ""),
        "versioned_at": str(public.get("created_at") or ""),
    }


def _state_dict(row: ResourceGovernanceState | None) -> dict:
    if row is None:
        return {}
    updated = row.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=datetime.timezone.utc)
    return {
        "catalog_hash": row.catalog_hash or "",
        "review_required": bool(row.review_required),
        "tool_count": int(row.tool_count or 0),
        "acknowledged_by": row.acknowledged_by,
        "acknowledged_catalog_hash": row.acknowledged_catalog_hash or "",
        "updated_at": updated.isoformat() if updated is not None else "",
    }


def resource_state(db: Session, resource_type: str, resource_id: int) -> dict:
    return _state_dict(db.get(
        ResourceGovernanceState, (str(resource_type), int(resource_id))
    ))


def update_resource_state(db: Session, resource_type: str, resource_id: int, **values) -> dict:
    """Atomically merge catalog state without replacing unrelated resources."""
    resource_type = str(resource_type)[:16]
    resource_id = int(resource_id)
    now = _utcnow()
    payload = {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "catalog_hash": str(values.get("catalog_hash") or "")[:64],
        "review_required": bool(values.get("review_required", False)),
        "tool_count": int(values.get("tool_count") or 0),
        "acknowledged_by": values.get("acknowledged_by"),
        "acknowledged_catalog_hash": str(
            values.get("acknowledged_catalog_hash") or ""
        )[:64],
        "updated_at": now,
    }
    table = ResourceGovernanceState.__table__
    dialect = db.bind.dialect.name if db.bind is not None else ""
    insert_factory = None
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as insert_factory
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as insert_factory

    if insert_factory is not None:
        statement = insert_factory(table).values(**payload)
        excluded = statement.excluded
        existing = table.c
        updates = {"updated_at": excluded.updated_at}
        for key in (
            "catalog_hash", "tool_count", "acknowledged_by",
            "acknowledged_catalog_hash",
        ):
            if key in values:
                updates[key] = getattr(excluded, key)
        if "acknowledged_catalog_hash" in values and not payload["review_required"]:
            updates["review_required"] = case(
                (existing.catalog_hash == excluded.acknowledged_catalog_hash, False),
                else_=existing.review_required,
            )
        elif "catalog_hash" in values:
            updates["review_required"] = (
                existing.review_required
                | excluded.review_required
                | ((existing.catalog_hash != "") & (existing.catalog_hash != excluded.catalog_hash))
            )
        elif "review_required" in values:
            updates["review_required"] = excluded.review_required
        db.execute(statement.on_conflict_do_update(
            index_elements=[table.c.resource_type, table.c.resource_id],
            set_=updates,
        ))
        db.flush()
        db.expire_all()
        return resource_state(db, resource_type, resource_id)

    row = db.execute(select(ResourceGovernanceState).where(
        ResourceGovernanceState.resource_type == resource_type,
        ResourceGovernanceState.resource_id == resource_id,
    ).with_for_update()).scalar_one_or_none()
    if row is None:
        row = ResourceGovernanceState(**payload)
        db.add(row)
    else:
        old_hash = row.catalog_hash or ""
        for key in values:
            if key in payload and key not in {"resource_type", "resource_id"}:
                setattr(row, key, payload[key])
        if "catalog_hash" in values and old_hash and old_hash != payload["catalog_hash"]:
            row.review_required = True
        row.updated_at = now
    db.flush()
    return _state_dict(row)
