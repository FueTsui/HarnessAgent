"""Fail-closed, secret-free release preflight checks.

Deployment-only checks may be explicitly skipped for CI/unit tests. The default
path checks production configuration, Git provenance, the live database and the
locked dependency file and exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import security_config_problems, settings  # noqa: E402
from backend.migrations import EXPECTED_SCHEMA_REVISION  # noqa: E402


@dataclass
class Check:
    name: str
    ok: bool
    message: str


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def check_production_config(skip: bool) -> Check:
    if skip:
        return Check("production-config", True, "SKIPPED explicitly")
    problems = list(security_config_problems(creating_root=False))
    if settings.APP_ENV != "production":
        problems.append("APP_ENV 必须为 production")
    return Check(
        "production-config", not problems,
        "valid" if not problems else "; ".join(problems),
    )


def check_dependency_lock(path: Path | None = None) -> Check:
    lock = path or ROOT / "requirements.lock"
    if not lock.is_file():
        return Check("dependency-lock", False, "requirements.lock is missing")
    try:
        lines = lock.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return Check("dependency-lock", False, "requirements.lock is unreadable")
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line and not line[0].isspace() and not line.startswith(("#", "--")):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    sha = re.compile(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)")
    invalid = [
        block[0].split("\\", 1)[0].strip()
        for block in blocks
        if "==" not in block[0] or not any(sha.search(line) for line in block)
    ]
    if not blocks or invalid:
        return Check(
            "dependency-lock", False,
            "lock entries must be exactly pinned and SHA-256 hashed",
        )
    return Check("dependency-lock", True, f"{len(blocks)} hashed entries")


def check_git(allow_unborn: bool, allow_dirty: bool) -> list[Check]:
    inside = _run_git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return [Check("git-provenance", False, "not a Git worktree")]
    head = _run_git("rev-parse", "--verify", "HEAD")
    checks = [Check(
        "git-head", head.returncode == 0 or allow_unborn,
        "commit present" if head.returncode == 0 else (
            "unborn repository explicitly allowed" if allow_unborn else "repository has no commit"
        ),
    )]
    dirty = bool(_run_git("status", "--porcelain=v1", "--untracked-files=all").stdout.strip())
    checks.append(Check(
        "git-worktree", not dirty or allow_dirty,
        "clean" if not dirty else (
            "dirty worktree explicitly allowed" if allow_dirty else "worktree has tracked/untracked changes"
        ),
    ))
    ignore_targets = (
        ".env", "data/app.db", "data/app.db-wal", "output/browser.png",
        "tmp/migration/app.db", "data_harness_test/app.db",
        "data_harness_test/app.db-wal", "data_harness_test/browser/evidence.png",
    )
    missing = [
        target for target in ignore_targets
        if _run_git("check-ignore", "--no-index", "-q", target).returncode != 0
    ]
    checks.append(Check(
        "generated-artifacts-ignore", not missing,
        "runtime databases/WAL/browser artifacts ignored" if not missing
        else "generated artifact ignore rules are incomplete",
    ))
    return checks


def check_database(skip: bool) -> list[Check]:
    if skip:
        return [Check("database", True, "SKIPPED explicitly")]
    try:
        url = make_url(settings.DATABASE_URL)
        if url.get_backend_name() == "sqlite" and url.database not in {None, "", ":memory:"}:
            db_path = Path(url.database)
            if not db_path.is_absolute():
                db_path = ROOT / db_path
            if not db_path.is_file():
                return [Check("database", False, "configured SQLite database does not exist")]
        engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            tables = set(inspect(connection).get_table_names())
            revision = None
            if "alembic_version" in tables:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
            checks = [
                Check("database-connectivity", True, "connected"),
                Check(
                    "database-revision", revision == EXPECTED_SCHEMA_REVISION,
                    "at expected revision" if revision == EXPECTED_SCHEMA_REVISION
                    else "database revision does not match application head",
                ),
            ]
            if connection.dialect.name == "sqlite":
                integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar()
                foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                checks.extend([
                    Check("sqlite-integrity", integrity == "ok", "ok" if integrity == "ok" else "failed"),
                    Check("sqlite-foreign-keys", not foreign_keys, "ok" if not foreign_keys else "violations found"),
                ])
        engine.dispose()
        return checks
    except Exception as exc:  # deliberately omit exception text/URL/credentials
        return [Check("database", False, f"check failed ({type(exc).__name__})")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed release preflight")
    parser.add_argument("--skip-production-config", action="store_true")
    parser.add_argument("--skip-database", action="store_true")
    parser.add_argument("--allow-unborn", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    checks = [check_production_config(args.skip_production_config), check_dependency_lock()]
    checks.extend(check_git(args.allow_unborn, args.allow_dirty))
    checks.extend(check_database(args.skip_database))
    for item in checks:
        print(f"[{'PASS' if item.ok else 'FAIL'}] {item.name}: {item.message}")
    failed = sum(not item.ok for item in checks)
    print(f"Release preflight: {'PASS' if failed == 0 else 'FAIL'} ({failed} failed)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
