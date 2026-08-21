"""SQLite Online Backup API utility with non-overwriting restore drills."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.migrations import EXPECTED_SCHEMA_REVISION  # noqa: E402


def validate_database(path: Path, expected_revision: str = EXPECTED_SCHEMA_REVISION) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise ValueError("SQLite file does not exist")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("SQLite integrity check failed")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ValueError("SQLite foreign-key check failed")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] if table else None
        if revision != expected_revision:
            raise ValueError("SQLite revision does not match application head")
    finally:
        connection.close()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "sha256": digest.hexdigest(),
        "size_bytes": stat.st_size,
        "revision": revision,
        "validated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def online_copy(
    source: Path,
    target: Path,
    expected_revision: str = EXPECTED_SCHEMA_REVISION,
) -> dict:
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise ValueError("source and target must differ")
    if not source.is_file():
        raise ValueError("source SQLite file does not exist")
    if target.exists():
        raise FileExistsError("target already exists; overwrite is forbidden")
    if not target.parent.is_dir():
        raise ValueError("target parent directory must already exist")
    source_db = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_db = None
    try:
        target_db = sqlite3.connect(str(target))
        source_db.backup(target_db)
        target_db.commit()
    except Exception:
        if target_db is not None:
            target_db.close()
        target.unlink(missing_ok=True)
        source_db.close()
        raise
    else:
        target_db.close()
        source_db.close()
    try:
        return validate_database(target, expected_revision)
    except Exception:
        # This call created target, so removing a failed validation artifact is
        # safe and prevents it being mistaken for a usable backup.
        target.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe SQLite backup/restore drill")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore-drill"):
        command = subcommands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--target", type=Path, required=True)
        command.add_argument(
            "--expected-revision", default=EXPECTED_SCHEMA_REVISION,
            help="revision required in the completed copy",
        )
    args = parser.parse_args(argv)
    try:
        metadata = online_copy(args.source, args.target, args.expected_revision)
    except Exception as exc:
        # Paths and SQLite error strings can contain deployment details; emit class only.
        print(f"SQLite {args.command}: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(json.dumps({"operation": args.command, **metadata}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
