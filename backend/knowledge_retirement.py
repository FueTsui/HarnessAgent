"""One-time, narrowly scoped removal of retired platform knowledge bases."""
from pathlib import Path
import json
import shutil

LEGACY_KEYS = frozenset({"green", "vpp"})
MARKER_NAME = "_retired_platform_datasets_v1.json"


def _guard(path: Path, root: Path) -> None:
    """Validate every removal target before any mutation, including junctions."""
    if path.is_symlink() or path.is_junction():
        raise RuntimeError(f"Refusing linked knowledge path: {path.name}")
    if not path.resolve().is_relative_to(root.resolve()) or path.resolve() == root.resolve():
        raise RuntimeError("Knowledge cleanup escaped its data directory")
    if path.is_dir():
        for child in path.iterdir():
            _guard(child, root)


def retire_legacy_datasets(db=None) -> dict:
    from .capabilities import knowledge
    from .models import Project

    with knowledge._locked_meta():
        root = knowledge.KNOWLEDGE_DIR
        marker = root / MARKER_NAME
        if marker.exists():
            return {"changed": False, "removed": [], "projects_updated": 0}
        registry = knowledge.DATASETS_META
        tombstones = root / "_deleted_builtins.json"
        for path in (registry, tombstones, marker):
            _guard(path, root)
        raw = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
        if not isinstance(raw, dict):
            raise ValueError("Invalid knowledge registry; refusing cleanup")
        removed = []
        for key in sorted(LEGACY_KEYS):
            meta = raw.get(key)
            # Explicitly owned/custom knowledge must never be retired merely
            # because it happens to reuse a historical platform key.
            if meta is not None and not (
                isinstance(meta, dict) and meta.get("created_by") is None
                and meta.get("builtin") is True
            ):
                continue
            _guard(root / key, root)
            removed.append(key)
        old_deleted = json.loads(tombstones.read_text(encoding="utf-8")) if tombstones.exists() else []
        if not isinstance(old_deleted, list):
            raise ValueError("Invalid legacy knowledge tombstones; refusing cleanup")
        projects = []
        if db is not None:
            for project in db.query(Project).all():
                keys = json.loads(project.dataset_ids or "[]")
                if not isinstance(keys, list):
                    raise ValueError("Invalid project knowledge references; refusing cleanup")
                kept = [key for key in keys if key not in removed]
                if kept != keys:
                    projects.append((project, kept))
        # All metadata and paths are now validated. On a partial I/O failure,
        # leave the marker absent so the next startup retries this exact scope.
        for key in removed:
            folder = root / key
            if folder.is_dir():
                shutil.rmtree(folder)
            elif folder.exists():
                folder.unlink()
            raw.pop(key, None)
        knowledge._atomic_json_write(registry, raw)
        if tombstones.exists():
            kept = [key for key in old_deleted if key not in LEGACY_KEYS]
            if kept:
                knowledge._atomic_json_write(tombstones, kept)
            else:
                tombstones.unlink()
        for project, kept in projects:
            project.dataset_ids = json.dumps(kept, ensure_ascii=False)
        if db is not None:
            db.commit()
        knowledge._chunk_cache.clear()
        result = {"changed": True, "removed": removed, "projects_updated": len(projects)}
        knowledge._atomic_json_write(marker, {"version": 1, **result})
        return result
