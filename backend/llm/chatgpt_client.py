"""ChatGPT 订阅直连客户端（基于 Codex CLI 的非官方流程）。

用 `codex login`（ChatGPT 账号登录）得到的 OAuth 令牌，走 OpenAI 的 Responses 通道
（chatgpt.com/backend-api/codex/responses），从而用上 ChatGPT 订阅额度，而非按量计费 API Key。

⚠ 这是基于 Codex 公开行为的非官方实现：端点/请求格式/令牌刷新流程都可能随 OpenAI 调整而失效；
   仅作便利接入，生产建议用正式 API Key。

对外暴露与 LLMClient 一致的子集：chat / chat_with_tools / vision / ping，供引擎无差别调用。
当前 Codex 登录模型仅声明文本输入，不暗中回退到另一视觉模型。
"""
import base64
import json
import logging
import uuid
from typing import Optional

import httpx

from ..config import settings
from .codex_models import (
    available_chatgpt_codex_models,
    normalize_chatgpt_codex_model,
)

logger = logging.getLogger(__name__)

# Codex CLI 的公开 OAuth 客户端 id 与令牌刷新端点（与 codex login 同源）
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
REQUEST_TIMEOUT = 300.0


def _b64url_json(segment: str) -> dict:
    """解码 JWT 的一段 base64url 为 dict（不校验签名，仅读取声明）。"""
    try:
        pad = "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment + pad))
    except Exception:  # noqa: BLE001
        return {}


def decode_jwt_claims(token: str) -> dict:
    parts = (token or "").split(".")
    return _b64url_json(parts[1]) if len(parts) >= 2 else {}


def account_id_from_tokens(tokens: dict) -> str:
    """从 tokens 取 chatgpt 账号 id：优先 account_id 字段，否则从 id_token 的 auth 声明里取。"""
    if tokens.get("account_id"):
        return str(tokens["account_id"])
    claims = decode_jwt_claims(tokens.get("id_token", ""))
    auth = claims.get("https://api.openai.com/auth") or {}
    return str(auth.get("chatgpt_account_id") or "")


def _token_expiry(access_token: str) -> int:
    return int(decode_jwt_claims(access_token).get("exp") or 0)


class ChatGPTClient:
    def __init__(self, provider) -> None:
        self.provider_id = getattr(provider, "id", None)
        configured_model = getattr(provider, "model_id", "")
        self.text_model = normalize_chatgpt_codex_model(configured_model)
        if configured_model and self.text_model != configured_model:
            logger.warning(
                "ChatGPT Codex 模型 %s 已迁移为 %s",
                configured_model,
                self.text_model,
            )
        self.base_url = (getattr(provider, "base_url", "") or DEFAULT_CHATGPT_BASE).rstrip("/")
        try:
            auth_extra = getattr(provider, "auth_extra", "") or ""
            self.auth = json.loads(auth_extra) if auth_extra else {}
        except json.JSONDecodeError:
            self.auth = {}
        # 兼容：access_token 也可能存在 api_key 字段
        api_key = getattr(provider, "api_key", "") or ""
        if not self.auth.get("access_token") and api_key:
            self.auth["access_token"] = api_key
        self.account_id = account_id_from_tokens(self.auth)
        self._session_id = str(uuid.uuid4())
        # 上下文窗口（token），供多轮对话自动压缩用；0 → 全局默认
        self.context_tokens = getattr(provider, "context_window", 0) or settings.LLM_CONTEXT_TOKENS

    # ---------- 令牌 ----------
    def _persist_auth(self) -> None:
        """把刷新后的令牌回写数据库（独立短会话，避免依赖调用方的 session）。"""
        try:
            from ..database import SessionLocal
            from ..models import ModelProvider
            db = SessionLocal()
            try:
                p = db.get(ModelProvider, self.provider_id)
                if p is not None:
                    p.auth_extra = json.dumps(self.auth, ensure_ascii=False)
                    p.api_key = self.auth.get("access_token", "")
                    db.commit()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ChatGPT 令牌回写失败（不影响本次调用）：%s", exc)

    async def _refresh_token(self) -> None:
        refresh = self.auth.get("refresh_token")
        if not refresh:
            raise RuntimeError("ChatGPT 凭据缺少 refresh_token，请重新 codex login 后再导入")
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(OAUTH_TOKEN_URL, json={
                "client_id": OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": "openid profile email",
            })
        if resp.status_code != 200:
            raise RuntimeError(f"ChatGPT 令牌刷新失败（{resp.status_code}）：{resp.text[:200]}")
        data = resp.json()
        self.auth["access_token"] = data.get("access_token", self.auth.get("access_token"))
        if data.get("refresh_token"):
            self.auth["refresh_token"] = data["refresh_token"]
        if data.get("id_token"):
            self.auth["id_token"] = data["id_token"]
        self.account_id = account_id_from_tokens(self.auth) or self.account_id
        self._persist_auth()
        logger.info("ChatGPT 令牌已刷新")

    async def _ensure_fresh(self) -> None:
        import time
        exp = _token_expiry(self.auth.get("access_token", ""))
        if exp and exp - time.time() < 120:  # 临近过期（<2min）提前刷新
            await self._refresh_token()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.auth.get('access_token', '')}",
            "chatgpt-account-id": self.account_id,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "session_id": self._session_id,
        }

    # ---------- Responses 请求 ----------
    def _build_input(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """把 chat/completions 风格 messages 转为 Responses 的 (instructions, input[])。"""
        instructions_parts: list[str] = []
        items: list[dict] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role == "system":
                if content:
                    instructions_parts.append(str(content))
            elif role == "user":
                items.append({"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": str(content or "")}]})
            elif role == "assistant":
                for call in m.get("tool_calls") or []:
                    fn = call.get("function", {})
                    items.append({"type": "function_call", "name": fn.get("name", ""),
                                  "arguments": fn.get("arguments", "{}"),
                                  "call_id": call.get("id", "")})
                if content:
                    items.append({"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": str(content)}]})
            elif role == "tool":
                items.append({"type": "function_call_output",
                              "call_id": m.get("tool_call_id", ""),
                              "output": str(content or "")})
        return "\n\n".join(instructions_parts), items

    @staticmethod
    def _tools_to_responses(tools: Optional[list[dict]]) -> list[dict]:
        """chat/completions 的 tools → Responses 的扁平 function 工具。"""
        out = []
        for t in tools or []:
            fn = t.get("function", {})
            if not fn.get("name"):
                continue
            out.append({"type": "function", "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters") or {"type": "object", "properties": {}}})
        return out

    async def _post_responses(self, instructions: str, input_items: list[dict],
                              tools: Optional[list[dict]] = None, on_delta=None) -> dict:
        """调用 Responses 流式端点，聚合出 {text, tool_calls}。401 时刷新令牌重试一次。

        on_delta：可选回调，文本增量逐段回传，用于前端边生成边显示（仅在鉴权通过、
        真正开始产出文本后触发，刷新令牌重试不会重复回调）。
        """
        from ..token_usage import ensure_usage_allowed
        ensure_usage_allowed()
        body = {
            "model": self.text_model,
            "instructions": instructions or "You are a helpful assistant.",
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if tools:
            body["tools"] = self._tools_to_responses(tools)
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = False

        await self._ensure_fresh()
        for attempt in range(2):
            try:
                return await self._stream_once(body, on_delta)
            except _Unauthorized:
                if attempt == 0:
                    await self._refresh_token()
                    continue
                raise RuntimeError("ChatGPT 鉴权失败：令牌已失效且刷新无效，请重新 codex login 后再导入")
        raise RuntimeError("ChatGPT 调用失败")

    async def _stream_once(self, body: dict, on_delta=None) -> dict:
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        usage_values: dict[str, int] = {}
        url = f"{self.base_url}/responses"
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            async with client.stream("POST", url, json=body, headers=self._headers()) as resp:
                if resp.status_code == 401:
                    raise _Unauthorized()
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    raise RuntimeError(f"ChatGPT Responses 返回 {resp.status_code}：{detail}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    etype = evt.get("type", "")
                    from ..token_usage import extract_usage
                    current_usage = extract_usage(evt)
                    if current_usage is not None:
                        for key, value in current_usage.items():
                            usage_values[key] = max(usage_values.get(key, 0), int(value or 0))
                    if etype == "response.output_text.delta":
                        delta = evt.get("delta", "")
                        text_parts.append(delta)
                        if delta and on_delta is not None:
                            on_delta(delta)
                    elif etype == "response.output_item.done":
                        item = evt.get("item", {})
                        if item.get("type") == "function_call":
                            tool_calls.append({
                                "id": item.get("call_id") or item.get("id") or str(uuid.uuid4()),
                                "type": "function",
                                "function": {"name": item.get("name", ""),
                                             "arguments": item.get("arguments", "{}")},
                            })
                    elif etype in ("response.failed", "error"):
                        msg = json.dumps(evt.get("response") or evt, ensure_ascii=False)[:300]
                        raise RuntimeError(f"ChatGPT 生成失败：{msg}")
        if usage_values:
            from ..token_usage import record_response_usage
            usage_values["total_tokens"] = max(
                usage_values.get("total_tokens", 0),
                usage_values.get("input_tokens", 0) + usage_values.get("output_tokens", 0),
            )
            record_response_usage(
                {"usage": usage_values},
                provider_id=self.provider_id,
                model=self.text_model,
            )
        return {"text": "".join(text_parts), "tool_calls": tool_calls}

    # ---------- 与 LLMClient 一致的接口 ----------
    async def list_models(self) -> list[str]:
        return available_chatgpt_codex_models()

    async def chat(self, system: str, user: str, temperature: float = 0.5, top_p=None) -> str:
        instructions, items = self._build_input([
            {"role": "system", "content": system},
            {"role": "user", "content": user or "请按系统提示输出。"},
        ])
        result = await self._post_responses(instructions, items)
        return result["text"]

    async def chat_stream(self, system: str, user: str, on_delta=None, temperature: float = 0.5) -> str:
        instructions, items = self._build_input([
            {"role": "system", "content": system},
            {"role": "user", "content": user or "请按系统提示输出。"},
        ])
        result = await self._post_responses(instructions, items, on_delta=on_delta)
        return result["text"]

    async def chat_messages_stream(self, messages: list[dict], on_delta=None, temperature: float = 0.5) -> str:
        instructions, items = self._build_input(messages)
        result = await self._post_responses(instructions, items, on_delta=on_delta)
        return result["text"]

    async def chat_with_tools(self, messages: list[dict], tools=None, temperature: float = 0.5) -> dict:
        instructions, items = self._build_input(messages)
        result = await self._post_responses(instructions, items, tools=tools)
        msg = {"role": "assistant", "content": result["text"]}
        if result["tool_calls"]:
            msg["tool_calls"] = result["tool_calls"]
        return msg

    async def vision(self, system: str, user: str, image_paths, temperature: float = 0.2) -> str:
        # 能力以通用模型元数据为准；当前 Codex 登录模型只声明文本输入。
        return ""

    async def ping(self) -> str:
        instructions, items = self._build_input([{"role": "user", "content": "ping，请回复 pong"}])
        result = await self._post_responses(instructions, items)
        return result["text"] or "(无文本输出)"


class _Unauthorized(Exception):
    pass
