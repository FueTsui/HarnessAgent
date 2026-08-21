"""协议驱动的多提供商 LLM 客户端。

运行时支持 OpenAI Responses、OpenAI Chat Completions 与 Anthropic
Messages 三条线路，厂商差异由 ModelProvider 的认证、版本、请求头、
额外请求参数及超时重试配置表达。模型输入能力由通用 input 模态声明。
"""
import asyncio
import base64
import copy
import inspect
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Optional

import httpx

from ..config import settings
from ..net_guard import validate_outbound_url
from .codex_models import (
    DEFAULT_CHATGPT_CODEX_MODEL,
    FALLBACK_CHATGPT_CODEX_MODELS,
)

logger = logging.getLogger(__name__)
_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?[^>]*>.*?(?:</think(?:ing)?>|$)",
    re.IGNORECASE | re.DOTALL,
)
_RETRYABLE_TOOL_PROTOCOL_ERRORS = (
    "unexpected tokens remaining in message header",
    "failed to parse tool call",
    "tool call parse error",
)
_HARMONY_TOOL_CHANNEL_SUFFIX_RE = re.compile(
    r"<\|channel\|>(?:analysis|commentary|final)\s*$",
    re.IGNORECASE,
)


def _normalize_tool_name(value) -> str:
    """移除 GPT-OSS Harmony 偶发泄漏到函数名末尾的 channel 标记。"""
    return _HARMONY_TOOL_CHANNEL_SUFFIX_RE.sub("", str(value or "")).strip()


def _normalize_assistant_message(message: dict) -> dict:
    """在协议边界规范化工具调用，不修改上游响应对象。"""
    if not isinstance(message, dict):
        return {}
    normalized = dict(message)
    calls = []
    for raw_call in message.get("tool_calls") or []:
        if not isinstance(raw_call, dict):
            continue
        call = dict(raw_call)
        function = call.get("function")
        if isinstance(function, dict):
            function = dict(function)
            function["name"] = _normalize_tool_name(function.get("name"))
            call["function"] = function
        calls.append(call)
    if "tool_calls" in message:
        normalized["tool_calls"] = calls
    return normalized


def explain_http_error(exc: "httpx.HTTPStatusError") -> str:
    """把上游 HTTP 错误翻译成可执行的中文提示（重点处理 OpenAI 等的 429/401/404）。"""
    resp = exc.response
    code = resp.status_code
    detail, ecode = "", ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or ""
            ecode = str(err.get("code") or err.get("type") or "")
        elif isinstance(err, str):
            detail = err
    except Exception:  # noqa: BLE001
        detail = (resp.text or "")[:300]
    tail = f"｜{detail}" if detail else ""
    if code == 429:
        if "insufficient_quota" in (ecode + detail):
            return ("429 额度不足（insufficient_quota）：该账户没有可用 API 额度或未开通计费。"
                    "请到 platform.openai.com 的 Billing 充值/绑定支付方式后重试" + tail)
        return ("429 触发限流（rate limit）：请求过于频繁或超出并发/速率上限，请稍后重试；"
                "若刚导入 Key 也可能是额度未生效" + tail)
    if code == 401:
        return "401 鉴权失败：API Key 无效/过期，或该 Key 无权访问此接口（注意区分 API Key 与 ChatGPT 登录令牌）" + tail
    if code == 404:
        return "404 未找到：模型名可能不存在或该账户无权访问，请核对文本模型名" + tail
    return f"{code} {resp.reason_phrase}{tail}"

# 进程级共享 httpx 客户端（连接池复用，M2）：在运行的事件循环内惰性创建，shutdown 时关闭。
_shared_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=settings.LLM_TIMEOUT,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _shared_client


async def aclose_http_client() -> None:
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None

# 各提供商预设（前端新建时自动填充；模型通过通用模型配置选择）
PROVIDER_PRESETS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "model_id": "deepseek-chat",
        "model_name": "DeepSeek Chat",
        "model_reasoning": False,
        "model_input": ["text"],
        "key_hint": "sk-...（platform.deepseek.com 申请）",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "qwen": {
        "label": "通义千问（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_id": "qwen3-max",
        "model_name": "Qwen 3 Max",
        "model_reasoning": False,
        "model_input": ["text"],
        "key_hint": "sk-...（bailian.console.aliyun.com 申请）",
        "models": ["qwen3-max", "qwen-plus-latest", "qwen-turbo", "qwen3-vl-plus", "qwen-vl-max"],
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model_id": "deepseek-ai/deepseek-v3.1",
        "model_name": "DeepSeek V3.1",
        "model_reasoning": False,
        "model_input": ["text"],
        "key_hint": "nvapi-...（build.nvidia.com 申请）；base_url 可改为自建 NIM 地址",
        "models": [],  # NIM 目录很大，以「识别可用模型」实时拉取为准
    },
    "openai": {
        "label": "OpenAI 兼容接口",
        "base_url": "https://api.openai.com/v1",
        "model_id": "",
        "model_name": "",
        "model_reasoning": False,
        "model_input": ["text"],
        "wire_api": "responses",
        "auth_type": "bearer",
        "api_version": "",
        "api_version_mode": "none",
        "models": [],
    },
    "anthropic": {
        "label": "Anthropic 兼容接口",
        "base_url": "https://api.anthropic.com/v1",
        "model_id": "",
        "model_name": "",
        "model_reasoning": False,
        "model_input": ["text"],
        "wire_api": "messages",
        "auth_type": "x_api_key",
        "api_version": "2023-06-01",
        "api_version_mode": "header",
        "models": [],
    },
    "chatgpt": {
        "label": "ChatGPT 订阅（Codex 登录）",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "model_id": DEFAULT_CHATGPT_CODEX_MODEL,
        "model_name": "Codex",
        "model_reasoning": True,
        "model_input": ["text"],
        "wire_api": "responses",
        "auth_type": "bearer",
        "key_hint": "用「从 Codex CLI 导入」接入；需先用 ChatGPT 账号 codex login",
        "models": list(FALLBACK_CHATGPT_CODEX_MODELS),
    },
    "custom": {
        "label": "自定义 OpenAI 兼容接口",
        "base_url": "http://127.0.0.1:8001/v1",
        "model_id": "",
        "model_name": "",
        "model_reasoning": False,
        "model_input": ["text"],
        "key_hint": "按目标服务要求填写；无鉴权可填任意占位符",
        "models": [],
    },
}


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_id: str,
        model_input: Optional[list[str]] = None,
        vision_model_id: str = "",
        vision_fallback: "Optional[LLMClient]" = None,
        enforce_ssrf: bool = False,
        context_window: int = 0,
        provider_type: str = "openai",
        wire_api: str = "chat_completions",
        auth_type: str = "bearer",
        auth_header: str = "",
        api_version: str = "",
        api_version_mode: str = "none",
        custom_headers: Optional[dict] = None,
        extra_body: Optional[dict] = None,
        model_list_path: str = "/models",
        reasoning_effort: str = "",
        max_tokens: int = 8192,
        max_tokens_param: str = "auto",
        timeout_ms: int = 120000,
        max_retries: int = 3,
        stream_max_retries: int = 3,
        stream_idle_timeout_ms: int = 300000,
        supports_temperature: bool = True,
        provider_id: int | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.provider_type = provider_type or "openai"
        self.wire_api = wire_api or ("messages" if self.provider_type == "anthropic" else "chat_completions")
        self.auth_type = auth_type or ("x_api_key" if self.wire_api == "messages" else "bearer")
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_type == "bearer" and api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        elif self.auth_type == "api_key" and api_key:
            self.headers["api-key"] = api_key
        elif self.auth_type == "x_api_key" and api_key:
            self.headers["x-api-key"] = api_key
        elif self.auth_type == "custom" and auth_header and api_key:
            self.headers[auth_header] = api_key
        self.headers.update({
            str(key): str(value)
            for key, value in (custom_headers or {}).items()
            if key and value is not None
        })
        self.api_version = api_version or ""
        self.api_version_mode = api_version_mode or "none"
        if self.api_version and self.api_version_mode == "header":
            header = "anthropic-version" if self.wire_api == "messages" else "api-version"
            self.headers[header] = self.api_version
        if self.wire_api == "messages" and "anthropic-version" not in {
            key.lower(): value for key, value in self.headers.items()
        }:
            self.headers["anthropic-version"] = "2023-06-01"
        self.query_params = (
            {"api-version": self.api_version}
            if self.api_version and self.api_version_mode == "query" else None
        )
        self.model_id = model_id
        self.model_input = list(dict.fromkeys(model_input or ["text"]))
        # 内部请求构造仍统一读这两个槽位；Provider 不再分别配置它们。
        self.text_model = model_id
        self.vision_model = (
            model_id if "image" in self.model_input else vision_model_id
        )
        self.vision_fallback = vision_fallback
        self.context_tokens = context_window or settings.LLM_CONTEXT_TOKENS
        self.extra_body = dict(extra_body or {})
        self.reasoning_effort = reasoning_effort or ""
        self.max_output_tokens = max(1, int(max_tokens or 8192))
        self.max_tokens_param = max_tokens_param or "auto"
        self.timeout_ms = max(1000, int(timeout_ms or 120000))
        self.max_retries = max(0, int(max_retries or 0))
        self.stream_max_retries = max(0, int(stream_max_retries or 0))
        self.stream_idle_timeout_ms = max(1000, int(stream_idle_timeout_ms or self.timeout_ms))
        self.supports_temperature = bool(supports_temperature)
        self.provider_id = provider_id
        path = str(model_list_path or "/models").strip()
        self.model_list_path = path if path.startswith("/") else f"/{path}"
        # 管理员自建提供商需做 SSRF 校验；.env 默认本地模型为操作者可信，不校验。
        self.enforce_ssrf = enforce_ssrf

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _timeout(self, *, stream: bool = False) -> httpx.Timeout:
        total = self.timeout_ms / 1000
        read = self.stream_idle_timeout_ms / 1000 if stream else total
        return httpx.Timeout(total, read=read)

    def _body(self, payload: dict, protocol: str | None = None) -> dict:
        protocol = protocol or self.wire_api
        body = {**self.extra_body, **payload}
        if not self.supports_temperature:
            body.pop("temperature", None)
        if self.reasoning_effort:
            if protocol == "responses":
                body["reasoning"] = {"effort": self.reasoning_effort}
            elif protocol == "messages":
                body["effort"] = self.reasoning_effort
            else:
                body["reasoning_effort"] = self.reasoning_effort
        token_param = self.max_tokens_param
        if token_param == "auto":
            token_param = {
                "responses": "max_output_tokens",
                "messages": "max_tokens",
                "chat_completions": "max_completion_tokens",
            }.get(protocol, "max_tokens")
        if token_param != "none":
            body.setdefault(token_param, self.max_output_tokens)
        return body

    @staticmethod
    def _upstream_error_text(exc: httpx.HTTPStatusError) -> str:
        try:
            data = exc.response.json()
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                return str(error.get("message") or error)
            return str(error or data)
        except Exception:  # noqa: BLE001
            return str(exc.response.text or "")

    def _apply_parameter_compatibility(
        self, payload: dict, exc: httpx.HTTPStatusError
    ) -> bool:
        """根据上游明确的 400 提示做一次无损参数降级。

        仅在 ``auto`` 模式切换两种 Chat Completions 输出长度字段；用户
        显式选择的字段不会被覆盖。部分推理模型拒绝 temperature，此时
        删除该可选字段后重试。
        """
        if exc.response.status_code != 400:
            return False
        detail = self._upstream_error_text(exc).lower()
        if self.max_tokens_param == "auto":
            if (
                "max_tokens" in payload
                and "max_tokens" in detail
                and ("max_completion_tokens" in detail or "unsupported parameter" in detail)
            ):
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                return True
            if (
                "max_completion_tokens" in payload
                and "max_completion_tokens" in detail
                and ("max_tokens" in detail or "unsupported parameter" in detail)
            ):
                payload["max_tokens"] = payload.pop("max_completion_tokens")
                return True
        if "temperature" in payload and "temperature" in detail and (
            "unsupported parameter" in detail or "does not support" in detail
        ):
            payload.pop("temperature", None)
            return True
        if "stream_options" in payload and "stream_options" in detail and (
            "unsupported parameter" in detail
            or "unknown parameter" in detail
            or "does not support" in detail
        ):
            # 老旧 OpenAI 兼容网关可能不支持 include_usage；优先保住流式答复，
            # 但支持标准协议的网关会返回最终 usage chunk 供精确计量。
            payload.pop("stream_options", None)
            return True
        return False

    async def _request_json(
        self,
        path: str,
        payload: dict,
        retries: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        from ..token_usage import ensure_usage_allowed
        ensure_usage_allowed()
        await self._guard()
        retry_count = self.max_retries if retries is None else max(0, retries)
        last_error = ""
        attempt = 0
        compatibility_adjustments = 0
        while attempt <= retry_count:
            try:
                response = await get_http_client().post(
                    self._url(path), json=payload,
                    headers=self.headers if headers is None else headers,
                    params=self.query_params, timeout=self._timeout(),
                )
                response.raise_for_status()
                data = response.json()
                from ..token_usage import record_response_usage
                record_response_usage(
                    data, provider_id=self.provider_id, model=self.text_model
                )
                return data
            except httpx.HTTPStatusError as exc:
                last_error = explain_http_error(exc)
                if (
                    compatibility_adjustments < 2
                    and self._apply_parameter_compatibility(payload, exc)
                ):
                    compatibility_adjustments += 1
                    continue
                if exc.response.status_code in (400, 401, 403, 404) or "insufficient_quota" in last_error:
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if attempt < retry_count:
                await asyncio.sleep(float(attempt + 1))
            attempt += 1
        raise RuntimeError(last_error or "上游接口未返回有效响应")

    async def _guard(self) -> None:
        if self.enforce_ssrf:
            await asyncio.to_thread(validate_outbound_url, self.base_url)

    async def _post(self, payload: dict) -> dict:
        """带重试地 POST /chat/completions，返回完整响应 JSON。"""
        body = self._body(payload, "chat_completions")
        semantic_attempt = 0
        while True:
            data = await self._request_json("/chat/completions", body)
            try:
                self._first_message(data)
            except RuntimeError as exc:
                detail = str(exc).lower()
                retryable = any(
                    marker in detail for marker in _RETRYABLE_TOOL_PROTOCOL_ERRORS
                )
                if not retryable or semantic_attempt >= self.max_retries:
                    raise
                semantic_attempt += 1
                logger.warning(
                    "上游工具协议解析失败，重试 Chat Completions（%s/%s）：%s",
                    semantic_attempt,
                    self.max_retries,
                    exc,
                )
                await asyncio.sleep(float(semantic_attempt))
                continue
            return data

    @staticmethod
    def _first_message(data: dict) -> dict:
        """从响应中取第一个 choice 的 message；无 choices 时抛出可读错误。

        部分提供商（如个别 NVIDIA NIM 部署）在工具/参数不被接受时，会以 HTTP 200 返回
        错误体（含 message/error/detail 而无 choices），直接 data["choices"] 会得到不知所云的
        KeyError: 'choices'。这里统一转成带原因的 RuntimeError。
        """
        choices = data.get("choices") if isinstance(data, dict) else None
        if choices:
            return _normalize_assistant_message(choices[0].get("message") or {})
        reason = ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                reason = err.get("message") or str(err)
            reason = reason or err if isinstance(err, str) else reason
            reason = reason or data.get("message") or data.get("detail") or ""
        import json as _json
        snippet = _json.dumps(data, ensure_ascii=False)[:600]
        raise RuntimeError(f"模型返回缺少 choices（疑似上游错误或工具/参数不被接受）：{reason or snippet}")

    async def _chat_completion(self, payload: dict) -> str:
        data = await self._post(payload)
        return self._first_message(data).get("content") or ""

    @staticmethod
    def _visible_assistant_text(content) -> str:
        """移除常见内嵌思考块，判断是否存在可展示正文。"""
        if not isinstance(content, str):
            return ""
        return _THINK_BLOCK_RE.sub("", content).strip()

    @classmethod
    def _has_assistant_output(cls, message: dict) -> bool:
        """正文或工具调用任一存在，即是可消费的 assistant 输出。"""
        content = message.get("content")
        if isinstance(content, str) and cls._visible_assistant_text(content):
            return True
        if isinstance(content, list) and content:
            return True
        return bool(message.get("tool_calls"))

    def _force_final_answer_mode(self, payload: dict) -> None:
        """按模型能力关闭/压低推理，确保输出预算留给用户正文。"""
        model = (self.text_model or "").lower()
        if self.wire_api != "chat_completions":
            return
        if "nemotron-3" in model:
            kwargs = payload.get("chat_template_kwargs")
            kwargs = dict(kwargs) if isinstance(kwargs, dict) else {}
            kwargs["enable_thinking"] = False
            kwargs.pop("low_effort", None)
            payload["chat_template_kwargs"] = kwargs
            payload.pop("reasoning_budget", None)
        elif "gpt-oss" in model:
            payload["reasoning_effort"] = "low"

    async def _recover_empty_message(
        self,
        messages: list[dict],
        temperature: float,
        *,
        reasoning_chars: int = 0,
    ) -> dict:
        """恢复非流式 reasoning-only/空 assistant 消息。

        不把 reasoning_content 当成用户答案；追加最终答复约束、移除工具并仅重试一次。
        """
        instruction = (
            "只输出给用户的最终答复，不展示内部推理过程，不再调用工具。"
            "优先使用已有证据，直接、完整地回答；不得返回空内容。"
        )
        recovery_messages = [
            *messages,
            {"role": "system", "content": instruction},
        ]
        if self.wire_api == "responses":
            payload = self._responses_payload(recovery_messages)
            if self.supports_temperature:
                payload["temperature"] = temperature
            if reasoning_chars or "gpt-oss" in (self.text_model or "").lower():
                payload["reasoning"] = {"effort": "low"}
            parsed = self._parse_responses(
                await self._request_json("/responses", payload, retries=0)
            )
        elif self.wire_api == "messages":
            payload = self._anthropic_payload(recovery_messages)
            if self.supports_temperature:
                payload["temperature"] = temperature
            if reasoning_chars:
                payload["effort"] = "low"
            parsed = self._parse_anthropic(
                await self._request_json("/messages", payload, retries=0)
            )
        else:
            payload = self._body({
                "model": self.text_model,
                "messages": recovery_messages,
                "temperature": temperature,
            }, "chat_completions")
            self._force_final_answer_mode(payload)
            data = await self._request_json("/chat/completions", payload, retries=0)
            parsed = self._first_message(data)
            if not reasoning_chars:
                reasoning_chars = len(str(
                    parsed.get("reasoning_content") or parsed.get("reasoning") or ""
                ))
        if self._has_assistant_output(parsed):
            logger.info(
                "LLM 空消息已通过非流式最终答复请求恢复（%s，推理字符=%s）",
                self.base_url, reasoning_chars,
            )
            return parsed
        raise RuntimeError(
            "模型未返回可展示的最终答复"
            + (f"（仅返回 {reasoning_chars} 字符内部推理）" if reasoning_chars else "")
        )

    @staticmethod
    def _responses_content(content):
        if isinstance(content, str):
            return content
        out = []
        for item in content or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                out.append({"type": "input_text", "text": item.get("text") or ""})
            elif item.get("type") == "image_url":
                image = item.get("image_url") or {}
                out.append({"type": "input_image", "image_url": image.get("url") or ""})
        return out or ""

    def _responses_payload(self, messages: list[dict], tools=None) -> dict:
        instructions = []
        inputs = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                instructions.append(str(message.get("content") or ""))
                continue
            if role == "tool":
                inputs.append({
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id") or "",
                    "output": str(message.get("content") or ""),
                })
                continue
            content = message.get("content")
            if content:
                inputs.append({"role": role, "content": self._responses_content(content)})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                inputs.append({
                    "type": "function_call",
                    "call_id": call.get("id") or "",
                    "name": function.get("name") or "",
                    "arguments": function.get("arguments") or "{}",
                })
        payload: dict = {"model": self.text_model, "input": inputs}
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": (tool.get("function") or {}).get("name"),
                    "description": (tool.get("function") or {}).get("description", ""),
                    "parameters": (tool.get("function") or {}).get("parameters", {"type": "object"}),
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
        return self._body(payload, "responses")

    @staticmethod
    def _parse_responses(data: dict) -> dict:
        texts = []
        calls = []
        if isinstance(data.get("output_text"), str):
            texts.append(data["output_text"])
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                calls.append({
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                })
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    texts.append(str(block.get("text") or ""))
        return _normalize_assistant_message({
            "role": "assistant", "content": "".join(texts), "tool_calls": calls,
        })

    @staticmethod
    def _anthropic_image(block: dict) -> dict | None:
        image = block.get("image_url") or {}
        url = str(image.get("url") or "")
        if not url.startswith("data:") or ";base64," not in url:
            return None
        meta, data = url.split(";base64,", 1)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": meta[5:], "data": data},
        }

    def _anthropic_payload(self, messages: list[dict], tools=None) -> dict:
        systems = []
        converted = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                systems.append(str(message.get("content") or ""))
                continue
            if role == "tool":
                converted.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": message.get("tool_call_id") or "",
                        "content": str(message.get("content") or ""),
                    }],
                })
                continue
            blocks = []
            content = message.get("content")
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        blocks.append({"type": "text", "text": block.get("text") or ""})
                    elif block.get("type") == "image_url":
                        image_block = self._anthropic_image(block)
                        if image_block:
                            blocks.append(image_block)
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    tool_input = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    tool_input = {}
                blocks.append({
                    "type": "tool_use", "id": call.get("id") or "",
                    "name": function.get("name") or "", "input": tool_input,
                })
            if blocks:
                converted.append({"role": "assistant" if role == "assistant" else "user", "content": blocks})
        payload: dict = {"model": self.text_model, "messages": converted}
        if systems:
            payload["system"] = "\n\n".join(systems)
        if tools:
            payload["tools"] = [
                {
                    "name": (tool.get("function") or {}).get("name"),
                    "description": (tool.get("function") or {}).get("description", ""),
                    "input_schema": (tool.get("function") or {}).get("parameters", {"type": "object"}),
                }
                for tool in tools
            ]
            payload["tool_choice"] = {"type": "auto"}
        return self._body(payload, "messages")

    @staticmethod
    def _parse_anthropic(data: dict) -> dict:
        texts = []
        calls = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                calls.append({
                    "id": block.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })
        return _normalize_assistant_message({
            "role": "assistant", "content": "".join(texts), "tool_calls": calls,
        })

    async def chat_with_tools(
        self, messages: list[dict], tools: Optional[list[dict]] = None, temperature: float = 1
    ) -> dict:
        """工具调用对话：返回完整 assistant message（可能含 tool_calls）。"""
        if self.wire_api == "responses":
            payload = self._responses_payload(messages, tools)
            if self.supports_temperature:
                payload["temperature"] = temperature
            parsed = self._parse_responses(await self._request_json("/responses", payload))
            if self._has_assistant_output(parsed):
                return parsed
            return await self._recover_empty_message(messages, temperature)
        if self.wire_api == "messages":
            payload = self._anthropic_payload(messages, tools)
            if self.supports_temperature:
                payload["temperature"] = temperature
            parsed = self._parse_anthropic(await self._request_json("/messages", payload))
            if self._has_assistant_output(parsed):
                return parsed
            return await self._recover_empty_message(messages, temperature)
        # GPT-OSS 20B 在较高随机性、多轮工具历史下偶发生成不合法的 Harmony
        # message header。只收紧工具决策温度，不改变无工具的最终文本生成参数。
        effective_temperature = temperature
        if tools and "gpt-oss" in (self.text_model or "").lower():
            effective_temperature = min(float(temperature), 0.2)
        payload: dict = {
            "model": self.text_model,
            "messages": messages,
            "temperature": effective_temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = await self._post(payload)
        parsed = self._first_message(data)
        if self._has_assistant_output(parsed):
            return parsed
        reasoning_chars = len(str(
            parsed.get("reasoning_content") or parsed.get("reasoning") or ""
        ))
        return await self._recover_empty_message(
            messages, temperature, reasoning_chars=reasoning_chars
        )

    async def chat_messages_stream(
        self,
        messages: list[dict],
        on_delta=None,
        temperature: float = 0.5,
    ) -> str:
        """按当前线路消费 SSE 文本增量，并将增量传给前端。"""
        from ..token_usage import ensure_usage_allowed
        ensure_usage_allowed()
        await self._guard()
        if self.wire_api == "responses":
            path = "/responses"
            payload = self._responses_payload(messages)
            if self.supports_temperature:
                payload["temperature"] = temperature

            def event_text(event: dict) -> str:
                if event.get("type") == "response.output_text.delta":
                    return str(event.get("delta") or "")
                return ""

            def response_text(data: dict) -> str:
                return self._parse_responses(data).get("content") or ""
        elif self.wire_api == "messages":
            path = "/messages"
            payload = self._anthropic_payload(messages)
            if self.supports_temperature:
                payload["temperature"] = temperature

            def event_text(event: dict) -> str:
                delta = event.get("delta") or {}
                if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    return str(delta.get("text") or "")
                return ""

            def response_text(data: dict) -> str:
                return self._parse_anthropic(data).get("content") or ""
        else:
            path = "/chat/completions"
            payload = self._body({
                "model": self.text_model,
                "messages": messages,
                "temperature": temperature,
                "stream_options": {"include_usage": True},
            }, "chat_completions")

            def event_text(event: dict) -> str:
                choices = event.get("choices") if isinstance(event, dict) else None
                if not choices:
                    return ""
                delta = (choices[0].get("delta") or {}).get("content") or ""
                if isinstance(delta, list):
                    return "".join(
                        str(item.get("text") or "")
                        for item in delta
                        if isinstance(item, dict)
                    )
                return delta if isinstance(delta, str) else ""

            def response_text(data: dict) -> str:
                return self._first_message(data).get("content") or ""
        payload["stream"] = True
        self._force_final_answer_mode(payload)

        async def recover_empty_stream(
            reasoning_chars: int,
            finish_reason: str,
        ) -> tuple[str, str]:
            """空流改走一次非流式最终答复。

            GPT-OSS 等推理模型可能在输出预算内只产生 ``reasoning_content``，
            正文 ``content`` 始终为空。对这种已正常结束的 SSE 重放相同请求没有
            意义，因此用低推理强度和明确的最终答复指令做一次非流式恢复。
            推理文本只用于判定，不返回给用户。
            """
            recovery = copy.deepcopy(payload)
            recovery["stream"] = False
            # OpenAI 兼容协议只允许 stream_options 与 stream=true 同时出现。
            # 空流恢复是非流式请求，必须移除原流式请求携带的 usage 选项。
            recovery.pop("stream_options", None)
            instruction = (
                "只输出给用户的最终答复，不展示内部推理过程，不再调用工具。"
                "优先使用已有证据，直接、完整地回答。"
            )
            if self.wire_api == "responses":
                existing = str(recovery.get("instructions") or "")
                recovery["instructions"] = f"{existing}\n\n{instruction}".strip()
            elif self.wire_api == "messages":
                existing = str(recovery.get("system") or "")
                recovery["system"] = f"{existing}\n\n{instruction}".strip()
            else:
                recovery["messages"] = [
                    *(recovery.get("messages") or []),
                    {"role": "system", "content": instruction},
                ]
            model_name = (self.text_model or "").lower()
            if reasoning_chars or "gpt-oss" in model_name:
                if self.wire_api == "responses":
                    recovery["reasoning"] = {"effort": "low"}
                elif self.wire_api == "messages":
                    recovery["effort"] = "low"
                elif "gpt-oss" in model_name:
                    recovery["reasoning_effort"] = "low"
            self._force_final_answer_mode(recovery)
            try:
                data = await self._request_json(path, recovery, retries=0)
                text = response_text(data)
            except Exception as exc:  # noqa: BLE001
                return "", f"非流式恢复失败：{exc}"
            if self._visible_assistant_text(text):
                return text, ""
            nonstream_reasoning = ""
            if self.wire_api == "chat_completions":
                try:
                    message = self._first_message(data)
                    nonstream_reasoning = str(
                        message.get("reasoning_content") or message.get("reasoning") or ""
                    )
                except Exception:
                    pass
            detail = (
                f"上游仅返回推理内容，未返回最终正文"
                f"（流式推理 {reasoning_chars} 字符"
                f"{f'，非流式推理 {len(nonstream_reasoning)} 字符' if nonstream_reasoning else ''}"
                f"{f'，finish_reason={finish_reason}' if finish_reason else ''}）"
            )
            return "", detail

        last_error = ""
        url = self._url(path)
        attempt = 0
        compatibility_adjustments = 0
        while attempt <= self.stream_max_retries:
            parts: list[str] = []
            emitted = False
            reasoning_chars = 0
            finish_reason = ""
            usage_values: dict[str, int] = {}
            try:
                async with get_http_client().stream(
                    "POST", url, json=payload, headers=self.headers,
                    params=self.query_params, timeout=self._timeout(stream=True),
                ) as resp:
                    # ``AsyncClient.stream`` 不会预先读取响应体。若上游返回 4xx/5xx，
                    # ``raise_for_status`` 产生的 HTTPStatusError 会交给下方统一错误
                    # 翻译与参数兼容逻辑；这些逻辑需要读取 ``response.json/text``。
                    # 先消费错误体，避免 httpx.ResponseNotRead 掩盖真正的上游错误，
                    # 同时让基于错误正文的无损参数降级能够生效。
                    if resp.status_code >= 400:
                        await resp.aread()
                    resp.raise_for_status()
                    if "text/event-stream" not in resp.headers.get("content-type", ""):
                        raw_body = await resp.aread()
                        data = json.loads(raw_body.decode("utf-8"))
                        from ..token_usage import record_response_usage
                        record_response_usage(
                            data, provider_id=self.provider_id, model=self.text_model
                        )
                        text = response_text(data)
                        if self._visible_assistant_text(text):
                            if on_delta is not None:
                                result = on_delta(text)
                                if inspect.isawaitable(result):
                                    await result
                            return text
                        recovered, recovery_error = await recover_empty_stream(0, "")
                        if self._visible_assistant_text(recovered):
                            if on_delta is not None:
                                result = on_delta(recovered)
                                if inspect.isawaitable(result):
                                    await result
                            return recovered
                        last_error = recovery_error or "上游非流式响应未返回正文"
                        break
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        error = event.get("error") if isinstance(event, dict) else None
                        if error:
                            raise RuntimeError(str(error))
                        from ..token_usage import extract_usage
                        current_usage = extract_usage(event) if isinstance(event, dict) else None
                        if current_usage is not None:
                            for key, value in current_usage.items():
                                usage_values[key] = max(usage_values.get(key, 0), int(value or 0))
                        if self.wire_api == "chat_completions" and isinstance(event, dict):
                            choices = event.get("choices") or []
                            if choices:
                                choice = choices[0] or {}
                                delta_obj = choice.get("delta") or {}
                                reasoning = (
                                    delta_obj.get("reasoning_content")
                                    or delta_obj.get("reasoning")
                                    or ""
                                )
                                if isinstance(reasoning, str):
                                    reasoning_chars += len(reasoning)
                                finish_reason = str(
                                    choice.get("finish_reason") or finish_reason or ""
                                )
                        delta = event_text(event) if isinstance(event, dict) else ""
                        if not delta:
                            continue
                        parts.append(delta)
                        emitted = True
                        if on_delta is not None:
                            result = on_delta(delta)
                            if inspect.isawaitable(result):
                                await result
                combined = "".join(parts)
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
                if not self._visible_assistant_text(combined):
                    recovered, recovery_error = await recover_empty_stream(
                        reasoning_chars, finish_reason
                    )
                    if self._visible_assistant_text(recovered):
                        if on_delta is not None:
                            result = on_delta(recovered)
                            if inspect.isawaitable(result):
                                await result
                        logger.info(
                            "LLM 空流已通过非流式请求恢复（%s，推理字符=%s，finish_reason=%s）",
                            self.base_url, reasoning_chars, finish_reason or "unknown",
                        )
                        return recovered
                    last_error = recovery_error or "上游流式响应结束但未返回文本"
                    logger.warning(
                        "LLM 流式调用返回空正文（%s）: %s",
                        self.base_url, last_error,
                    )
                    # SSE 已正常结束且非流式恢复也失败；重复同一流式请求只会
                    # 消耗预算并得到相同结果，直接退出。
                    break
                return combined
            except httpx.HTTPStatusError as exc:
                last_error = explain_http_error(exc)
                logger.warning(
                    "LLM 流式调用失败（%s 第 %s 次）: %s",
                    self.base_url, attempt + 1, last_error,
                )
                if (
                    not emitted
                    and compatibility_adjustments < 2
                    and self._apply_parameter_compatibility(payload, exc)
                ):
                    compatibility_adjustments += 1
                    continue
                if emitted or exc.response.status_code in (401, 403):
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning(
                    "LLM 流式调用失败（%s 第 %s 次）: %s",
                    self.base_url, attempt + 1, last_error,
                )
                # 已向前端发送过增量时不能重试，否则会重复输出。
                if emitted:
                    break
            if attempt < self.stream_max_retries:
                await asyncio.sleep(float(attempt + 1))
            attempt += 1
        raise RuntimeError(f"LLM 流式调用失败：{last_error or '上游未返回内容'}")

    async def chat(
        self,
        system: str,
        user: str,
        temperature: float = 1,
        top_p: Optional[float] = None,
    ) -> str:
        """纯文本对话节点。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user or "请按系统提示输出。"},
        ]
        if self.wire_api != "chat_completions":
            return (await self.chat_with_tools(messages, temperature=temperature)).get("content") or ""
        payload: dict = {
            "model": self.text_model,
            "messages": messages,
            "temperature": temperature,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        return await self._chat_completion(payload)

    async def vision(
        self,
        system: str,
        user: str,
        image_paths: list[Path],
        temperature: float = 0.2,
    ) -> str:
        """视觉节点：卫星图 / CAD 图纸 / 电费单。无图片时返回空串（对应原工作流分支静默）。"""
        if not image_paths:
            return ""
        if not self.vision_model:
            if self.vision_fallback is not None:
                return await self.vision_fallback.vision(system, user, image_paths, temperature)
            return ""
        content: list[dict] = [{"type": "text", "text": user or "请分析图片。"}]
        for path in image_paths[: settings.MAX_FILES_PER_FIELD]:
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            b64 = base64.b64encode(path.read_bytes()).decode()
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        if self.wire_api == "responses":
            payload = self._responses_payload(messages)
            payload["model"] = self.vision_model
            if self.supports_temperature:
                payload["temperature"] = temperature
            return self._parse_responses(
                await self._request_json("/responses", payload)
            ).get("content") or ""
        if self.wire_api == "messages":
            payload = self._anthropic_payload(messages)
            payload["model"] = self.vision_model
            if self.supports_temperature:
                payload["temperature"] = temperature
            return self._parse_anthropic(
                await self._request_json("/messages", payload)
            ).get("content") or ""
        payload = {
            "model": self.vision_model, "messages": messages, "temperature": temperature,
        }
        return await self._chat_completion(payload)

    async def list_models(self) -> list[str]:
        """按配置的模型列表路径识别当前凭据可用的模型。"""
        await self._guard()
        params = dict(self.query_params or {})
        if self.wire_api == "messages":
            params.setdefault("limit", 1000)
        resp = await get_http_client().get(
            self._url(self.model_list_path), headers=self.headers,
            params=params or None, timeout=self._timeout(),
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(explain_http_error(exc)) from exc
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        out = [str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")]
        return sorted(set(out))

    async def ping(self) -> str:
        """连通性测试：返回模型一句应答，异常时抛错。"""
        if not self.text_model:
            raise RuntimeError("请先选择文本模型")
        result = await self.chat_with_tools(
            [{"role": "user", "content": "ping，请只回复 pong"}],
            temperature=0.2,
        )
        return result.get("content") or ""


# 默认本地客户端（.env 配置；provider_id 为空的智能体使用它）
llm_client = LLMClient(
    base_url=settings.LLM_BASE_URL,
    api_key=settings.LLM_API_KEY,
    model_id=settings.LLM_TEXT_MODEL,
    model_input=["text"],
    vision_model_id=settings.LLM_VISION_MODEL,
    context_window=settings.LLM_CONTEXT_TOKENS,
)


def client_for_provider(provider):
    """根据 ModelProvider 记录构建客户端；provider 为 None 时返回默认本地客户端。"""
    if provider is None:
        return llm_client
    provider_base = str(getattr(provider, "base_url", "") or "").rstrip("/").lower()
    if (
        getattr(provider, "provider_type", "") == "chatgpt"
        or provider_base == "https://chatgpt.com/backend-api/codex"
    ):
        from .chatgpt_client import ChatGPTClient
        return ChatGPTClient(provider)
    return LLMClient(
        base_url=provider.base_url,
        api_key=provider.api_key,
        model_id=provider.model_id,
        model_input=(
            json.loads(getattr(provider, "model_input", '["text"]') or '["text"]')
            if isinstance(getattr(provider, "model_input", None), str)
            else getattr(provider, "model_input", ["text"])
        ),
        enforce_ssrf=True,           # 管理员自建提供商做 SSRF 校验
        context_window=getattr(provider, "context_window", 0),
        provider_type=getattr(provider, "provider_type", "openai"),
        wire_api=getattr(provider, "wire_api", "chat_completions"),
        auth_type=getattr(provider, "auth_type", "bearer"),
        auth_header=getattr(provider, "auth_header", ""),
        api_version=getattr(provider, "api_version", ""),
        api_version_mode=getattr(provider, "api_version_mode", "none"),
        custom_headers=json.loads(getattr(provider, "custom_headers", "{}") or "{}"),
        extra_body=json.loads(getattr(provider, "extra_body", "{}") or "{}"),
        model_list_path=getattr(provider, "model_list_path", "/models"),
        reasoning_effort=(
            getattr(provider, "reasoning_effort", "")
            if getattr(provider, "model_reasoning", False)
            else ""
        ),
        max_tokens=getattr(provider, "max_tokens", 8192),
        max_tokens_param=getattr(provider, "max_tokens_param", "auto"),
        timeout_ms=getattr(provider, "timeout_ms", 120000),
        max_retries=getattr(provider, "max_retries", 3),
        stream_max_retries=getattr(provider, "stream_max_retries", 3),
        stream_idle_timeout_ms=getattr(provider, "stream_idle_timeout_ms", 300000),
        supports_temperature=getattr(provider, "supports_temperature", True),
        # 保存前识别模型时 provider 是没有数据库 id 的临时对象。
        provider_id=getattr(provider, "id", None),
    )
