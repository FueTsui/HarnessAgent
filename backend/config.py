"""全局配置：通过环境变量或 .env 文件覆盖默认值。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# 数据根目录可经 APP_DATA_DIR 覆盖（默认 <repo>/data）。测试/多实例部署据此隔离落盘数据
# （Logo、导出件、知识库、SQLite 库等），避免冒烟测试等进程污染或清除生产数据目录。
DATA_DIR = Path(os.getenv("APP_DATA_DIR") or (BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
TEMPLATES_DIR = DATA_DIR / "templates"
SKILLS_DIR = DATA_DIR / "skills"
BRANDING_DIR = DATA_DIR / "branding"
FRONTEND_DIR = BASE_DIR / "frontend"
# 内置编码工具只允许访问此根目录。默认必须与应用源码、.env 和数据库分离；
# 后续运行时还会在此根目录下为每个用户/Turn 建立独立目录。
WORKSPACE_DIR = Path(
    os.getenv("AGENT_WORKSPACE_ROOT") or (DATA_DIR / "workspaces")
).resolve()


# 不安全默认值（启动时检测，非调试模式拒绝启动）
DEFAULT_JWT_SECRET = "change-me-in-production"
DEFAULT_ROOT_PASSWORD = "Root@123456"
# 已知占位符也视为不安全（含 .env.example 中的样例）
INSECURE_JWT_SECRETS = {
    "", DEFAULT_JWT_SECRET, "please-change-this-to-a-long-random-string",
}


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


class Settings:
    # 服务
    APP_ENV: str = os.getenv("APP_ENV", "development").strip().lower()
    HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("APP_PORT", "8000"))
    # 跨域默认关闭（同源前后端无需 CORS）；需要时填写逗号分隔的精确 Origin。
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "")
    TRUST_PROXY_HEADERS: bool = _env_bool("TRUST_PROXY_HEADERS", False)

    # 数据库
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"
    )

    # JWT
    JWT_SECRET: str = os.getenv("JWT_SECRET", DEFAULT_JWT_SECRET)
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))
    AUTH_COOKIE_NAME: str = os.getenv("AUTH_COOKIE_NAME", "gca_session")
    AUTH_COOKIE_SECURE: bool = _env_bool("AUTH_COOKIE_SECURE", False)
    # 数据库凭据信封加密主密钥。生产建议与 JWT_SECRET 分离。
    SECRET_MASTER_KEY: str = os.getenv("SECRET_MASTER_KEY", "")
    SECRET_MASTER_KEY_PREVIOUS: str = os.getenv("SECRET_MASTER_KEY_PREVIOUS", "")

    # 初始 root 账号（首次启动自动创建）
    ROOT_USERNAME: str = os.getenv("ROOT_USERNAME", "root")
    ROOT_PASSWORD: str = os.getenv("ROOT_PASSWORD", DEFAULT_ROOT_PASSWORD)

    # 安全开关
    # 开发环境放行不安全默认值（默认密钥/口令）。生产务必为 false（默认）。
    ALLOW_INSECURE_DEFAULTS: bool = _env_bool("ALLOW_INSECURE_DEFAULTS", False)
    # SSRF：默认拒绝外联到环回/内网/链路本地网段（MCP / 模型提供商 URL）。
    # 可信内网部署可设为 true，或用 SSRF_ALLOWLIST 精确放行（逗号分隔 host 或 CIDR）。
    SSRF_ALLOW_PRIVATE: bool = _env_bool("SSRF_ALLOW_PRIVATE", False)
    SSRF_ALLOWLIST: str = os.getenv("SSRF_ALLOWLIST", "")

    # Harness 内置工具。所有文件路径仍会被 WORKSPACE_DIR 强制约束；Shell 还会经过
    # 命令白名单、元字符检查、环境变量清理、超时和输出限额。
    BUILTIN_TOOLS_ENABLED: bool = _env_bool("BUILTIN_TOOLS_ENABLED", True)
    # Shell 默认关闭。只有部署方提供一个真正以低权限身份/容器执行命令的包装器时才可开启；
    # 仅靠命令白名单不构成 OS 沙箱。
    SHELL_TOOL_ENABLED: bool = _env_bool("SHELL_TOOL_ENABLED", False)
    SHELL_SANDBOX_COMMAND: str = os.getenv("SHELL_SANDBOX_COMMAND", "").strip()
    SHELL_TIMEOUT_SECONDS: int = int(os.getenv("SHELL_TIMEOUT_SECONDS", "60"))
    SHELL_OUTPUT_CHARS: int = int(os.getenv("SHELL_OUTPUT_CHARS", "20000"))
    BROWSER_TIMEOUT_SECONDS: int = int(os.getenv("BROWSER_TIMEOUT_SECONDS", "30"))
    # 免费联网检索：优先使用自托管 SearXNG；未配置或不可用时降级为
    # DuckDuckGo + Bing 的低频聚合检索，不需要 API Token。
    SEARXNG_URL: str = os.getenv("SEARXNG_URL", "").rstrip("/")
    WEB_SEARCH_FALLBACK: bool = _env_bool("WEB_SEARCH_FALLBACK", True)
    WEB_SEARCH_TIMEOUT_SECONDS: int = int(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "15"))
    WEB_REQUEST_MAX_RETRIES: int = int(os.getenv("WEB_REQUEST_MAX_RETRIES", "2"))
    WEB_FETCH_MAX_BYTES: int = int(os.getenv("WEB_FETCH_MAX_BYTES", "2097152"))
    WEB_FETCH_MAX_CHARS: int = int(os.getenv("WEB_FETCH_MAX_CHARS", "20000"))
    # OpenAI Images 兼容端点；留空时 image_generate 返回可操作的配置错误。
    IMAGE_API_BASE_URL: str = os.getenv("IMAGE_API_BASE_URL", "")
    IMAGE_API_KEY: str = os.getenv("IMAGE_API_KEY", "")
    IMAGE_MODEL: str = os.getenv("IMAGE_MODEL", "")
    # 可选图片编辑端点。必须显式配置，避免把只支持文生图的 IMAGE_* 端点
    # 误当成图片编辑服务；失败时 Editorial Skill 安全回退到本地 v3 渲染。
    IMAGE_EDIT_API_BASE_URL: str = os.getenv("IMAGE_EDIT_API_BASE_URL", "")
    IMAGE_EDIT_API_KEY: str = os.getenv("IMAGE_EDIT_API_KEY", "")
    IMAGE_EDIT_MODEL: str = os.getenv("IMAGE_EDIT_MODEL", "")
    IMAGE_OUTPUT_MAX_MB: int = int(os.getenv("IMAGE_OUTPUT_MAX_MB", "25"))
    IMAGE_MAX_PIXELS: int = int(os.getenv("IMAGE_MAX_PIXELS", "80000000"))

    # 本地 LLM（OpenAI 兼容接口：Ollama / vLLM / LM Studio / Xinference 均可）
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "ollama")
    LLM_TEXT_MODEL: str = os.getenv("LLM_TEXT_MODEL", "qwen2.5:14b")
    LLM_VISION_MODEL: str = os.getenv("LLM_VISION_MODEL", "qwen2.5vl:7b")
    LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "300"))
    LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
    # 多轮对话上下文预算（token 估算）：默认值，可被提供商的 context_window 覆盖（=模型上下文窗口）。
    # 对话历史 + 系统提示 + 当前问题的估算超过预算的约 70% 时，自动把较早的历史压缩成摘要。
    LLM_CONTEXT_TOKENS: int = int(os.getenv("LLM_CONTEXT_TOKENS", "8192"))

    # 上传限制（与原 Dify 应用一致）
    MAX_FILE_MB: int = int(os.getenv("MAX_FILE_MB", "15"))
    MAX_FILES_PER_FIELD: int = 5
    UPLOAD_TTL_SECONDS: int = int(os.getenv("UPLOAD_TTL_SECONDS", "86400"))

    # 数据库共享的应用级固定窗口限流（0 表示关闭）；网关仍可作为第一层保护。
    LOGIN_RATE_LIMIT: int = int(os.getenv("LOGIN_RATE_LIMIT", "10"))
    LOGIN_RATE_WINDOW_SECONDS: int = int(os.getenv("LOGIN_RATE_WINDOW_SECONDS", "300"))
    OPEN_API_RATE_LIMIT: int = int(os.getenv("OPEN_API_RATE_LIMIT", "60"))
    OPEN_API_RATE_WINDOW_SECONDS: int = int(os.getenv("OPEN_API_RATE_WINDOW_SECONDS", "60"))
    OPEN_API_SYNC_WAIT_SECONDS: float = float(os.getenv("OPEN_API_SYNC_WAIT_SECONDS", "60"))
    CHAT_RATE_LIMIT: int = int(os.getenv("CHAT_RATE_LIMIT", "30"))
    CHAT_RATE_WINDOW_SECONDS: int = int(os.getenv("CHAT_RATE_WINDOW_SECONDS", "60"))
    # 共享限流桶只需覆盖活跃窗口；更老的数据由 Worker 周期回收，避免表无限增长。
    RATE_LIMIT_BUCKET_TTL_SECONDS: int = int(
        os.getenv("RATE_LIMIT_BUCKET_TTL_SECONDS", "86400")
    )

    # 微信公众号接入：公众号要求 5s 内响应（失败重试至多 3 次 ≈ 15s）。
    # 同步等待此秒数智能体出结果再回复（须 < 5s）；超时则回 success，靠公众号重试或客服消息推送。
    WECHAT_SYNC_WAIT_SECONDS: float = float(os.getenv("WECHAT_SYNC_WAIT_SECONDS", "4.5"))
    # 单条回复（同步 XML 文本 / 客服消息）最大字符数，超出截断（公众号文本上限约 2048 字节）。
    WECHAT_REPLY_MAX_CHARS: int = int(os.getenv("WECHAT_REPLY_MAX_CHARS", "1500"))
    # 通用第三方接口（generic 渠道）：同步调用最多等待此秒数取结果；超时返回 job_id 供轮询。
    CHANNEL_SYNC_WAIT_SECONDS: float = float(os.getenv("CHANNEL_SYNC_WAIT_SECONDS", "60"))

    # 任务队列 / worker（M1）
    # 进程内 worker：单机部署默认开启；多副本部署可设为 false，改用独立 `python run.py worker` 进程。
    JOB_WORKER_ENABLED: bool = _env_bool("JOB_WORKER_ENABLED", True)
    JOB_WORKER_CONCURRENCY: int = int(os.getenv("JOB_WORKER_CONCURRENCY", "4"))
    JOB_MAX_INFLIGHT_PER_USER: int = int(os.getenv("JOB_MAX_INFLIGHT_PER_USER", "8"))
    JOB_MAX_RUNNING_PER_USER: int = int(os.getenv("JOB_MAX_RUNNING_PER_USER", "2"))
    JOB_MAX_QUEUED_GLOBAL: int = int(os.getenv("JOB_MAX_QUEUED_GLOBAL", "1000"))
    JOB_QUEUE_WARN_PENDING: int = int(os.getenv("JOB_QUEUE_WARN_PENDING", "100"))
    HEALTH_REQUIRE_WORKER: bool = _env_bool("HEALTH_REQUIRE_WORKER", True)
    JOB_PARTIAL_FLUSH_CHARS: int = int(os.getenv("JOB_PARTIAL_FLUSH_CHARS", "512"))
    JOB_PARTIAL_FLUSH_SECONDS: float = float(os.getenv("JOB_PARTIAL_FLUSH_SECONDS", "0.5"))
    JOB_POLL_SECONDS: float = float(os.getenv("JOB_POLL_SECONDS", "1.0"))
    JOB_HEARTBEAT_SECONDS: float = float(os.getenv("JOB_HEARTBEAT_SECONDS", "20"))
    JOB_RETRY_BASE_SECONDS: float = float(os.getenv("JOB_RETRY_BASE_SECONDS", "5"))
    JOB_RETRY_MAX_SECONDS: float = float(os.getenv("JOB_RETRY_MAX_SECONDS", "300"))
    JOB_SHUTDOWN_GRACE_SECONDS: float = float(
        os.getenv("JOB_SHUTDOWN_GRACE_SECONDS", "30")
    )
    JOB_CANCEL_CASCADE_LIMIT: int = int(os.getenv("JOB_CANCEL_CASCADE_LIMIT", "100"))
    # 会话标题不属于任务成功关键路径；超过此时间使用首问截断标题，不阻塞 Job 终态。
    TITLE_GENERATION_TIMEOUT_SECONDS: float = float(
        os.getenv("TITLE_GENERATION_TIMEOUT_SECONDS", "5.0")
    )
    # 新引导在 Worker 可领取前保留一个短暂撤回/编辑窗口，避免 UI 操作与检查点抢占。
    JOB_GUIDANCE_GRACE_SECONDS: float = float(os.getenv("JOB_GUIDANCE_GRACE_SECONDS", "3.0"))
    # 心跳租约：running 任务超过此秒数无心跳即视为 worker 失联，重新入队（须 > 心跳间隔）
    JOB_LEASE_SECONDS: int = int(os.getenv("JOB_LEASE_SECONDS", "120"))
    # 终态任务保留时长，过期清理
    JOB_TTL_SECONDS: int = int(os.getenv("JOB_TTL_SECONDS", "2592000"))
    ARTIFACT_TTL_SECONDS: int = int(os.getenv("ARTIFACT_TTL_SECONDS", "2592000"))
    CRON_SCHEDULER_ENABLED: bool = _env_bool("CRON_SCHEDULER_ENABLED", True)
    CRON_RETRY_BASE_SECONDS: float = float(os.getenv("CRON_RETRY_BASE_SECONDS", "30"))
    CRON_RETRY_MAX_SECONDS: float = float(os.getenv("CRON_RETRY_MAX_SECONDS", "3600"))
    CRON_HEARTBEAT_STALE_SECONDS: int = int(
        os.getenv("CRON_HEARTBEAT_STALE_SECONDS", "60")
    )


settings = Settings()


def security_config_problems(creating_root: bool = False) -> list[str]:
    """返回会导致启动失败的安全配置问题，不泄露任何密钥内容。"""
    problems: list[str] = []
    if settings.APP_ENV not in {"development", "test", "production"}:
        problems.append("APP_ENV 只能是 development、test 或 production")
    secret = settings.JWT_SECRET or ""
    if secret in INSECURE_JWT_SECRETS:
        problems.append("JWT_SECRET 仍为默认/占位值，必须改为随机字符串")
    elif len(secret.encode("utf-8")) < 32:
        problems.append("JWT_SECRET 长度不足 32 字节")
    if creating_root and settings.ROOT_PASSWORD == DEFAULT_ROOT_PASSWORD:
        problems.append("首次创建 root 账号仍使用默认口令")

    origins = [item.strip() for item in settings.CORS_ORIGINS.split(",") if item.strip()]
    if "*" in origins:
        problems.append("CORS_ORIGINS 不允许通配符，必须填写精确 Origin")

    if settings.APP_ENV == "production":
        master = settings.SECRET_MASTER_KEY or ""
        if not master:
            problems.append("生产环境必须配置独立的 SECRET_MASTER_KEY")
        elif len(master.encode("utf-8")) < 32:
            problems.append("SECRET_MASTER_KEY 长度不足 32 字节")
        elif master == secret:
            problems.append("SECRET_MASTER_KEY 必须与 JWT_SECRET 分离")
        if not settings.AUTH_COOKIE_SECURE:
            problems.append("生产环境必须启用 AUTH_COOKIE_SECURE")
        if settings.ALLOW_INSECURE_DEFAULTS:
            problems.append("生产环境禁止启用 ALLOW_INSECURE_DEFAULTS")
    return problems


def security_config_warnings() -> list[str]:
    """返回兼容模式风险，供健康检查与运维审计展示。"""
    warnings: list[str] = []
    master = settings.SECRET_MASTER_KEY or ""
    if not master:
        warnings.append("未配置独立 SECRET_MASTER_KEY，凭据加密仍使用 JWT_SECRET 兼容路径")
    elif master == (settings.JWT_SECRET or ""):
        warnings.append("SECRET_MASTER_KEY 与 JWT_SECRET 未隔离")
    if not settings.AUTH_COOKIE_SECURE:
        warnings.append("AUTH_COOKIE_SECURE 未启用，仅适合本地 HTTP 开发")
    if settings.DATABASE_URL.startswith("sqlite"):
        warnings.append("SQLite 仅适合单机部署；多副本应使用共享数据库")
    return warnings

for _d in (DATA_DIR, UPLOAD_DIR, EXPORT_DIR, KNOWLEDGE_DIR / "green", KNOWLEDGE_DIR / "vpp", TEMPLATES_DIR, SKILLS_DIR, BRANDING_DIR, WORKSPACE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
