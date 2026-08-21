"""数据库共享的固定窗口限流。

所有 API 副本使用同一计数表；主体键经 SHA-256 后落库，避免长期保存原始 IP、用户名
或 API Key 标识。网关限流仍可作为第一层保护，本模块提供一致的应用层硬门禁。
"""
from __future__ import annotations

import datetime
import hashlib

from fastapi import HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from .config import settings
from .database import SessionLocal
from .models import RateLimitBucket


def client_ip(request: Request) -> str:
    """默认只信任直连地址；明确启用后才读取反向代理传入的 X-Forwarded-For。"""
    if settings.TRUST_PROXY_HEADERS:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def _token(bucket: str, key: str) -> str:
    return hashlib.sha256(f"{bucket}\0{key}".encode("utf-8")).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def enforce(bucket: str, key: str, limit: int, window_seconds: int) -> None:
    if limit <= 0 or window_seconds <= 0:
        return
    token = _token(bucket, str(key))
    now = _now()
    cutoff = now - datetime.timedelta(seconds=window_seconds)
    for _ in range(2):
        db = SessionLocal()
        try:
            # 过期窗口先原子归零；随后只在 count < limit 时递增。
            db.query(RateLimitBucket).filter(
                RateLimitBucket.bucket_key == token,
                RateLimitBucket.window_start <= cutoff,
            ).update(
                {
                    RateLimitBucket.count: 0,
                    RateLimitBucket.window_start: now,
                },
                synchronize_session=False,
            )
            updated = db.query(RateLimitBucket).filter(
                RateLimitBucket.bucket_key == token,
                RateLimitBucket.count < limit,
            ).update(
                {RateLimitBucket.count: RateLimitBucket.count + 1},
                synchronize_session=False,
            )
            if updated:
                db.commit()
                return
            row = db.get(RateLimitBucket, token)
            if row is None:
                db.add(RateLimitBucket(
                    bucket_key=token, count=1, window_start=now
                ))
                try:
                    db.commit()
                    return
                except IntegrityError:
                    db.rollback()
                    continue
            started = row.window_start
            if started.tzinfo is None:
                started = started.replace(tzinfo=datetime.timezone.utc)
            retry_after = max(
                1,
                int(window_seconds - (now - started).total_seconds()),
            )
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "请求过于频繁，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        finally:
            db.close()
    raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")


def reset(bucket: str, key: str) -> None:
    db = SessionLocal()
    try:
        db.query(RateLimitBucket).filter(
            RateLimitBucket.bucket_key == _token(bucket, str(key))
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def cleanup_expired(retention_seconds: int | None = None) -> int:
    """回收远早于任何活跃窗口的共享限流桶，避免高基数主体永久占表。"""
    retention = max(
        1,
        int(
            settings.RATE_LIMIT_BUCKET_TTL_SECONDS
            if retention_seconds is None
            else retention_seconds
        ),
    )
    cutoff = _now() - datetime.timedelta(seconds=retention)
    db = SessionLocal()
    try:
        deleted = db.query(RateLimitBucket).filter(
            RateLimitBucket.window_start < cutoff
        ).delete(synchronize_session=False)
        db.commit()
        return int(deleted or 0)
    finally:
        db.close()


def clear_all() -> None:
    """仅供测试/维护使用。"""
    db = SessionLocal()
    try:
        db.query(RateLimitBucket).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
