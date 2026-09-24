"""Global tool guardrail administration; all endpoints require root."""
import csv
import datetime as dt
import io
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, File, Form, UploadFile, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.orm import Session
from sqlalchemy import update

from .. import guardrails
from ..database import get_db
from ..models import User
from ..runtime.builtin_tools import TOOLS
from ..security import require_root
from ..security import require_module, require_owner, is_root, can_access_agent, can_use
from ..models import Agent, ModelProvider, iso_utc
from ..guardrail_models import GuardrailBlocklist, GuardrailPolicy
from ..guardrail_policies import (
    DETECTORS, POINTS, BlocklistBody, BlockEntry, PolicyBody, evaluate_content,
)
from ..upload_utils import read_upload_limited

router = APIRouter(prefix="/api/v1/guardrails", tags=["护栏"])


class GuardrailUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: guardrails.GuardrailConfig
    revision: int = Field(ge=0, strict=True)


class GuardrailPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["builtin", "mcp"] = "builtin"
    tool_name: str = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_policy: Literal["ask", "auto", "full_access"] = "ask"
    mutating: StrictBool = False
    destructive: StrictBool = False
    server_id: int | None = Field(default=None, ge=1)


def payload(db: Session) -> dict:
    state = guardrails.read_config(db)
    config = state["config"]
    rules = [
        {"id": key, "name": name, "description": description, "enabled": bool(getattr(config, key))}
        for key, name, description in guardrails.RULES
    ]
    configured = sum(rule["enabled"] for rule in rules)
    return {
        **state,
        "config": config.model_dump(),
        "rules": rules,
        "stats": {
            "configured_rules": configured,
            "active_rules": configured if config.enabled else 0,
            "blocked_tools": len(config.blocked_tools),
            "builtin_tools": len(TOOLS),
        },
        "baseline": guardrails.BASELINE,
        "builtin_tools": [
            {"name": tool.name, "description": tool.description, "group": tool.group, "mutating": tool.mutating}
            for tool in TOOLS.values()
        ],
        "scope": "global",
        "managed_by": "root",
    }


@router.get("")
def get_guardrails(_: User = Depends(require_root), db: Session = Depends(get_db)):
    return payload(db)


@router.put("")
def update_guardrails(body: GuardrailUpdate, _: User = Depends(require_root), db: Session = Depends(get_db)):
    try:
        guardrails.save_config(db, body.config, body.revision)
    except guardrails.RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return payload(db)


@router.post("/preview")
def preview_guardrails(body: GuardrailPreview, _: User = Depends(require_root), db: Session = Depends(get_db)):
    mutating = body.mutating
    if body.kind == "builtin":
        tool = TOOLS.get(body.tool_name)
        if tool is None:
            raise HTTPException(400, "未知内置工具；请选择目录中的工具")
        mutating = tool.mutating
    state = guardrails.read_config(db)
    return {
        **guardrails.evaluate_tool(
            state["config"], kind=body.kind, tool_name=body.tool_name,
            arguments=body.arguments, policy=body.approval_policy,
            mutating=mutating, destructive=body.destructive, server_id=body.server_id,
        ),
        "preview": True,
        "authorization_checked": False,
        "revision": state["revision"],
    }


_content_access = require_module("guardrails")


def _reviewer(user: User = Depends(_content_access)):
    from ..guardrail_reviews import can_review
    if not can_review(user):
        raise HTTPException(403, "需要有护栏权限的管理员审批")
    return user


@router.get("/reviews")
def list_reviews(user: User = Depends(_reviewer), db: Session = Depends(get_db)):
    from ..guardrail_models import GuardrailReview
    rows = db.query(GuardrailReview).filter(
        GuardrailReview.status == "pending",
        GuardrailReview.expires_at > dt.datetime.now(dt.timezone.utc),
    ).order_by(GuardrailReview.id).limit(100).all()
    return {"items": [{"id": row.id, "user_id": row.user_id, "agent_id": row.agent_id,
        "summary": json.loads(row.summary), "created_at": iso_utc(row.created_at),
        "expires_at": iso_utc(row.expires_at)} for row in rows]}


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=1000)


@router.post("/reviews/{review_id}/decision")
def decide_review(review_id: int, body: ReviewDecision, user: User = Depends(_reviewer), db: Session = Depends(get_db)):
    from ..guardrail_models import GuardrailReview
    now = dt.datetime.now(dt.timezone.utc)
    changed = db.query(GuardrailReview).filter(
        GuardrailReview.id == review_id, GuardrailReview.status == "pending",
        GuardrailReview.expires_at > now,
    ).update({"status": body.decision, "reviewed_by": user.id, "reviewed_at": now,
              "comment": body.comment}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise HTTPException(409, "审批已处理、过期或不存在，请刷新")
    db.commit()
    return {"id": review_id, "status": body.decision}


def _owned(db, model, user):
    query = db.query(model)
    return query if is_root(user) else query.filter(model.created_by == user.id)


def _get_owned(db, model, resource_id, user):
    row = _owned(db, model, user).filter(model.id == resource_id).first()
    if row is None:
        raise HTTPException(404, "护栏资源不存在或无权访问")
    return row


def _serialize(row):
    base = {"id": row.id, "name": row.name, "description": row.description,
            "revision": row.revision, "created_by": row.created_by, "can_manage": True,
            "created_at": iso_utc(row.created_at), "updated_at": iso_utc(row.updated_at)}
    if isinstance(row, GuardrailPolicy):
        return {**base, **json.loads(row.config), "enabled": row.enabled}
    return {**base, "entries": json.loads(row.entries)}


def _validate_policy_refs(db, user, body: PolicyBody):
    for agent_id in body.agent_ids:
        agent = db.get(Agent, agent_id)
        if agent is None or not can_access_agent(user, agent):
            raise HTTPException(403, "无权绑定指定智能体")
    for provider_id in body.provider_ids:
        provider = db.get(ModelProvider, provider_id)
        if provider is None or not provider.enabled or not can_use(user, provider):
            raise HTTPException(403, "无权绑定指定模型")
    ids = {bid for rule in body.rules for bid in rule.blocklist_ids}
    return {bid: json.loads(_get_owned(db, GuardrailBlocklist, bid, user).entries) for bid in ids}


def _policy_values(body):
    return {
        "name": body.name, "description": body.description, "enabled": body.enabled,
        "config": json.dumps(body.model_dump(exclude={"name", "description", "enabled", "revision"}), ensure_ascii=False),
    }


def _cas_update(db, model, row, revision, values):
    if revision is None or row.revision != revision:
        raise HTTPException(409, "资源版本已变化，请刷新后重试")
    result = db.execute(update(model).where(model.id == row.id, model.revision == revision).values(
        **values, revision=revision + 1, updated_at=dt.datetime.now(dt.timezone.utc),
    ).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "资源版本已变化，请刷新后重试")
    db.commit()
    db.refresh(row)
    return _serialize(row)


@router.get("/catalog")
def content_catalog(user: User = Depends(_content_access), db: Session = Depends(get_db)):
    return {
        "detectors": DETECTORS,
        "intervention_points": [{"id": key, "name": name} for key, name in zip(POINTS, ("用户输入", "工具输入", "工具输出", "模型输出"))],
        "agents": [{"id": row.id, "name": row.name} for row in db.query(Agent).filter(Agent.enabled.is_(True)).all() if can_access_agent(user, row)],
        "providers": [{"id": row.id, "name": row.name} for row in db.query(ModelProvider).filter(ModelProvider.enabled.is_(True)).all() if can_use(user, row)],
        "can_manage_global": is_root(user),
        "scope_note": "root策略作用于绑定资源的所有使用者；其他账户的策略仅作用于自己的任务。多个绑定按任一命中生效。",
        "limits": {"blocklist_entries": 1000, "csv_bytes": 262144, "safe_regex": "字符类、转义、锚点、固定{n}重复；不支持分组、分支、+、*、?或可变次数。"},
    }


@router.get("/policies")
def list_policies(q: str = Query("", max_length=128), user: User = Depends(_content_access), db: Session = Depends(get_db)):
    query = _owned(db, GuardrailPolicy, user)
    if q.strip():
        query = query.filter(GuardrailPolicy.name.contains(q.strip(), autoescape=True))
    rows = query.order_by(GuardrailPolicy.updated_at.desc(), GuardrailPolicy.id.desc()).all()
    return {"items": [_serialize(row) for row in rows], "total": len(rows)}


@router.post("/policies", status_code=201)
def create_policy(body: PolicyBody, user: User = Depends(_content_access), db: Session = Depends(get_db)):
    _validate_policy_refs(db, user, body)
    row = GuardrailPolicy(**_policy_values(body), created_by=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)


class PolicyPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: PolicyBody
    point: Literal["user_input", "tool_input", "tool_output", "model_output"]
    text: str = Field(max_length=100000)


@router.post("/policies/preview")
def preview_policy(body: PolicyPreview, user: User = Depends(_content_access), db: Session = Depends(get_db)):
    lists = _validate_policy_refs(db, user, body.policy)
    result = evaluate_content([body.policy.model_dump()], lists, body.point, body.text)
    return {**result, "preview": True, "scope_checked": True}


@router.get("/policies/{policy_id}")
def get_policy(policy_id: int, user: User = Depends(_content_access), db: Session = Depends(get_db)):
    return _serialize(_get_owned(db, GuardrailPolicy, policy_id, user))


@router.put("/policies/{policy_id}")
def update_policy(policy_id: int, body: PolicyBody, user: User = Depends(_content_access), db: Session = Depends(get_db)):
    row = _get_owned(db, GuardrailPolicy, policy_id, user)
    _validate_policy_refs(db, user, body)
    # Root editing someone else's policy cannot attach private root resources
    # that the policy owner cannot access.
    owner = db.get(User, row.created_by)
    if user.id != row.created_by:
        _validate_policy_refs(db, owner, body)
    return _cas_update(db, GuardrailPolicy, row, body.revision, _policy_values(body))


@router.delete("/policies/{policy_id}", status_code=204)
def delete_policy(policy_id: int, user: User = Depends(_content_access), db: Session = Depends(get_db)):
    db.delete(_get_owned(db, GuardrailPolicy, policy_id, user))
    db.commit()


@router.get("/blocklists")
def list_blocklists(q: str = Query("", max_length=128), user: User = Depends(_content_access), db: Session = Depends(get_db)):
    query = _owned(db, GuardrailBlocklist, user)
    if q.strip():
        query = query.filter(GuardrailBlocklist.name.contains(q.strip(), autoescape=True))
    rows = query.order_by(GuardrailBlocklist.updated_at.desc(), GuardrailBlocklist.id.desc()).all()
    return {"items": [_serialize(row) for row in rows], "total": len(rows)}


@router.post("/blocklists", status_code=201)
def create_blocklist(body: BlocklistBody, user: User = Depends(_content_access), db: Session = Depends(get_db)):
    row = GuardrailBlocklist(name=body.name, description=body.description,
        entries=json.dumps([entry.model_dump() for entry in body.entries], ensure_ascii=False), created_by=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)


@router.post("/blocklists/import", status_code=201)
async def import_blocklist(file: UploadFile = File(...), name: str = Form(...), description: str = Form(""), user: User = Depends(_content_access), db: Session = Depends(get_db)):
    content = await read_upload_limited(file, 262144, "阻止列表 CSV")
    try:
        decoded = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))
        if not reader.fieldnames or "value" not in reader.fieldnames or set(reader.fieldnames) - {"value", "mode", "case_sensitive"}:
            raise ValueError("CSV表头须包含value，可选mode、case_sensitive")
        entries = []
        for row in reader:
            if len(entries) >= 1000:
                raise ValueError("CSV最多1000条")
            case = (row.get("case_sensitive") or "false").strip().lower()
            if case not in {"true", "false", "1", "0"}:
                raise ValueError("case_sensitive须为true/false")
            entries.append(BlockEntry(value=row.get("value") or "", mode=row.get("mode") or "exact", case_sensitive=case in {"true", "1"}))
        body = BlocklistBody(name=name, description=description, entries=entries)
    except (UnicodeError, ValueError, csv.Error) as exc:
        raise HTTPException(422, f"CSV校验失败：{exc}") from exc
    return create_blocklist(body, user, db)


@router.put("/blocklists/{blocklist_id}")
def update_blocklist(blocklist_id: int, body: BlocklistBody, user: User = Depends(_content_access), db: Session = Depends(get_db)):
    row = _get_owned(db, GuardrailBlocklist, blocklist_id, user)
    return _cas_update(db, GuardrailBlocklist, row, body.revision, {
        "name": body.name, "description": body.description,
        "entries": json.dumps([entry.model_dump() for entry in body.entries], ensure_ascii=False),
    })


@router.delete("/blocklists/{blocklist_id}", status_code=204)
def delete_blocklist(blocklist_id: int, user: User = Depends(_content_access), db: Session = Depends(get_db)):
    row = _get_owned(db, GuardrailBlocklist, blocklist_id, user)
    for policy in db.query(GuardrailPolicy).all():
        if any(blocklist_id in rule.get("blocklist_ids", []) for rule in json.loads(policy.config).get("rules", [])):
            raise HTTPException(409, "此阻止列表仍被护栏引用，请先解除绑定")
    db.delete(row)
    db.commit()
