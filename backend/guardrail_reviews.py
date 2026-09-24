"""Live, single-invocation review gates. No reusable bypass credentials."""
import asyncio
import datetime as dt
import json
from contextvars import ContextVar
from contextlib import contextmanager

from .database import SessionLocal
from .guardrail_models import GuardrailReview
from .security import has_module_access

review_context = ContextVar("guardrail_review_context", default=False)
WAIT_SECONDS = 300
POLL_SECONDS = 1


@contextmanager
def bind_review_context(run_id):
    token = review_context.set(run_id)
    try:
        yield
    finally:
        review_context.reset(token)


def can_review(user):
    return bool(user and user.is_active and user.role in {"root", "admin"}
                and has_module_access(user, "guardrails"))


def create_review(user_id, agent_id, summary):
    with SessionLocal() as db:
        row = GuardrailReview(user_id=user_id, agent_id=agent_id,
            summary=json.dumps(summary, ensure_ascii=False),
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=WAIT_SECONDS))
        db.add(row)
        db.commit()
        return row.id


def state(review_id):
    with SessionLocal() as db:
        row = db.get(GuardrailReview, review_id)
        if row and row.status == "approved":
            from .models import User
            if not can_review(db.get(User, row.reviewed_by)):
                return "rejected"
        return row.status if row else "cancelled"


def close_review(review_id, status):
    with SessionLocal() as db:
        db.query(GuardrailReview).filter_by(id=review_id, status="pending").update({"status": status})
        db.commit()


async def request_review(user_id, agent_id, summary, runtime_event=None):
    if not review_context.get() or user_id is None:
        return False
    review_id = await asyncio.to_thread(create_review, user_id, agent_id,
        {**summary, "run_id": str(review_context.get())})
    try:
        if runtime_event:
            import inspect
            value = runtime_event("guardrail.awaiting_review", {
                "review_id": review_id, "message": "已通知有护栏权限的管理员审批，最多等待5分钟",
            })
            if inspect.isawaitable(value):
                await value
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            status = await asyncio.to_thread(state, review_id)
            if status != "pending":
                if runtime_event:
                    value = runtime_event("guardrail.reviewed", {"review_id": review_id, "status": status})
                    if inspect.isawaitable(value):
                        await value
                return status == "approved"
            await asyncio.sleep(POLL_SECONDS)
        return False
    finally:
        await asyncio.to_thread(close_review, review_id, "expired")


async def review_tool(decision, user_id, agent_id, runtime_event=None):
    if not review_context.get():
        return decision
    if decision["decision"] != "block" and not decision["guardrail_requires_approval"]:
        return decision
    # Broken configuration and size limits cannot be waived by human review.
    if any(rule["id"] in {"policy_unavailable", "max_argument_chars"} for rule in decision["matched_rules"]):
        return decision
    if await request_review(user_id, agent_id, {
        "tool": decision["tool_name"], "kind": decision["kind"],
        "matched_rules": decision["matched_rules"],
    }, runtime_event):
        baseline = any(rule["id"] == "approval_policy" for rule in decision["matched_rules"])
        return {**decision, "decision": "require_approval" if baseline else "allow",
                "allowed": True, "guardrail_requires_approval": False, "requires_approval": baseline}
    return {**decision, "decision": "block", "allowed": False,
            "reason": "护栏审批未获批准，调用已阻止"}
