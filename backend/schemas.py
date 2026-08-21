"""Pydantic 请求/响应模型。"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---- 认证 ----
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    all_modules: bool = False
    modules: list[str] = []


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    context_text: str = Field(default="", max_length=8000)
    default_agent_id: Optional[int] = None
    dataset_ids: list[str] = Field(default_factory=list, max_length=20)
    default: bool = False


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    context_text: Optional[str] = Field(default=None, max_length=8000)
    default_agent_id: Optional[int] = None
    dataset_ids: Optional[list[str]] = Field(default=None, max_length=20)
    default: Optional[bool] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None


class ThreadMemoryUpdate(BaseModel):
    enabled: Optional[bool] = None
    source_excluded: Optional[bool] = None

    @field_validator("enabled", "source_excluded", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("记忆设置不能为 null")
        return value


class ThreadUpdate(BaseModel):
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    project_id: Optional[int] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=80)


# ---- 用户（root 管理） ----
class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = Field(default="user", pattern="^(root|admin|user)$")
    # 设置页模块授权：admin 可选全部模块，user 仅可选通用模块。
    all_modules: Optional[bool] = None
    modules: Optional[list[str]] = None


class UserUpdate(BaseModel):
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)
    role: Optional[str] = Field(default=None, pattern="^(root|admin|user)$")
    is_active: Optional[bool] = None
    all_modules: Optional[bool] = None     # True = 授予全部模块
    modules: Optional[list[str]] = None    # 指定可访问模块（all_modules 为真时忽略）


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    all_modules: bool = False      # 是否拥有当前角色可授予的全部模块
    modules: list[str] = []        # 当非全部时，具体可访问模块


class UserTokenLimitsUpdate(BaseModel):
    """用户 Token 限额；0 表示不限额。"""

    weekly_limit: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    monthly_limit: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    total_limit: int = Field(default=0, ge=0, le=9_000_000_000_000_000)


# ---- 模型提供商（root/admin 管理） ----
class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    provider_type: str = Field(
        default="openai", pattern="^(openai|anthropic|chatgpt|deepseek|qwen|nvidia|custom)$"
    )
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = ""
    model_id: str = Field(min_length=1, max_length=256)
    model_name: str = Field(default="", max_length=256)
    model_reasoning: bool = False
    model_input: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"], min_length=1, max_length=2
    )
    context_window: int = Field(default=0, ge=0, le=2_000_000)
    wire_api: str = Field(default="chat_completions", pattern="^(chat_completions|responses|messages)$")
    auth_type: str = Field(default="bearer", pattern="^(bearer|api_key|x_api_key|custom|none)$")
    auth_header: str = Field(default="", max_length=64)
    api_version: str = Field(default="", max_length=64)
    api_version_mode: str = Field(default="none", pattern="^(none|header|query)$")
    custom_headers: dict = Field(default_factory=dict)
    extra_body: dict = Field(default_factory=dict)
    model_list_path: str = Field(default="/models", max_length=128)
    reasoning_effort: str = Field(default="", pattern="^(|minimal|low|medium|high|xhigh|max)$")
    max_tokens: int = Field(default=8192, ge=1, le=1_000_000)
    max_tokens_param: str = Field(
        default="auto",
        pattern="^(auto|max_tokens|max_completion_tokens|max_output_tokens|none)$",
    )
    timeout_ms: int = Field(default=120000, ge=1000, le=3_600_000)
    max_retries: int = Field(default=3, ge=0, le=20)
    stream_max_retries: int = Field(default=3, ge=0, le=20)
    stream_idle_timeout_ms: int = Field(default=300000, ge=1000, le=3_600_000)
    supports_temperature: bool = True
    enabled: bool = True
    is_public: bool = False

    @field_validator("model_input")
    @classmethod
    def validate_model_input(cls, value: list[str]) -> list[str]:
        if "text" not in value:
            raise ValueError("模型输入模态必须包含 text")
        if len(set(value)) != len(value):
            raise ValueError("模型输入模态不能重复")
        return [item for item in ("text", "image") if item in value]


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    provider_type: Optional[str] = Field(default=None, pattern="^(openai|anthropic|chatgpt|deepseek|qwen|nvidia|custom)$")
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    model_name: Optional[str] = Field(default=None, max_length=256)
    model_reasoning: Optional[bool] = None
    model_input: Optional[list[Literal["text", "image"]]] = Field(
        default=None, min_length=1, max_length=2
    )
    context_window: Optional[int] = Field(default=None, ge=0, le=2_000_000)
    wire_api: Optional[str] = Field(default=None, pattern="^(chat_completions|responses|messages)$")
    auth_type: Optional[str] = Field(default=None, pattern="^(bearer|api_key|x_api_key|custom|none)$")
    auth_header: Optional[str] = Field(default=None, max_length=64)
    api_version: Optional[str] = Field(default=None, max_length=64)
    api_version_mode: Optional[str] = Field(default=None, pattern="^(none|header|query)$")
    custom_headers: Optional[dict] = None
    extra_body: Optional[dict] = None
    model_list_path: Optional[str] = Field(default=None, max_length=128)
    reasoning_effort: Optional[str] = Field(default=None, pattern="^(|minimal|low|medium|high|xhigh|max)$")
    max_tokens: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    max_tokens_param: Optional[str] = Field(
        default=None,
        pattern="^(auto|max_tokens|max_completion_tokens|max_output_tokens|none)$",
    )
    timeout_ms: Optional[int] = Field(default=None, ge=1000, le=3_600_000)
    max_retries: Optional[int] = Field(default=None, ge=0, le=20)
    stream_max_retries: Optional[int] = Field(default=None, ge=0, le=20)
    stream_idle_timeout_ms: Optional[int] = Field(default=None, ge=1000, le=3_600_000)
    supports_temperature: Optional[bool] = None
    enabled: Optional[bool] = None
    is_public: Optional[bool] = None

    @field_validator("model_input")
    @classmethod
    def validate_model_input(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if "text" not in value:
            raise ValueError("模型输入模态必须包含 text")
        if len(set(value)) != len(value):
            raise ValueError("模型输入模态不能重复")
        return [item for item in ("text", "image") if item in value]


class ProviderProbe(ProviderCreate):
    name: str = "临时连接"
    model_id: str = Field(default="", max_length=256)


class ProviderOut(BaseModel):
    """api_key 不回传明文，仅指示是否已配置。"""
    id: int
    name: str
    provider_type: str
    base_url: str
    model_id: str
    model_name: str = ""
    model_reasoning: bool = False
    model_input: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text"])
    context_window: int = 0
    wire_api: str = "chat_completions"
    auth_type: str = "bearer"
    auth_header: str = ""
    api_version: str = ""
    api_version_mode: str = "none"
    custom_headers: dict = Field(default_factory=dict)
    extra_body: dict = Field(default_factory=dict)
    model_list_path: str = "/models"
    reasoning_effort: str = ""
    max_tokens: int = 8192
    max_tokens_param: str = "auto"
    timeout_ms: int = 120000
    max_retries: int = 3
    stream_max_retries: int = 3
    stream_idle_timeout_ms: int = 300000
    supports_temperature: bool = True
    enabled: bool
    has_key: bool = False
    is_public: bool = False
    can_manage: bool = True

    class Config:
        from_attributes = True


# ---- 智能体（root/admin 管理） ----
class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    system_prompt: str = ""
    opening_statement: str = ""
    enabled: bool = True
    provider_id: Optional[int] = None
    mcp_ids: Optional[list[int]] = None     # 挂载的 MCP 服务
    skill_ids: Optional[list[int]] = None   # 挂载的技能
    agent_ids: Optional[list[int]] = None   # 可调用的其他智能体
    builtin_tools: Optional[list[str]] = None  # 仅 root 可分配的内置工具
    memory_enabled: bool = False            # 记忆模块（回忆历史会话）
    routing: Optional[dict] = None          # 模型路由配置
    is_public: bool = False


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    opening_statement: Optional[str] = None
    enabled: Optional[bool] = None
    provider_id: Optional[int] = Field(default=None)
    clear_provider: bool = False  # 置 True 时恢复使用默认本地模型
    mcp_ids: Optional[list[int]] = None
    skill_ids: Optional[list[int]] = None
    agent_ids: Optional[list[int]] = None
    builtin_tools: Optional[list[str]] = None
    memory_enabled: Optional[bool] = None
    routing: Optional[dict] = None
    is_public: Optional[bool] = None
    is_default: Optional[bool] = None  # 仅 root 可设置；置 True 会把其它智能体的默认标记清除


class AgentOut(BaseModel):
    id: int
    name: str
    description: str
    system_prompt: str
    opening_statement: str
    enabled: bool
    provider_id: Optional[int] = None
    active_version: int = 1
    mcp_ids: list[int] = []
    skill_ids: list[int] = []
    agent_ids: list[int] = []
    builtin_tools: list[str] = []
    memory_enabled: bool = False
    routing: dict = {}
    is_public: bool = False
    is_default: bool = False
    can_manage: bool = True


# ---- MCP 服务（root/admin 管理） ----
class McpServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    transport: str = Field(default="http", pattern="^(http|sse)$")
    url: str = Field(min_length=1, max_length=1024)
    headers: dict = Field(default_factory=dict)
    risk_policy: str = Field(default="auto", pattern="^(auto|read_only)$")
    enabled: bool = True


class McpServerUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    transport: Optional[str] = Field(default=None, pattern="^(http|sse)$")
    url: Optional[str] = None
    headers: Optional[dict] = None
    risk_policy: Optional[str] = Field(default=None, pattern="^(auto|read_only)$")
    enabled: Optional[bool] = None
    is_public: Optional[bool] = None


class McpServerOut(BaseModel):
    id: int
    name: str
    description: str
    transport: str
    url: str
    headers: dict = {}
    risk_policy: str = "auto"
    enabled: bool
    is_public: bool = False
    can_manage: bool = True
    version: int = 0
    content_hash: str = ""
    catalog_hash: str = ""
    review_required: bool = False


# ---- 技能（root/admin 管理） ----
class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    instructions: str = Field(min_length=1)
    resources: Optional[list[dict]] = None  # [{"name","content"}]
    enabled: bool = True


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    resources: Optional[list[dict]] = None
    enabled: Optional[bool] = None
    is_public: Optional[bool] = None


class SkillOut(BaseModel):
    id: int
    name: str
    description: str
    instructions: str
    resources: list[dict] = []
    enabled: bool
    is_public: bool = False
    can_manage: bool = True
    version: int = 0
    content_hash: str = ""


# ---- 模板（root/admin 管理） ----
class TemplateOut(BaseModel):
    id: int
    name: str
    description: str
    kind: str
    ext: str = ""
    placeholders: list[str] = []
    has_file: bool = False
    enabled: bool
    is_public: bool = False
    can_manage: bool = True


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    is_public: Optional[bool] = None


class TemplateRenderBody(BaseModel):
    values: dict = {}


class HarnessVersionCreate(BaseModel):
    system_prompt: str = ""
    tool_policy: dict = Field(default_factory=dict)
    memory_policy: dict = Field(default_factory=dict)
    verification_policy: dict = Field(default_factory=dict)
    output_policy: dict = Field(default_factory=dict)
    change_summary: str = ""
    publish: bool = False


class HarnessVersionOut(BaseModel):
    id: int
    agent_id: int
    version: int
    system_prompt: str
    tool_policy: dict = Field(default_factory=dict)
    memory_policy: dict = Field(default_factory=dict)
    verification_policy: dict = Field(default_factory=dict)
    output_policy: dict = Field(default_factory=dict)
    loop: dict = Field(default_factory=dict)
    change_summary: str = ""
    status: str
    created_at: str = ""


# ---- API Key ----
class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    is_active: bool
    can_manage: bool = True

    class Config:
        from_attributes = True


# ---- 消息渠道（个人微信扫码；兼容历史 Webhook / 公众号记录） ----
class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    agent_id: int
    type: str = Field(default="openclaw_weixin", max_length=24)
    app_id: str = Field(default="", max_length=64)
    app_secret: str = Field(default="", max_length=128)


class ChannelUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    agent_id: Optional[int] = None
    enabled: Optional[bool] = None
    app_id: Optional[str] = Field(default=None, max_length=64)
    app_secret: Optional[str] = Field(default=None, max_length=128)
    regenerate_token: bool = False


class ChannelOut(BaseModel):
    id: int
    name: str
    type: str
    agent_id: Optional[int] = None
    agent_name: str = ""
    path_key: str
    token: str
    webhook_path: str
    webhook_url: str = ""
    app_id: str = ""
    has_app_secret: bool = False
    enabled: bool
    can_manage: bool = True
    account_id: str = ""
    connection_status: str = "unbound"
    last_error: str = ""
    last_inbound_at: str = ""
    last_outbound_at: str = ""
    workspace_key: str = ""


class WeixinPairCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=12, pattern=r"^[0-9]+$")


# ---- 对话 ----
class ChatResponse(BaseModel):
    answer: str
    export_files: list[str] = []
    turn_id: str
    thread_id: str


class OpenChatResponse(BaseModel):
    turn_id: str
    status: str
    task_status: str = ""
    completion_status: str = ""
    completion_issues: list[str] = []
    plan_summary: dict = {}
    answer: str = ""
    export_files: list[str] = []
    thread_id: Optional[str] = None


class OpenChatRequest(BaseModel):
    """开放 API 纯文本调用（文件上传请使用 multipart 端点）。"""
    agent_id: Optional[int] = None
    query: str = ""
    project_name: str = ""
    city_name: str = ""
    project_address: str = ""
    project_info: str = ""
    industry_structure: str = ""
    electricity_trading: str = ""
    image_scale: str = ""
