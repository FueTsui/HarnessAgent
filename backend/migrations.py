"""Alembic 启动迁移与多进程互斥。"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from .config import BASE_DIR, DATA_DIR
from .database import engine

_LOCK_TIMEOUT_SECONDS = 60
_STALE_LOCK_SECONDS = 600
_POSTGRES_LOCK_ID = 0x4841524E455353  # "HARNESS"
EXPECTED_SCHEMA_REVISION = "0029_retire_guest_access"


@contextlib.contextmanager
def _filesystem_lock():
    """SQLite/单机数据库迁移互斥；崩溃遗留锁十分钟后可回收。"""
    path = DATA_DIR / ".alembic-migration.lock"
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    descriptor = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > _STALE_LOCK_SECONDS:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError("等待数据库迁移锁超时")
            time.sleep(0.1)
    try:
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


def _config() -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    return config


def run_schema_migrations() -> None:
    """升级到 head；首次接管既有数据库时先标记基线，再跑增量迁移。"""
    lock = _filesystem_lock() if engine.dialect.name != "postgresql" else contextlib.nullcontext()
    # 外层事务负责提交 alembic_version；env.py 复用该连接，不自行另开连接。
    with lock, engine.begin() as connection:
        advisory = engine.dialect.name == "postgresql"
        if advisory:
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": _POSTGRES_LOCK_ID},
            )
        try:
            config = _config()
            config.attributes["connection"] = connection
            tables = set(inspect(connection).get_table_names())
            business_tables = tables - {"alembic_version"}
            if business_tables and "alembic_version" not in tables:
                command.stamp(config, "0001_baseline")
            command.upgrade(config, "head")
        finally:
            if advisory:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _POSTGRES_LOCK_ID},
                )
