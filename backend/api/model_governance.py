"""模型健康、价格版本和成本估算控制面。"""
from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..model_governance import (
    append_price,
    estimate_cost,
    microusd_to_usd,
    price_entries,
    provider_health,
    public_price,
)
from ..models import ModelProvider, TokenUsage, User, iso_utc
from ..security import can_manage, require_module, require_owner, scope_owned


router = APIRouter(
    prefix="/api/v1/model-governance",
    tags=["模型运行治理"],
)


def _provider(db: Session, provider_id: int) -> ModelProvider:
    row = db.get(ModelProvider, int(provider_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型提供商不存在")
    return row


def _latest_price(db: Session, provider: ModelProvider) -> dict | None:
    values = price_entries(db, provider.id)
    model_values = [
        item for item in values
        if str(item.get("model") or "") in {"", "*", provider.model_id}
    ]
    return public_price(model_values[-1]) if model_values else None


def _modalities(provider: ModelProvider) -> list[str]:
    try:
        values = json.loads(provider.model_input or '["text"]')
    except (json.JSONDecodeError, TypeError):
        values = ["text"]
    return [str(value) for value in values] if isinstance(values, list) else ["text"]


@router.get("/providers")
def provider_governance_summary(
    lookback_minutes: int = Query(60, ge=1, le=10080),
    admin: User = Depends(require_module("providers")),
    db: Session = Depends(get_db),
):
    rows = scope_owned(db.query(ModelProvider), ModelProvider, admin).order_by(
        ModelProvider.name, ModelProvider.id
    ).all()
    return {
        "lookback_minutes": lookback_minutes,
        "items": [
            {
                "provider_id": row.id,
                "name": row.name,
                "model": row.model_id,
                "enabled": bool(row.enabled),
                "modalities": _modalities(row),
                "health": provider_health(
                    db, row.id, lookback_minutes=lookback_minutes
                ),
                "price": _latest_price(db, row),
                "can_manage": can_manage(admin, row),
            }
            for row in rows
        ],
    }


@router.get("/providers/{provider_id}/prices")
def list_provider_prices(
    provider_id: int,
    admin: User = Depends(require_module("providers")),
    db: Session = Depends(get_db),
):
    provider = _provider(db, provider_id)
    # scope_owned 的语义：公开资源可被查看，但鉴权信息和管理动作仍不可用。
    visible = scope_owned(
        db.query(ModelProvider).filter(ModelProvider.id == provider.id),
        ModelProvider,
        admin,
    ).first()
    if visible is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该模型提供商")
    return {
        "provider_id": provider.id,
        "model": provider.model_id,
        "items": [public_price(item) for item in price_entries(db, provider.id)],
    }


@router.put("/providers/{provider_id}/pricing", status_code=status.HTTP_201_CREATED)
def set_provider_price(
    provider_id: int,
    body: dict = Body(...),
    admin: User = Depends(require_module("providers")),
    db: Session = Depends(get_db),
):
    provider = _provider(db, provider_id)
    require_owner(admin, provider)
    model = str(body.get("model") or provider.model_id or "").strip()
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provider 尚未选择模型")
    try:
        return append_price(
            db,
            provider_id=provider.id,
            model=model,
            input_usd_per_million=body.get("input_usd_per_million", 0),
            output_usd_per_million=body.get("output_usd_per_million", 0),
            cached_usd_per_million=body.get("cached_usd_per_million", 0),
            reasoning_usd_per_million=body.get("reasoning_usd_per_million", 0),
            created_by=admin.id,
            priced=bool(body.get("priced", True)),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/costs")
def model_cost_summary(
    days: int = Query(30, ge=0, le=3650),
    user_id: int | None = Query(None, ge=1),
    user: User = Depends(require_module("token_usage")),
    db: Session = Depends(get_db),
):
    from ..security import is_root

    query = db.query(TokenUsage)
    if not is_root(user):
        user_id = user.id
    if days:
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(0, days - 1))
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(TokenUsage.created_at >= start)
    if user_id is not None:
        query = query.filter(TokenUsage.user_id == user_id)
    rows = query.order_by(TokenUsage.created_at, TokenUsage.id).all()
    total_microusd = 0
    priced_requests = 0
    unknown_requests = 0
    by_provider: dict[str, dict] = {}
    recent = []
    for row in rows:
        cost = estimate_cost(
            db,
            provider_id=row.provider_id,
            model=row.model,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cached_tokens=row.cached_tokens,
            reasoning_tokens=row.reasoning_tokens,
            at=row.created_at,
        )
        if cost["status"] == "estimated":
            priced_requests += 1
            total_microusd += int(cost["microusd"] or 0)
        else:
            unknown_requests += 1
        key = str(row.provider_id or "unknown")
        bucket = by_provider.setdefault(key, {
            "provider_id": row.provider_id,
            "requests": 0,
            "priced_requests": 0,
            "unknown_requests": 0,
            "estimated_microusd": 0,
        })
        bucket["requests"] += 1
        bucket[
            "priced_requests" if cost["status"] == "estimated" else "unknown_requests"
        ] += 1
        bucket["estimated_microusd"] += int(cost["microusd"] or 0)
        recent.append({
            "usage_id": row.id,
            "provider_id": row.provider_id,
            "model": row.model,
            "total_tokens": row.total_tokens,
            "cost": cost,
            "created_at": iso_utc(row.created_at),
        })
    return {
        "range_days": days,
        "totals": {
            "requests": len(rows),
            "priced_requests": priced_requests,
            "unknown_requests": unknown_requests,
            "estimated_microusd": total_microusd,
            "estimated_usd": microusd_to_usd(total_microusd),
        },
        "providers": [
            {**bucket, "estimated_usd": microusd_to_usd(bucket["estimated_microusd"])}
            for bucket in by_provider.values()
        ],
        "recent": list(reversed(recent[-50:])),
    }
