"""Legacy visitor data helpers and restricted historical model snapshots.

Visitor authentication and HTTP model management have been retired.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from fastapi import HTTPException

from . import harness
from .models import Agent, ModelProvider, User
from .personal_network import get_personal_http_client, public_addresses

PERSONAL_MODEL_PREFIX = "__personal_model_"
GUEST_PROMPT = "你是一个对话助手。根据用户提供的内容回答。不能访问平台工具、知识库或执行外部操作；不要声称已执行这些操作。"


def is_guest(user) -> bool:
    return getattr(user, "role", "") == "guest"


def guest_agent_name(user_id: int) -> str:
    return f"__guest_agent_{user_id}"


def guest_agent(db, user: User, *, create: bool = False) -> Agent:
    row = db.query(Agent).filter(
        Agent.created_by == user.id, Agent.name == guest_agent_name(user.id),
    ).first()
    if row is None and create:
        row = Agent(
            name=guest_agent_name(user.id), description="访客专属对话", enabled=True,
            provider_id=None, routing="{}", mcp_ids="[]", skill_ids="[]",
            agent_ids="[]", builtin_tools="[]", memory_enabled=False,
            is_public=False, is_default=False, created_by=user.id,
        )
        db.add(row)
        db.flush()
        harness.create_version(
            db, row, system_prompt=GUEST_PROMPT,
            tool_policy={**harness.DEFAULT_TOOL_POLICY, "router": {"enabled": False}},
            memory_policy={"enabled": False}, created_by=user.id, publish=True,
            change_summary="创建隔离访客对话",
        )
    if row is None or not row.enabled:
        raise HTTPException(404, "访客对话不存在，请重新打开页面")
    return row


def guest_provider_allowed(user: User, provider) -> bool:
    return bool(provider is not None and provider.enabled and provider.model_id and (
        provider.is_public or provider.created_by == user.id
    ))


def select_guest_provider(db, user: User, provider_id=None):
    if provider_id is not None:
        provider = db.get(ModelProvider, int(provider_id))
        if not guest_provider_allowed(user, provider):
            raise ValueError("该模型未公开、已停用或不属于当前访客")
        return provider
    candidates = db.query(ModelProvider).filter(
        ModelProvider.enabled.is_(True),
        (ModelProvider.is_public.is_(True)) | (ModelProvider.created_by == user.id),
    ).order_by(ModelProvider.is_public.desc(), ModelProvider.id).all()
    return next((row for row in candidates if row.model_id), None)


def reject_guest_resources(user, *, skill_ids=(), mcp_ids=(), agent_ids=(),
                           template_ids=(), dataset_ids=()) -> None:
    if is_guest(user) and any((skill_ids, mcp_ids, agent_ids, template_ids, dataset_ids)):
        raise HTTPException(403, "访客仅可对话，平台工具、知识库、模板和智能体需要登录")


def restrict_guest_snapshot(snapshot: dict, user: User) -> dict:
    """Apply a server-owned profile even if an administrator edited the guest row."""
    result = dict(snapshot)
    result["guest_owner_id"] = user.id
    result["agent"] = {**result.get("agent", {}), "name": "访客助手", "memory_enabled": False}
    result["harness"] = {
        **result.get("harness", {}), "system_prompt": GUEST_PROMPT,
        "tool_policy": {**harness.DEFAULT_TOOL_POLICY, "router": {"enabled": False}},
        "memory_policy": {"enabled": False},
    }
    for key in ("skills", "mcp_servers", "sub_agents", "builtin_tools", "provider_fallbacks"):
        result[key] = []
    result["model_roles"] = {}
    result["model_execution"] = {}
    result["approval_policy"] = "ask"
    return result


def validate_guest_execution(db, user: User, agent: Agent, snapshot: dict) -> None:
    if not is_guest(user):
        return
    raise RuntimeError("访客功能已移除，请登录后使用问答")


def validate_personal_model_url(value: str) -> str:
    """Personal endpoints never inherit an administrator's private-network bypass."""
    url = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
        public_addresses(parsed.hostname, port or (443 if parsed.scheme == "https" else 80))
    except (ValueError, OSError):
        raise ValueError("个人模型接口必须是可解析的公网 HTTP(S) 地址，不允许内网、凭据或查询参数") from None
    return url


def personal_model_client(provider):
    from .llm.client import LLMClient

    class PersonalClient(LLMClient):
        async def _guard(self):
            await asyncio.to_thread(validate_personal_model_url, self.base_url)

        def _http_client(self):
            return get_personal_http_client()

    return PersonalClient(
        base_url=provider.base_url, api_key=provider.api_key, model_id=provider.model_id,
        model_input=json.loads(provider.model_input or '["text"]')
        if isinstance(provider.model_input, str) else provider.model_input,
        provider_type="anthropic" if provider.wire_api == "messages" else "openai",
        wire_api=provider.wire_api, auth_type="x_api_key" if provider.wire_api == "messages" else "bearer",
        reasoning_effort=getattr(provider, "reasoning_effort", "") if provider.model_reasoning else "",
        reasoning_config=getattr(provider, "reasoning_config", "{}") or {},
        max_tokens=min(8192, int(getattr(provider, "max_tokens", 8192) or 8192)),
        timeout_ms=120000, max_retries=1, stream_max_retries=1,
        context_window=getattr(provider, "context_window", 0), provider_id=provider.id,
    )


def effective_reasoning_provider(provider):
    """Use the personal client's actual output limit when advertising controls."""
    def field(name, default=None):
        return provider.get(name, default) if isinstance(provider, dict) else getattr(provider, name, default)

    if not str(field("name", "") or "").startswith(PERSONAL_MODEL_PREFIX):
        return provider
    result = {name: field(name) for name in (
        "name", "model_id", "base_url", "provider_type", "wire_api", "model_reasoning",
        "reasoning_effort", "reasoning_config",
    )}
    result.update(max_tokens=min(8192, int(field("max_tokens", 8192) or 8192)),
                  max_tokens_param="auto", extra_body={})
    return result
