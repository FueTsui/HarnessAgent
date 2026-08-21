"""有副作用工具的用户批准：服务端签发、短时有效、数据库原子单次消费。"""
from __future__ import annotations

import datetime
import hashlib
import uuid

import jwt

from .config import settings
from .database import SessionLocal
from .models import ToolApproval


class ApprovalRequired(RuntimeError):
    def __init__(
        self,
        scope: str,
        description: str,
        *,
        agent_id: int | None = None,
        execution_context: dict | None = None,
    ):
        super().__init__(f"工具 {scope} 需要用户批准")
        self.scope = scope
        self.description = description
        # 内联子智能体与父任务共享持久化 Job，但批准必须绑定真正执行动作的
        # Agent。execution_context 仅包含可公开的父子运行标识，供审计事件使用。
        self.agent_id = agent_id
        self.execution_context = dict(execution_context or {})


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(run_id: str, user_id: int, agent_id: int | None, scope: str) -> str:
    now = _now()
    approval_id = uuid.uuid4().hex
    payload = {
        "typ": "tool_approval",
        "jti": approval_id,
        "run_id": run_id,
        "sub": str(user_id),
        "agent_id": agent_id,
        "scope": scope,
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(minutes=5)).timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    db = SessionLocal()
    try:
        db.add(
            ToolApproval(
                id=approval_id,
                run_id=run_id,
                user_id=user_id,
                agent_id=agent_id,
                scope=scope,
                token_hash=_hash(token),
                expires_at=now + datetime.timedelta(minutes=5),
            )
        )
        db.commit()
    finally:
        db.close()
    return token


def consume(
    tokens: list[str],
    *,
    run_id: str,
    user_id: int | None,
    agent_id: int | None,
    scope: str,
) -> bool:
    if not user_id or not run_id:
        return False
    now = _now()
    for token in tokens:
        try:
            claims = jwt.decode(
                token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
            )
        except jwt.PyJWTError:
            continue
        if (
            claims.get("typ") != "tool_approval"
            or claims.get("run_id") != run_id
            or claims.get("sub") != str(user_id)
            or claims.get("agent_id") != agent_id
            or claims.get("scope") != scope
        ):
            continue
        db = SessionLocal()
        try:
            updated = (
                db.query(ToolApproval)
                .filter(
                    ToolApproval.id == claims.get("jti"),
                    ToolApproval.token_hash == _hash(token),
                    ToolApproval.run_id == run_id,
                    ToolApproval.user_id == user_id,
                    ToolApproval.scope == scope,
                    ToolApproval.consumed_at.is_(None),
                    ToolApproval.expires_at > now,
                )
                .update(
                    {ToolApproval.consumed_at: now},
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated == 1:
                return True
        finally:
            db.close()
    return False
