"""Token 用量：root 查看全体，获授权的非 root 用户仅查看本人。"""
from __future__ import annotations

import datetime
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import TokenUsage, User, UserTokenLimit, iso_utc
from ..schemas import UserTokenLimitsUpdate
from ..security import is_root, require_module, require_root
from ..token_usage import get_or_create_limits, usage_limit_snapshot

router = APIRouter(prefix="/api/v1/token-usage", tags=["Token 用量（root）"])


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _day_key(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.date().isoformat()


@router.get("")
def token_usage_summary(
    days: int = Query(30, ge=0, le=3650),
    user_id: int | None = Query(None, ge=1),
    _: User = Depends(require_module("token_usage")),
    db: Session = Depends(get_db),
):
    """按时间范围返回总览、每日趋势、用户排行和最近调用。

    days=0 表示全部历史；其余值包含今天在内。
    """
    query = db.query(TokenUsage)
    if not is_root(_):
        user_id = _.id
    start = None
    if days:
        start = _utc_now() - datetime.timedelta(days=max(0, days - 1))
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(TokenUsage.created_at >= start)
    if user_id is not None:
        query = query.filter(TokenUsage.user_id == user_id)
    rows = query.order_by(TokenUsage.created_at.asc(), TokenUsage.id.asc()).all()

    users_query = db.query(User).order_by(User.id)
    if not is_root(_):
        users_query = users_query.filter(User.id == _.id)
    users = {u.id: u for u in users_query.all()}
    managed_users = (
        [users[user_id]] if user_id is not None and user_id in users
        else list(users.values()) if user_id is None
        else []
    )
    policies = {
        policy.user_id: policy
        for policy in db.query(UserTokenLimit).filter(
            UserTokenLimit.user_id.in_(users.keys())
        ).all()
    } if users else {}
    by_user: dict[int, dict] = {
        user.id: {
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 0, "requests": 0,
            "last_used_at": "",
            "periods": usage_limit_snapshot(
                db, user.id, policy=policies.get(user.id)
            ),
        }
        for user in managed_users
    }
    active_user_ids: set[int] = set()
    by_day: dict[str, dict] = defaultdict(
        lambda: {
            "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 0, "requests": 0,
        }
    )
    totals = {
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "reasoning_tokens": 0, "total_tokens": 0, "requests": 0,
    }
    for row in rows:
        aggregate = by_user.get(row.user_id) if row.user_id is not None else None
        day = by_day[_day_key(row.created_at)]
        for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "total_tokens"):
            value = int(getattr(row, key) or 0)
            if aggregate is not None:
                aggregate[key] += value
            day[key] += value
            totals[key] += value
        if aggregate is not None:
            aggregate["requests"] += 1
            aggregate["last_used_at"] = iso_utc(row.created_at)
            active_user_ids.add(int(row.user_id))
        day["requests"] += 1
        totals["requests"] += 1

    if days and start is not None:
        daily = []
        for offset in range(days):
            key = (start.date() + datetime.timedelta(days=offset)).isoformat()
            daily.append({"date": key, **by_day[key]})
    else:
        daily = [{"date": key, **value} for key, value in sorted(by_day.items())]

    user_rows = sorted(
        by_user.values(),
        key=lambda item: (-item["periods"]["total"]["used"], item["username"]),
    )
    recent_query = db.query(TokenUsage)
    if start is not None:
        recent_query = recent_query.filter(TokenUsage.created_at >= start)
    if user_id is not None:
        recent_query = recent_query.filter(TokenUsage.user_id == user_id)
    recent = recent_query.order_by(TokenUsage.id.desc()).limit(20).all()
    return {
        "range_days": days,
        "totals": {**totals, "active_users": len(active_user_ids)},
        "user_options": [
            {
                "user_id": user.id,
                "username": user.username,
                "role": user.role,
                "periods": usage_limit_snapshot(
                    db, user.id, policy=policies.get(user.id)
                ),
            }
            for user in users.values()
        ],
        "daily": daily,
        "users": user_rows,
        "recent": [
            {
                "id": row.id,
                "user_id": row.user_id,
                "username": (
                    users.get(row.user_id).username if users.get(row.user_id)
                    else row.username or "已删除用户"
                ),
                "model": row.model,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cached_tokens": row.cached_tokens,
                "reasoning_tokens": row.reasoning_tokens,
                "total_tokens": row.total_tokens,
                "created_at": iso_utc(row.created_at),
            }
            for row in recent
        ],
    }


def _limits_result(db: Session, user: User, policy: UserTokenLimit) -> dict:
    return {
        "user_id": user.id,
        "username": user.username,
        "periods": usage_limit_snapshot(db, user.id, policy=policy),
        "weekly_reset_at": iso_utc(policy.weekly_reset_at),
        "monthly_reset_at": iso_utc(policy.monthly_reset_at),
    }


@router.put("/users/{user_id}/limits")
def update_user_token_limits(
    user_id: int,
    body: UserTokenLimitsUpdate,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    """设置用户每周、每月和历史总量上限；0 表示不限额。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    policy = get_or_create_limits(db, user_id)
    policy.weekly_limit = body.weekly_limit
    policy.monthly_limit = body.monthly_limit
    policy.total_limit = body.total_limit
    db.commit()
    db.refresh(policy)
    return _limits_result(db, user, policy)


def _reset_period(
    db: Session, user_id: int, period: str
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    policy = get_or_create_limits(db, user_id)
    setattr(policy, f"{period}_reset_at", _utc_now())
    db.commit()
    db.refresh(policy)
    return _limits_result(db, user, policy)


@router.post("/users/{user_id}/reset-week")
def reset_user_weekly_usage(
    user_id: int,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    """重置本周计费用量；历史 TokenUsage 明细保持不变。"""
    return _reset_period(db, user_id, "weekly")


@router.post("/users/{user_id}/reset-month")
def reset_user_monthly_usage(
    user_id: int,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    """重置本月计费用量；历史 TokenUsage 明细保持不变。"""
    return _reset_period(db, user_id, "monthly")
