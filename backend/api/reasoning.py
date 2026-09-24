"""Authenticated model reasoning capability preview."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from ..models import User
from ..reasoning_options import normalize_reasoning_config, reasoning_capabilities, validate_reasoning_settings
from ..security import get_current_user

router = APIRouter(prefix="/api/v1", tags=["模型推理配置"])

class ReasoningCapabilitiesPreview(BaseModel):
    """Capability inspection is local and accepts no endpoint or credentials."""
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(default="", max_length=256)
    provider_type: Literal["openai", "anthropic", "chatgpt", "deepseek", "qwen", "nvidia", "custom"] = "openai"
    wire_api: Literal["chat_completions", "responses", "messages"] = "chat_completions"
    model_reasoning: bool = False
    reasoning_effort: str = Field(default="", pattern="^(|none|minimal|low|medium|high|xhigh|max|disabled|enabled)$")
    reasoning_config: dict = Field(default_factory=dict)
    max_tokens: int = Field(default=8192, ge=1, le=1_000_000)


@router.post("/reasoning-capabilities")
def preview_reasoning_capabilities(body: ReasoningCapabilitiesPreview, _: User = Depends(get_current_user)):
    provider = body.model_dump()
    try:
        provider["reasoning_config"] = normalize_reasoning_config(provider["reasoning_config"])
        validate_reasoning_settings(provider)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return reasoning_capabilities(provider)


