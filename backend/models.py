"""ORM 模型：用户 / 智能体 / API密钥 / 对话记录。"""
import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .secret_store import EncryptedText

# 角色常量
ROLE_ROOT = "root"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_GUEST = "guest"
ROLES = (ROLE_ROOT, ROLE_ADMIN, ROLE_USER)

# 设置页模块目录：root 可逐个授予 admin / user 访问；root 始终拥有全部。
# 普通用户只可被授予 USER_MODULE_KEYS 中的通用模块，避免开放平台治理能力。
# 在此追加 (key, 标签) 即新增一个可授权模块（数据驱动，前端自动渲染）。
ADMIN_MODULES = [
    ("agents", "智能体"),
    ("improvement", "评估与改进"),
    ("knowledge", "知识"),
    ("tools", "工具总览（内置工具）"),
    ("mcp", "工具 · MCP"),
    ("services", "服务（网络与编程）"),
    ("memory", "记忆"),
    ("guardrails", "护栏"),
    ("providers", "模型"),
    ("skills", "工具 · 技能"),
    ("templates", "模板"),
    ("schedules", "定时任务"),
    ("archive", "归档"),
    ("token_usage", "Token 用量"),
    ("keys", "API 密钥"),
    ("channels", "消息渠道"),
]
ADMIN_MODULE_KEYS = {k for k, _ in ADMIN_MODULES}
USER_MODULE_KEYS = {
    "tools", "services", "memory", "guardrails",
    "knowledge",
    "mcp",
    "skills",
    "providers",
    "templates",
    "schedules",
    "archive",
    "token_usage",
    "channels",
}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso_utc(dt: "datetime.datetime | None") -> str:
    """把库内时间统一序列化为带时区（UTC）的 ISO 字符串，供前端正确换算本地时区显示。

    库内时间由 _now() 以 UTC 生成；但 SQLite 不保留时区，读回为 naive（数值仍是 UTC）。
    若直接 isoformat()，输出不带时区后缀，前端 new Date() 会按「本地时间」解析，导致显示时间
    偏差一个时区（如东八区早 8 小时）。这里对 naive 补 UTC tzinfo 再 isoformat，确保带 +00:00 后缀。
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.isoformat()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 会话/JWT 撤销版本；改密、禁用、角色或权限变更时递增，使既有凭据立即失效。
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    # 设置页模块授权：admin 空串 = 全部（向后兼容）；user 空串 = 无模块。
    # JSON 数组表示明确授权的模块；对 root 无意义（始终全部）。
    permissions: Mapped[str] = mapped_column(Text, default="")
    # Account-scoped UI/execution preferences; never grants module or tool access.
    preferences: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class AuthSession(Base):
    """Server-side browser sessions. Only a digest of the cookie is persisted."""

    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)


class ModelProvider(Base):
    """模型提供商：以协议而非厂商建模，支持 OpenAI / Anthropic 兼容接口。"""
    __tablename__ = "model_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider_type: Mapped[str] = mapped_column(String(32), default="openai")
    base_url: Mapped[str] = mapped_column(String(512), default="")
    api_key: Mapped[str] = mapped_column(EncryptedText(), default="")
    # OpenClaw 风格的通用模型元数据：一个提供商配置只选择一个运行模型，
    # 能力由 input 模态声明，不再为文本、视觉和图片生成维护互相冲突的模型槽位。
    model_id: Mapped[str] = mapped_column(String(256), default="")
    model_name: Mapped[str] = mapped_column(String(256), default="")
    model_reasoning: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    model_input: Mapped[str] = mapped_column(
        Text, default='["text"]', server_default='["text"]'
    )
    # 模型级上下文窗口和最大输出；0 表示使用服务端/全局默认值。
    context_window: Mapped[int] = mapped_column(Integer, default=0)
    # chatgpt 类型专用：JSON 存 OAuth 凭据 {access_token, refresh_token, account_id, id_token}，
    # 令牌过期时用 refresh_token 刷新并回写。其它类型留空。
    auth_extra: Mapped[str] = mapped_column(EncryptedText(), default="")
    # 通用协议配置。旧提供商未设置时按 OpenAI Chat Completions + Bearer 处理。
    wire_api: Mapped[str] = mapped_column(String(32), default="chat_completions")
    auth_type: Mapped[str] = mapped_column(String(16), default="bearer")
    auth_header: Mapped[str] = mapped_column(String(64), default="")
    api_version: Mapped[str] = mapped_column(String(64), default="")
    api_version_mode: Mapped[str] = mapped_column(String(16), default="none")
    custom_headers: Mapped[str] = mapped_column(EncryptedText(), default="{}")
    extra_body: Mapped[str] = mapped_column(Text, default="{}")
    model_list_path: Mapped[str] = mapped_column(String(128), default="/models")
    reasoning_effort: Mapped[str] = mapped_column(String(16), default="")
    reasoning_config: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    max_tokens: Mapped[int] = mapped_column(Integer, default=8192)
    max_tokens_param: Mapped[str] = mapped_column(String(32), default="auto")
    timeout_ms: Mapped[int] = mapped_column(Integer, default=120000)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    stream_max_retries: Mapped[int] = mapped_column(Integer, default=3)
    stream_idle_timeout_ms: Mapped[int] = mapped_column(Integer, default=300000)
    supports_temperature: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    opening_statement: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 绑定的模型提供商；NULL = 使用 .env 配置的默认本地模型
    provider_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("model_providers.id"), nullable=True
    )
    # 当前已发布的 Harness 版本号；具体指令与策略存于 immutable harness_versions。
    active_version: Mapped[int] = mapped_column(Integer, default=1)
    # 通用能力接口：挂载的 MCP 服务 / 技能 id 列表（JSON 数组）
    mcp_ids: Mapped[str] = mapped_column(Text, default="[]")
    skill_ids: Mapped[str] = mapped_column(Text, default="[]")
    # 可调用的其他智能体 id 列表（JSON 数组）：对话中以工具形式按需调用（子智能体）
    agent_ids: Mapped[str] = mapped_column(Text, default="[]")
    # root 分配的内置工具名称（JSON 数组）；运行时还会与全局启用状态取交集。
    builtin_tools: Mapped[str] = mapped_column(Text, default="[]")
    # 记忆模块开关：开启后回忆该用户的历史会话并注入上下文
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 模型路由：JSON。{"mode":"fixed"} 用 provider_id；
    #   {"mode":"rules","default_provider_id":N,"rules":[{"match":"keyword|length_gt","value":..,"provider_id":N}]}
    routing: Mapped[str] = mapped_column(Text, default="")
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    # 默认智能体：对话页默认载入它；全局至多一个，仅 root 可设置/配置/删除。
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class HarnessVersion(Base):
    """智能体行为的不可变版本；发布通过 Agent.active_version 指向版本号完成。"""

    __tablename__ = "harness_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_harness_agent_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    tool_policy: Mapped[str] = mapped_column(Text, default="{}")
    memory_policy: Mapped[str] = mapped_column(Text, default="{}")
    verification_policy: Mapped[str] = mapped_column(Text, default="{}")
    output_policy: Mapped[str] = mapped_column(Text, default="{}")
    change_summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class ImprovementProposal(Base):
    """由运行证据驱动的最小 Harness 修改提案；禁止直接改写生产配置。"""

    __tablename__ = "improvement_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    base_version: Mapped[int] = mapped_column(Integer)
    proposed_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("harness_versions.id"), nullable=True
    )
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    expected_metrics: Mapped[str] = mapped_column(Text, default="{}")
    evaluation: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class McpServer(Base):
    """MCP 服务（通用工具接口）：智能体可挂载，对话时按需调用其工具。

    transport：
      http  Streamable HTTP（现行 MCP 标准，推荐）—— 直接 POST JSON-RPC 到 url
      sse   HTTP+SSE（旧式两通道）—— url 为 SSE 端点
    headers：JSON 对象，附加鉴权头（如 {"Authorization": "Bearer ..."}）。
    """
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    transport: Mapped[str] = mapped_column(String(16), default="http")
    command: Mapped[str] = mapped_column(Text, default="", server_default="")
    args: Mapped[str] = mapped_column(EncryptedText(), default="[]")
    env: Mapped[str] = mapped_column(EncryptedText(), default="{}")
    cwd: Mapped[str] = mapped_column(Text, default="", server_default="")
    stdio_authorized: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    url: Mapped[str] = mapped_column(String(1024), default="")
    headers: Mapped[str] = mapped_column(EncryptedText(), default="{}")
    # auto：优先 annotations，再用保守语义判断；read_only：管理员确认该服务
    # 只提供查询能力，但显式 destructive/write annotations 和写入动词仍会覆盖。
    risk_policy: Mapped[str] = mapped_column(String(16), default="auto")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Skill(Base):
    """技能（Agent Skill）：可复用、可打包的能力。

    对齐 Anthropic Agent Skills 结构：
      name        技能名（SKILL.md frontmatter）
      description 何时使用（触发说明，始终对模型可见）
      instructions  详细操作说明（SKILL.md 正文，挂载后注入系统提示词）
      resources   附带资源文件 JSON：[{"name","content"}]，对话中由模型经
                  read_skill_resource 工具按需读取（渐进式披露）。
    可导出为 .zip（SKILL.md + resources/）或 JSON，并支持导入。
    """
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    resources: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Template(Base):
    """报告模板：可复用的 Word / Markdown / PPT / Excel 模板，占位符替换式渲染。

    源文件内用 {{占位符}} 标记可填充位；渲染时模型按上下文产出「占位符→取值」键值对，
    系统用 python-docx / openpyxl / python-pptx 把值填回原文件，保留原排版。
      kind          word | md | ppt | excel
      ext           源文件后缀（.docx / .md / .pptx / .xlsx）
      placeholders  上传时解析缓存的占位符名 JSON 数组（仅用于展示/编辑提示）
    源文件落盘于 data/templates/{id}{ext}（二进制），不入库。
    可在流程中以 template_render 步骤引用，或在对话中经 @ 选中后渲染为可下载文件。
    """
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="word")
    ext: Mapped[str] = mapped_column(String(16), default="")
    placeholders: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))  # 用于前端展示 sk-xxxx****
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Channel(Base):
    """第三方接入渠道：把一个对外接口/Webhook 绑定到某个特定智能体，供外部系统调用。

    type 决定对外协议：
    - generic：通用 JSON 接口。`POST /open/channel/<path_key>`（Bearer token 鉴权）传 {"query": ...}
      调用绑定智能体；同步返回答案或返回 job_id 轮询。适配任意第三方系统/自有前端/对话平台 webhook。
    - wechat_mp：微信公众号。以 path_key 路由的免登录 Webhook（/open/wechat/<path_key>），
      GET 验签回显 echostr，POST 收消息→调用绑定智能体→回复（明文/兼容模式）。

    字段：
    - token：generic 渠道的调用令牌（Bearer）；wechat_mp 渠道的「服务器配置」Token（消息签名）。
    - app_id/app_secret：仅 wechat_mp。配置后启用「客服消息」异步推送，应对超 5s 窗口的长回答。
    """
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(24), default="generic")
    # 绑定的「特定智能体」：该渠道的所有消息都路由到它
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), nullable=True)
    # URL 路由片段（同时充当渠道密钥，随机生成、唯一）
    path_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 公众号服务器配置 Token（消息签名用）
    token: Mapped[str] = mapped_column(EncryptedText(), default="")
    # 可选：公众号 AppID / AppSecret（启用客服消息异步推送）
    app_id: Mapped[str] = mapped_column(String(64), default="")
    app_secret: Mapped[str] = mapped_column(EncryptedText(), default="")
    # 腾讯微信 iLink 个人消息渠道。token 复用上面的加密字段保存 bot token；
    # 下列字段仅保存路由身份、同步游标和不含凭据的运行状态。
    account_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    account_user_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    base_url: Mapped[str] = mapped_column(String(512), default="")
    sync_buf: Mapped[str] = mapped_column(EncryptedText(), default="")
    connection_status: Mapped[str] = mapped_column(String(24), default="unbound", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_inbound_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_outbound_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class AppSetting(Base):
    """全局系统设置（键值）：如 site_name（系统名称）、logo_ext（Logo 文件后缀）。

    仅 root 可写；公开读端点只暴露品牌相关项（站名 / Logo URL）。
    Logo 文件落盘于 data/branding/logo<ext>，此处仅存后缀以定 MIME。
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class ResourceVersion(Base):
    """Skill/MCP 的追加式、凭证无关生命周期快照。"""

    __tablename__ = "resource_versions"
    __table_args__ = (
        UniqueConstraint(
            "resource_type", "resource_id", "version",
            name="uq_resource_version_number",
        ),
        Index("ix_resource_versions_resource", "resource_type", "resource_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(16))
    resource_id: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    change: Mapped[str] = mapped_column(String(64), default="updated")
    # 不建立用户外键：账号删除后仍保留当时的治理审计归属编号。
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    snapshot: Mapped[str] = mapped_column(Text, default="{}")


class ResourceGovernanceState(Base):
    """每个资源一行的可变复核状态；复合主键防止并发丢失整张状态图。"""

    __tablename__ = "resource_governance_states"

    resource_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    resource_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_hash: Mapped[str] = mapped_column(String(64), default="")
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    tool_count: Mapped[int] = mapped_column(Integer, default=0)
    acknowledged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acknowledged_catalog_hash: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AuditLog(Base):
    """操作日志：记录变更类管理请求（POST/PUT/PATCH/DELETE）与登录，供 root 审计。

    轻量记录，不存请求体；用户身份由中间件 best-effort 解析 JWT 得到，匿名时留空。
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="")
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(String(512), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)


class TokenUsage(Base):
    """一次上游模型请求的 Token 用量。

    每条记录对应一次实际模型响应；用户、任务、智能体和提供商维度都保留下来，
    便于系统管理员按用户审计消耗，也能在模型线路变更后保留历史口径。
    """

    __tablename__ = "token_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 用户删除后仍保留完整 usage 审计账本：user_id 主动置空，username 保存采集时快照。
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(64), default="")
    run_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    agent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agents.id"), nullable=True, index=True
    )
    provider_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("model_providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)


class UserTokenLimit(Base):
    """用户 Token 限额与手动周期重置基线；0 表示对应周期不限额。"""

    __tablename__ = "user_token_limits"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    weekly_limit: Mapped[int] = mapped_column(Integer, default=0)
    monthly_limit: Mapped[int] = mapped_column(Integer, default=0)
    total_limit: Mapped[int] = mapped_column(Integer, default=0)
    weekly_reset_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    monthly_reset_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now
    )


class Project(Base):
    """长期工作空间容器；一个 Project 可以承载多条独立 Thread。"""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_project_user_name"),
        # SQLite 与 PostgreSQL 均支持部分唯一索引：每个用户允许零个或一个默认项目。
        Index(
            "uq_projects_one_default_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("is_default = 1"),
            postgresql_where=text("is_default IS TRUE"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    # 项目说明来自用户输入，只能作为 user 层参考上下文，不能提升为系统指令。
    context_text: Mapped[str] = mapped_column(Text, default="")
    default_agent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # JSON 字符串数组；保存项目默认引用的知识库 key，实际检索仍逐次执行 ACL 交集。
    dataset_ids: Mapped[str] = mapped_column(Text, default="[]")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Thread(Base):
    """一条持续任务工作线。

    Thread 只承载任务级上下文和用户侧组织信息；每次用户交互由 Turn 表示，
    具体消息、工具调用、审批与验证记录统一追加到 Item。
    """

    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # memory_enabled 控制当前 Thread 是否接收长期记忆；memory_excluded 控制该
    # Thread 是否还能作为其他会话的记忆来源。后者是可逆墓碑，不删除 Turn/Item。
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    memory_excluded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    memory_excluded_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    context_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, index=True
    )


class Turn(Base):
    """一次用户输入到控制权返回的完整执行周期。"""

    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("thread_id", "sequence", name="uq_turn_thread_sequence"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    continuation_of_turn_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    agent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    source: Mapped[str] = mapped_column(String(16), default="web")
    input: Mapped[str] = mapped_column(Text, default="")
    final_output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    execution_snapshot: Mapped[str] = mapped_column(EncryptedText(), default="{}")
    item_sequence: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now
    )


class Attachment(Base):
    """Thread 内可持续引用的用户附件，而不是一次 HTTP 请求的临时路径。

    storage_name 仅保存 UPLOAD_DIR 下的随机文件名；原始文件名与抽取正文分别作为
    展示元数据和加密任务资料保存。来源 Turn 删除后记录仍随 Thread 保留，物理文件
    仅由统一清理器在确认没有持久引用后回收。
    """

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    storage_name: Mapped[str] = mapped_column(String(255), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    kind: Mapped[str] = mapped_column(String(16), default="document", index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    extracted_text: Mapped[str] = mapped_column(EncryptedText(), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)


class Item(Base):
    """Turn 内的追加式最小事件：消息、工具、审批、计划、验证或状态变化。"""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("turn_id", "sequence", name="uq_item_turn_sequence"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("turns.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(16), default="")
    name: Mapped[str] = mapped_column(String(96), default="")
    status: Mapped[str] = mapped_column(String(24), default="completed")
    content: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)


class ToolApproval(Base):
    """用户对单个 Turn、单个有副作用工具签发的一次性批准。"""

    __tablename__ = "tool_approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), nullable=True)
    scope: Mapped[str] = mapped_column(String(64))
    invocation_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    arguments_digest: Mapped[str] = mapped_column(String(64), default="", server_default="")
    capability_revision: Mapped[str] = mapped_column(String(128), default="", server_default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Artifact(Base):
    """一次运行生成的可下载产物及其归属、保留期。"""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "filename", name="uq_artifact_run_file"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=True, index=True
    )
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), index=True)
    media_type: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Job(Base):
    """Turn 的持久化传输队列，由 worker（进程内或独立进程）领取执行。

    跨重启不丢：进程崩溃后，心跳超时的 running 任务会被重新入队（见 jobs.requeue_stale）。
    取消为协作式：置 cancel_requested，运行中的任务在阶段检查点（progress 回调）感知并中止，
    适配独立 worker 进程（无法直接 task.cancel 的跨进程场景）。业务状态和审计历史
    位于 Thread / Turn / Item，本表只负责调度、租约、重试和取消。
    """
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_job_owner_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # uuid4 hex
    owner_id: Mapped[int] = mapped_column(Integer, index=True, nullable=True)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=True)
    # 异步子智能体的可索引父任务关联；顶层任务为空。业务事件仍保存在 Item。
    parent_job_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="chat")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    payload: Mapped[str] = mapped_column(EncryptedText(), default="{}")   # JSON：执行入参与 Harness 版本快照
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    progress: Mapped[str] = mapped_column(String(256), default="")
    partial_result: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str] = mapped_column(Text, default="")      # JSON：成功结果
    error: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str] = mapped_column(String(64), default="")  # 领取该任务的 worker 标识
    lease_token: Mapped[str] = mapped_column(String(32), default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    # 可分类重试的最早再次领取时间；NULL 表示立即可领取。
    next_attempt_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    # 调度优先级，数值越大越优先；同优先级仍按用户公平性和创建时间排序。
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    error_class: Mapped[str] = mapped_column(String(32), default="")
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)  # 续租心跳
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class RuntimeCheckpoint(Base):
    """Private executable state; never part of the public Item/event projection."""

    __tablename__ = "runtime_checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "execution_key", name="uq_runtime_checkpoint_execution"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    execution_key: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(EncryptedText(), default="{}")
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ToolInvocation(Base):
    """Write-ahead execution ledger with encrypted arguments and observations."""

    __tablename__ = "tool_invocations"
    __table_args__ = (UniqueConstraint("run_id", "execution_key", "call_id", name="uq_tool_invocation_call"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    execution_key: Mapped[str] = mapped_column(String(160))
    call_id: Mapped[str] = mapped_column(String(160))
    tool_name: Mapped[str] = mapped_column(String(160))
    arguments_digest: Mapped[str] = mapped_column(String(64))
    capability_revision: Mapped[str] = mapped_column(String(128))
    effect: Mapped[str] = mapped_column(String(32), default="unknown")
    state: Mapped[str] = mapped_column(String(32), default="prepared", index=True)
    arguments: Mapped[str] = mapped_column(EncryptedText(), default="{}")
    result: Mapped[str] = mapped_column(EncryptedText(), default="{}")
    lease_token: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class JobGuidance(Base):
    """用户在任务运行期间追加的引导消息。

    引导与普通排队消息分开持久化：worker 只在模型调用之间的安全检查点领取，
    因而不会篡改已经固化的任务快照，也能跨 API/worker 进程传递。
    """

    __tablename__ = "job_guidance"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)
    applied_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)


class WorkerHeartbeat(Base):
    """跨进程 Worker 存活证据，供健康检查和容量观测。"""

    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)


class SchedulerHeartbeat(Base):
    """Cron 调度器存活与最近派发结果，供健康检查判断调度面是否可用。"""

    __tablename__ = "scheduler_heartbeats"

    scheduler_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)
    last_successful_dispatch_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_error: Mapped[str] = mapped_column(Text, default="")


class RateLimitBucket(Base):
    """跨 API 副本共享的固定窗口限流计数。键仅保存 SHA-256。"""

    __tablename__ = "rate_limit_buckets"

    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)


class ChannelDispatch(Base):
    """微信公众号重试去重与客服消息推送领取状态。"""

    __tablename__ = "channel_dispatches"

    dedupe_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[int] = mapped_column(Integer, ForeignKey("channels.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id"), index=True)
    pushing: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class ScheduledTask(Base):
    """持久化 Cron 任务。

    调度器只负责在到期时创建普通 ``chat`` Job，实际执行、取消、重试和事件记录继续
    复用统一 Worker，避免出现第二套隐式执行路径。
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    # 固定追加到创建提醒的对话线程，到期执行时不再生成新的会话窗口。
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    cron: Mapped[str] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    query: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    next_run_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    last_run_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    last_job_id: Mapped[str] = mapped_column(String(32), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_dispatch_status: Mapped[str] = mapped_column(String(24), default="scheduled")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
