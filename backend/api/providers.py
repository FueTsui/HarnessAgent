"""协议驱动的模型提供商管理（root/admin）。

- OpenAI 兼容：Responses 或 Chat Completions
- Anthropic 兼容：Messages
- 保存前模型发现、脱敏自定义请求头、通用生成与可靠性参数
- Codex 导入：读取本机 Codex CLI 登录凭据
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..llm.client import PROVIDER_PRESETS, client_for_provider
from ..llm.codex_models import (
    available_chatgpt_codex_models,
    preferred_chatgpt_codex_model,
)
from ..models import Agent, ModelProvider, TokenUsage, User
from ..schemas import ProviderCreate, ProviderOut, ProviderProbe, ProviderUpdate
from ..security import can_manage, require_module, require_owner, scope_owned

router = APIRouter(prefix="/api/v1/providers", tags=["模型提供商（root/admin）"])
MASKED_HEADER = "••••••••"
SENSITIVE_HEADER = re.compile(r"(authorization|api[-_]?key|token|secret)", re.I)
RESERVED_BODY_FIELDS = {
    "model", "messages", "input", "tools", "tool_choice", "stream", "system",
    "max_tokens", "max_completion_tokens", "max_output_tokens",
    "temperature", "reasoning", "reasoning_effort", "effort",
}
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _clean_headers(value: dict, existing: dict | None = None) -> dict:
    out = {}
    existing = existing or {}
    for raw_key, raw_value in (value or {}).items():
        key = str(raw_key or "").strip()
        if not key or len(key) > 128 or key.lower() in FORBIDDEN_HEADERS:
            continue
        text_value = str(raw_value or "")
        if text_value == MASKED_HEADER and key in existing:
            text_value = str(existing[key])
        if len(text_value) > 4096:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"请求头 {key} 的值过长")
        out[key] = text_value
    return out


def _clean_extra_body(value: dict) -> dict:
    out = {}
    for raw_key, raw_value in (value or {}).items():
        key = str(raw_key or "").strip()
        if not key or key in RESERVED_BODY_FIELDS:
            continue
        out[key] = raw_value
    encoded = json.dumps(out, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 32 * 1024:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "额外请求参数过大")
    return out


def _public_headers(value: dict) -> dict:
    return {
        key: (MASKED_HEADER if SENSITIVE_HEADER.search(key) else str(val))
        for key, val in value.items()
    }


def _model_input(value) -> list[str]:
    """规范化通用模型输入模态；未知值不会进入运行时能力判断。"""
    if isinstance(value, str):
        try:
            value = json.loads(value or '[]')
        except (json.JSONDecodeError, TypeError):
            value = []
    allowed = {"text", "image"}
    normalized = [str(item) for item in (value or []) if str(item) in allowed]
    return list(dict.fromkeys(normalized)) or ["text"]


def _to_out(p: ModelProvider, user: User | None = None) -> ProviderOut:
    return ProviderOut(
        id=p.id, name=p.name, provider_type=p.provider_type,
        base_url=p.base_url, model_id=p.model_id,
        model_name=getattr(p, "model_name", "") or "",
        model_reasoning=bool(getattr(p, "model_reasoning", False)),
        model_input=_model_input(getattr(p, "model_input", '["text"]')),
        context_window=int(getattr(p, "context_window", 0) or 0),
        wire_api=getattr(p, "wire_api", "chat_completions") or "chat_completions",
        auth_type=getattr(p, "auth_type", "bearer") or "bearer",
        auth_header=getattr(p, "auth_header", "") or "",
        api_version=getattr(p, "api_version", "") or "",
        api_version_mode=getattr(p, "api_version_mode", "none") or "none",
        custom_headers=_public_headers(_json_dict(getattr(p, "custom_headers", "{}"))),
        extra_body=_json_dict(getattr(p, "extra_body", "{}")),
        model_list_path=getattr(p, "model_list_path", "/models") or "/models",
        reasoning_effort=getattr(p, "reasoning_effort", "") or "",
        max_tokens=int(getattr(p, "max_tokens", 8192) or 8192),
        max_tokens_param=getattr(p, "max_tokens_param", "auto") or "auto",
        timeout_ms=int(getattr(p, "timeout_ms", 120000) or 120000),
        max_retries=int(getattr(p, "max_retries", 3) or 0),
        stream_max_retries=int(getattr(p, "stream_max_retries", 3) or 0),
        stream_idle_timeout_ms=int(getattr(p, "stream_idle_timeout_ms", 300000) or 300000),
        supports_temperature=bool(getattr(p, "supports_temperature", True)),
        enabled=p.enabled, has_key=bool(p.api_key),
        is_public=bool(getattr(p, "is_public", False)),
        can_manage=True if user is None else can_manage(user, p),
    )


@router.get("/presets")
def list_presets(_: User = Depends(require_module("providers"))):
    """前端新建提供商时的预设模板。"""
    return PROVIDER_PRESETS


@router.post("/discover-models")
async def discover_models(
    body: ProviderProbe,
    _: User = Depends(require_module("providers")),
):
    """保存前识别兼容接口公开给当前凭据的模型。"""
    if body.provider_type == "chatgpt":
        return {"models": available_chatgpt_codex_models(), "source": "codex-cache"}
    transient = SimpleNamespace(**body.model_dump())
    transient.id = None
    transient.custom_headers = json.dumps(_clean_headers(body.custom_headers), ensure_ascii=False)
    transient.extra_body = json.dumps(_clean_extra_body(body.extra_body), ensure_ascii=False)
    try:
        models = await client_for_provider(transient).list_models()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"识别模型失败：{exc}")
    return {"models": models, "source": "live"}


@router.post("/{provider_id}/discover-models")
async def discover_models_for_provider(
    provider_id: int,
    body: ProviderProbe,
    admin: User = Depends(require_module("providers")),
    db: Session = Depends(get_db),
):
    """用未保存的表单参数识别模型，空 API Key 复用现有加密域中的凭据。"""
    provider = db.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提供商不存在")
    require_owner(admin, provider)
    if (
        provider.provider_type == "chatgpt"
        or str(provider.base_url or "").rstrip("/").lower()
        == "https://chatgpt.com/backend-api/codex"
    ):
        return {"models": available_chatgpt_codex_models(), "source": "codex-cache"}
    values = body.model_dump()
    values["id"] = provider.id
    values["api_key"] = body.api_key or provider.api_key
    existing_headers = _json_dict(getattr(provider, "custom_headers", "{}"))
    values["custom_headers"] = json.dumps(
        _clean_headers(body.custom_headers, existing_headers), ensure_ascii=False
    )
    values["extra_body"] = json.dumps(_clean_extra_body(body.extra_body), ensure_ascii=False)
    try:
        models = await client_for_provider(SimpleNamespace(**values)).list_models()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"识别模型失败：{exc}")
    return {"models": models, "source": "live"}


@router.get("", response_model=list[ProviderOut])
def list_providers(admin: User = Depends(require_module("providers")), db: Session = Depends(get_db)):
    q = scope_owned(db.query(ModelProvider), ModelProvider, admin)
    return [_to_out(p, admin) for p in q.order_by(ModelProvider.id).all()]


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(
    body: ProviderCreate, admin: User = Depends(require_module("providers")), db: Session = Depends(get_db)
):
    if db.query(ModelProvider).filter(ModelProvider.name == body.name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "提供商名称已存在")
    provider = ModelProvider(
        name=body.name,
        provider_type=body.provider_type,
        base_url=body.base_url.rstrip("/"),
        api_key=body.api_key,
        model_id=body.model_id,
        model_name=body.model_name,
        model_reasoning=body.model_reasoning,
        model_input=json.dumps(_model_input(body.model_input)),
        context_window=body.context_window,
        wire_api=body.wire_api,
        auth_type=body.auth_type,
        auth_header=body.auth_header,
        api_version=body.api_version,
        api_version_mode=body.api_version_mode,
        custom_headers=json.dumps(_clean_headers(body.custom_headers), ensure_ascii=False),
        extra_body=json.dumps(_clean_extra_body(body.extra_body), ensure_ascii=False),
        model_list_path=body.model_list_path or "/models",
        reasoning_effort=body.reasoning_effort if body.model_reasoning else "",
        max_tokens=body.max_tokens,
        max_tokens_param=body.max_tokens_param,
        timeout_ms=body.timeout_ms,
        max_retries=body.max_retries,
        stream_max_retries=body.stream_max_retries,
        stream_idle_timeout_ms=body.stream_idle_timeout_ms,
        supports_temperature=body.supports_temperature,
        enabled=body.enabled,
        is_public=body.is_public,
        created_by=admin.id,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return _to_out(provider, admin)


@router.patch("/{provider_id}", response_model=ProviderOut)
def update_provider(
    provider_id: int,
    body: ProviderUpdate,
    admin: User = Depends(require_module("providers")),
    db: Session = Depends(get_db),
):
    provider = db.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提供商不存在")
    require_owner(admin, provider)
    is_chatgpt_codex = (
        provider.provider_type == "chatgpt"
        or str(provider.base_url or "").rstrip("/").lower()
        == "https://chatgpt.com/backend-api/codex"
    )
    if is_chatgpt_codex:
        if body.provider_type not in (None, "chatgpt"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Codex 登录提供商必须保持 ChatGPT 订阅类型；如需 API Key，请新建 OpenAI 提供商。",
            )
        if body.wire_api not in (None, "responses"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Codex 登录提供商只支持 Responses 调用线路。",
            )
        if (
            body.base_url is not None
            and body.base_url.rstrip("/").lower()
            != "https://chatgpt.com/backend-api/codex"
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Codex 登录提供商必须使用 ChatGPT Codex Responses 地址。",
            )
        if body.model_id:
            supported = available_chatgpt_codex_models()
            if body.model_id not in supported:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"模型 {body.model_id} 不在当前 Codex 登录可用列表中：{', '.join(supported)}",
                )
        # 顺带修复由旧版管理表单误改的类型和线路。
        provider.provider_type = "chatgpt"
        provider.wire_api = "responses"
    if body.name and body.name != provider.name:
        if db.query(ModelProvider).filter(ModelProvider.name == body.name).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "提供商名称已存在")
        provider.name = body.name
    if body.provider_type is not None:
        provider.provider_type = body.provider_type
    if body.base_url is not None:
        provider.base_url = body.base_url.rstrip("/")
    if body.api_key:  # 留空表示不修改
        provider.api_key = body.api_key
    if body.model_id is not None:
        provider.model_id = body.model_id
    if body.model_name is not None:
        provider.model_name = body.model_name
    if body.model_reasoning is not None:
        provider.model_reasoning = body.model_reasoning
        if not body.model_reasoning:
            provider.reasoning_effort = ""
    if body.model_input is not None:
        provider.model_input = json.dumps(_model_input(body.model_input))
    if body.context_window is not None:
        provider.context_window = body.context_window
    for field in (
        "wire_api", "auth_type", "auth_header", "api_version", "api_version_mode",
        "model_list_path", "reasoning_effort", "max_tokens", "timeout_ms",
        "max_tokens_param",
        "max_retries", "stream_max_retries", "stream_idle_timeout_ms",
        "supports_temperature",
    ):
        value = getattr(body, field)
        if value is not None:
            if field == "reasoning_effort" and not provider.model_reasoning:
                value = ""
            setattr(provider, field, value)
    if body.custom_headers is not None:
        existing_headers = _json_dict(getattr(provider, "custom_headers", "{}"))
        provider.custom_headers = json.dumps(
            _clean_headers(body.custom_headers, existing_headers), ensure_ascii=False
        )
    if body.extra_body is not None:
        provider.extra_body = json.dumps(_clean_extra_body(body.extra_body), ensure_ascii=False)
    if body.enabled is not None:
        provider.enabled = body.enabled
    if body.is_public is not None:
        provider.is_public = body.is_public
    db.commit()
    db.refresh(provider)
    return _to_out(provider, admin)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(
    provider_id: int, admin: User = Depends(require_module("providers")), db: Session = Depends(get_db)
):
    provider = db.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提供商不存在")
    require_owner(admin, provider)
    bound = db.query(Agent).filter(Agent.provider_id == provider_id).count()
    if bound:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"仍有 {bound} 个智能体绑定该提供商，请先解绑"
        )
    # Token 用量是历史审计数据，删除提供商时应保留。旧数据库的
    # 外键没有 ON DELETE SET NULL，因此在这里显式解除引用，避免提交时返回 500。
    db.query(TokenUsage).filter(TokenUsage.provider_id == provider_id).update(
        {TokenUsage.provider_id: None}, synchronize_session=False
    )
    db.delete(provider)
    db.commit()


@router.post("/{provider_id}/test")
async def test_provider(
    provider_id: int, admin: User = Depends(require_module("providers")), db: Session = Depends(get_db)
):
    provider = db.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提供商不存在")
    require_owner(admin, provider)
    try:
        reply = await client_for_provider(provider).ping()
    except Exception as exc:  # noqa: BLE001 - 把失败原因透传给前端
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"连接失败: {exc}")
    return {"ok": True, "model": provider.model_id, "reply": reply[:200]}


@router.get("/{provider_id}/models")
async def list_provider_models(
    provider_id: int, admin: User = Depends(require_module("providers")), db: Session = Depends(get_db)
):
    """识别该提供商可用的模型：OpenAI 兼容接口实时拉取 GET /models；
    chatgpt 通道及拉取失败时回退到内置预设列表。"""
    provider = db.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提供商不存在")
    require_owner(admin, provider)
    preset_models = (PROVIDER_PRESETS.get(provider.provider_type) or {}).get("models") or []
    # ChatGPT 订阅通道没有标准 /models 接口，直接给内置预设
    if (
        provider.provider_type == "chatgpt"
        or str(provider.base_url or "").rstrip("/").lower()
        == "https://chatgpt.com/backend-api/codex"
    ):
        return {"models": available_chatgpt_codex_models(), "source": "codex-cache"}
    try:
        models = await client_for_provider(provider).list_models()
        if models:
            return {"models": models, "source": "live"}
    except Exception as exc:  # noqa: BLE001
        if preset_models:
            return {"models": preset_models, "source": "preset", "note": f"实时获取失败，已回退预设：{exc}"[:200]}
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"获取模型列表失败: {exc}")
    return {"models": preset_models, "source": "preset"}


# ---------- Codex CLI 登录凭据导入 ----------

def _codex_auth_paths() -> list[Path]:
    home = Path.home()
    return [home / ".codex" / "auth.json"]


@router.post("/import-codex", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def import_codex(admin: User = Depends(require_module("providers")), db: Session = Depends(get_db)):
    """读取本机 Codex CLI 登录凭据（codex login 生成的 ~/.codex/auth.json），
    自动创建/更新 API Key 或 ChatGPT 订阅对应的 Codex 提供商。"""
    auth_file = next((p for p in _codex_auth_paths() if p.exists()), None)
    if auth_file is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "未找到 Codex 登录凭据（~/.codex/auth.json）。请先在本机执行 codex login，"
            "或在「新增提供商」中选择 OpenAI 类型手动填写 API Key。",
        )
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Codex 凭据文件解析失败")

    api_key = (data.get("OPENAI_API_KEY") or "").strip()
    tokens = data.get("tokens") or {}
    oauth_token = (tokens.get("access_token") or "").strip()

    if api_key:
        # 正式 API Key（codex login --api-key 或环境提供）→ 按量计费的 OpenAI 通道
        preset = PROVIDER_PRESETS["openai"]
        provider = _upsert_provider(db, admin, "Codex (OpenAI)", "openai", preset)
        provider.api_key = api_key
        provider.auth_extra = ""
    elif oauth_token:
        # ChatGPT 账号登录的 OAuth 令牌 → 走 Responses 通道（chatgpt 类型），用 ChatGPT 订阅额度
        from ..llm.chatgpt_client import account_id_from_tokens
        if not account_id_from_tokens(tokens):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "ChatGPT 登录凭据中缺少账号信息（account_id / id_token），请重新执行 codex login 后再导入。",
            )
        preset = PROVIDER_PRESETS["chatgpt"]
        preset = dict(preset)
        preset["model_id"] = preferred_chatgpt_codex_model()
        provider = _upsert_provider(db, admin, "ChatGPT 订阅 (Codex)", "chatgpt", preset)
        provider.api_key = oauth_token
        provider.auth_extra = json.dumps({
            "access_token": oauth_token,
            "refresh_token": tokens.get("refresh_token", ""),
            "id_token": tokens.get("id_token", ""),
            "account_id": tokens.get("account_id", ""),
        }, ensure_ascii=False)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Codex 凭据中未找到可用凭据，请重新 codex login")

    provider.enabled = True
    db.commit()
    db.refresh(provider)
    return _to_out(provider, admin)


def _upsert_provider(db: Session, admin: User, name: str, ptype: str, preset: dict) -> ModelProvider:
    provider = db.query(ModelProvider).filter(ModelProvider.name == name).first()
    if provider is None and ptype == "chatgpt":
        provider = next((
            item for item in db.query(ModelProvider).all()
            if str(item.base_url or "").rstrip("/").lower()
            == "https://chatgpt.com/backend-api/codex"
        ), None)
    if provider is None:
        provider = ModelProvider(
            name=name, provider_type=ptype,
            base_url=preset["base_url"], model_id=preset["model_id"],
            model_name=preset.get("model_name", ""),
            model_reasoning=bool(preset.get("model_reasoning", False)),
            model_input=json.dumps(_model_input(preset.get("model_input"))),
            created_by=admin.id,
        )
        db.add(provider)
    else:
        require_owner(admin, provider)
        # 复用旧版导入记录，避免中文名称损坏或变化时重复创建提供商。
        name_taken = db.query(ModelProvider).filter(
            ModelProvider.name == name,
            ModelProvider.id != provider.id,
        ).first()
        if name_taken is None:
            provider.name = name
        provider.provider_type = ptype
        provider.base_url = preset["base_url"]
    provider.model_id = preset.get("model_id", provider.model_id)
    provider.model_name = preset.get("model_name", provider.model_name)
    provider.model_reasoning = bool(preset.get("model_reasoning", False))
    provider.model_input = json.dumps(_model_input(preset.get("model_input")))
    provider.wire_api = preset.get("wire_api", "chat_completions")
    provider.auth_type = preset.get("auth_type", "bearer")
    provider.api_version = preset.get("api_version", "")
    provider.api_version_mode = preset.get("api_version_mode", "none")
    provider.model_list_path = preset.get("model_list_path", "/models")
    return provider
