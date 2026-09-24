"""FastAPI 应用入口：Harness 控制平面、运行平面与静态前端。"""
import asyncio
import contextlib
import html
import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from . import harness as harness_registry, jobs, scheduler, worker, weixin_channel
from .api import (
    agents, auth, capabilities, channels, chat, guardrails, improvement, mcp, open_api,
    model_governance, operations, providers, skills, templates, token_usage, users,
)
from .api import settings as settings_api
from .api import services, memories, reasoning
from .browser_access import cross_origin_cookie_write
from .config import (
    BRANDING_DIR,
    DEFAULT_ROOT_PASSWORD,
    FRONTEND_DIR,
    INSECURE_JWT_SECRETS,
    TEMPLATES_DIR,
    security_config_problems,
    settings,
)
from .database import Base, SessionLocal, engine
from .logging_utils import configure_secure_logging
from .rate_limit import client_ip
from .models import (
    AuditLog, ROLE_ROOT, Agent, Channel, HarnessVersion,
    ImprovementProposal, Job, McpServer, Project, ScheduledTask, Skill, Template,
    Thread, Turn, User,
)
from .security import hash_password
from .runtime import builtin_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
# httpx 的 INFO 日志会输出完整请求 URL；MCP 常把 API Key 放在查询参数中，
# 因而默认关闭其逐请求日志，并对其它日志中的常见密钥参数做最后一道脱敏。
configure_secure_logging()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    init_data()
    stop_event = asyncio.Event()
    worker_task = None
    scheduler_task = None
    await asyncio.to_thread(jobs.requeue_stale, settings.JOB_LEASE_SECONDS)
    if settings.JOB_WORKER_ENABLED:
        worker_task = asyncio.create_task(worker.run_worker(stop_event))
    if settings.CRON_SCHEDULER_ENABLED:
        scheduler_task = asyncio.create_task(scheduler.run_scheduler(stop_event))
    await weixin_channel.manager.start()
    try:
        yield
    finally:
        await weixin_channel.manager.stop()
        if worker_task is not None:
            stop_event.set()
            with contextlib.suppress(Exception):
                await worker_task
        if scheduler_task is not None:
            stop_event.set()
            with contextlib.suppress(Exception):
                await scheduler_task
        from .runtime import browser_cdp
        await browser_cdp.close_all()
        from .llm.client import aclose_http_client
        await aclose_http_client()
        # 释放连接池中的空闲数据库连接，保证优雅停机不依赖解释器终结器。
        await asyncio.to_thread(engine.dispose)


DOCS_DESCRIPTION = "目标驱动智能体与 Harness 版本管理接口。"
app = FastAPI(
    title=settings_api.DEFAULT_SITE_NAME,
    description=DOCS_DESCRIPTION,
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.exception_handler(jobs.JobQuotaExceeded)
async def job_quota_handler(_request, exc):
    return JSONResponse(status_code=429, content={"detail": str(exc)})


@app.exception_handler(jobs.JobOwnerUnavailable)
async def job_owner_unavailable_handler(_request, exc):
    return JSONResponse(status_code=401, content={"detail": str(exc)})


def _docs_branding() -> tuple[str, str]:
    db = SessionLocal()
    try:
        name = settings_api.get_setting(
            db, settings_api.SITE_NAME_KEY, ""
        ) or settings_api.DEFAULT_SITE_NAME
        return name, settings_api._logo_url(db)
    finally:
        db.close()


def custom_openapi():
    if app.openapi_schema is None:
        app.openapi_schema = get_openapi(
            title=settings_api.DEFAULT_SITE_NAME,
            version=app.version,
            description=DOCS_DESCRIPTION,
            routes=app.routes,
        )
    name, logo = _docs_branding()
    app.openapi_schema["info"]["title"] = name
    if logo:
        app.openapi_schema["info"]["x-logo"] = {"url": logo}
    else:
        app.openapi_schema["info"].pop("x-logo", None)
    return app.openapi_schema


app.openapi = custom_openapi


@app.get("/docs", include_in_schema=False)
def swagger_ui_html():
    name, logo = _docs_branding()
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{name} - 接口文档",
        swagger_favicon_url=logo or "/api/v1/settings/logo",
    )


@app.get("/redoc", include_in_schema=False)
def redoc_html():
    name, logo = _docs_branding()
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=f"{name} - 接口文档",
        redoc_favicon_url=logo or "/api/v1/settings/logo",
    )


_cors_origins = [value.strip() for value in settings.CORS_ORIGINS.split(",") if value.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        allow_credentials=True,
        max_age=600,
    )


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/v1/"):
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
    )
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _should_audit(method: str, path: str) -> bool:
    """所有 API 写请求都进入审计；流式读取本身是 GET，无需按路径前缀跳过。"""
    return method.upper() in _AUDIT_METHODS and path.startswith("/api/v1")


def _audit_identity(request):
    explicit = getattr(request.state, "audit_identity", None)
    if explicit is not None:
        return explicit
    if not request.headers.get("authorization") and not request.cookies.get(settings.AUTH_COOKIE_NAME):
        return None, "", ""
    from .security import resolve_request_user
    db = SessionLocal()
    try:
        user = resolve_request_user(request, db)
        return user.id, user.username, user.role
    except HTTPException:
        return None, "", ""
    finally:
        db.close()


def _write_audit_log(request, status_code: int) -> None:
    path = request.url.path
    if not _should_audit(request.method, path):
        return
    uid, username, role = _audit_identity(request)
    db = SessionLocal()
    try:
        db.add(
            AuditLog(
                user_id=uid,
                username=username,
                role=role,
                method=request.method.upper(),
                path=path[:512],
                status_code=int(status_code),
                ip=client_ip(request)[:64],
            )
        )
        db.commit()
    finally:
        db.close()


def _write_audit_log_safely(request, status_code: int) -> None:
    try:
        _write_audit_log(request, status_code)
    except Exception:
        logger.warning("审计日志写入失败", exc_info=True)


@app.middleware("http")
async def audit_log_middleware(request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        _write_audit_log_safely(request, 500)
        raise
    _write_audit_log_safely(request, response.status_code)
    return response


@app.middleware("http")
async def browser_access_middleware(request, call_next):
    if cross_origin_cookie_write(request):
        _write_audit_log_safely(request, 403)
        return JSONResponse(status_code=403, content={"detail": "不允许跨站提交，请返回当前工作区操作"}, headers={"Cache-Control": "no-store"})
    if request.url.path.startswith("/api/v1/") and (
        request.cookies.get(settings.AUTH_COOKIE_NAME) or request.headers.get("authorization")
    ):
        from .security import resolve_request_user
        def identity():
            with SessionLocal() as db:
                try:
                    user = resolve_request_user(request, db)
                except HTTPException:
                    return None
                return user.id, user.username, user.role
        actor = await asyncio.to_thread(identity)
        if actor:
            request.state.audit_identity = actor
    return await call_next(request)


app.include_router(auth.router)
app.include_router(reasoning.router)
app.include_router(users.router)
app.include_router(agents.router)
app.include_router(agents.keys_router)
app.include_router(agents.kb_router)
app.include_router(providers.router)
app.include_router(mcp.router)
app.include_router(skills.router)
app.include_router(templates.router)
app.include_router(settings_api.router)
app.include_router(settings_api.audit_router)
app.include_router(guardrails.router)
app.include_router(services.router)
app.include_router(memories.router)
app.include_router(token_usage.router)
app.include_router(model_governance.router)
app.include_router(operations.router)
app.include_router(chat.router)
app.include_router(improvement.router)
app.include_router(open_api.router)
app.include_router(channels.router)
app.include_router(channels.webhook_router)
app.include_router(channels.generic_router)
app.include_router(capabilities.router)


def _enforce_security_config(creating_root: bool) -> None:
    problems = security_config_problems(creating_root)
    if not problems:
        return
    if settings.ALLOW_INSECURE_DEFAULTS and settings.APP_ENV != "production":
        logger.warning("检测到不安全配置，开发模式已放行：%s", "；".join(problems))
        return
    raise RuntimeError("检测到不安全的安全配置，已拒绝启动：" + "；".join(problems))


def _migrate_harness_architecture() -> None:
    """已禁用的历史迁移入口；保留名称仅防止旧外部脚本静默调用。"""
    raise RuntimeError("手写迁移已永久禁用，请使用 Alembic upgrade head")
    # 以下旧实现不再可达，后续版本可在确认无人引用后物理删除。
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "agents" in tables:
        columns = {column["name"] for column in inspector.get_columns("agents")}
        if "active_version" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN active_version INTEGER DEFAULT 1"))
        if "builtin_tools" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN builtin_tools TEXT DEFAULT '[]'"))
                # 升级前所有 Agent 都能使用内置工具；迁移后保留现有行为，
                # 新建 Agent 则必须由 root 显式分配。
                conn.execute(
                    text("UPDATE agents SET builtin_tools=:tools"),
                    {"tools": json.dumps(sorted(builtin_tools.TOOLS), ensure_ascii=False)},
                )
    if "users" in tables:
        user_columns = {
            column["name"] for column in inspector.get_columns("users")
        }
        if "token_version" not in user_columns:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0"
                ))

    Base.metadata.create_all(bind=engine)
    job_columns = {
        column["name"] for column in inspect(engine).get_columns("jobs")
    }
    job_additions = {
        "idempotency_key": "VARCHAR(128)",
        "session_key": "VARCHAR(128) DEFAULT ''",
        "lease_token": "VARCHAR(32) DEFAULT ''",
        "attempt_count": "INTEGER DEFAULT 0",
        "max_attempts": "INTEGER DEFAULT 3",
        "event_sequence": "INTEGER DEFAULT 0",
    }
    with engine.begin() as conn:
        for name, ddl in job_additions.items():
            if name not in job_columns:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_owner_idempotency "
            "ON jobs(owner_id, idempotency_key)"
        ))
        conn.execute(text(
            "UPDATE jobs SET event_sequence=("
            "SELECT COALESCE(MAX(sequence), 0) FROM run_events "
            "WHERE run_events.run_id=jobs.id"
            ") WHERE event_sequence=0"
        ))
        conn.execute(text(
            "DELETE FROM run_events WHERE id NOT IN ("
            "SELECT MIN(id) FROM run_events GROUP BY run_id, sequence)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_event_sequence "
            "ON run_events(run_id, sequence)"
        ))
    conversation_columns = {
        column["name"] for column in inspect(engine).get_columns("conversations")
    }
    conversation_additions = {
        "project_id": "INTEGER REFERENCES projects(id)",
        "is_pinned": "BOOLEAN DEFAULT 0",
        "is_archived": "BOOLEAN DEFAULT 0",
    }
    missing_conversation_columns = {
        name: ddl for name, ddl in conversation_additions.items()
        if name not in conversation_columns
    }
    if missing_conversation_columns:
        with engine.begin() as conn:
            for name, ddl in missing_conversation_columns.items():
                conn.execute(text(f"ALTER TABLE conversations ADD COLUMN {name} {ddl}"))
    project_columns = {
        column["name"] for column in inspect(engine).get_columns("projects")
    }
    project_additions = {
        "is_pinned": "BOOLEAN DEFAULT 0",
        "is_archived": "BOOLEAN DEFAULT 0",
    }
    missing_project_columns = {
        name: ddl for name, ddl in project_additions.items()
        if name not in project_columns
    }
    if missing_project_columns:
        with engine.begin() as conn:
            for name, ddl in missing_project_columns.items():
                conn.execute(text(f"ALTER TABLE projects ADD COLUMN {name} {ddl}"))
    scheduled_columns = {
        column["name"] for column in inspect(engine).get_columns("scheduled_tasks")
    }
    if "session_id" not in scheduled_columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE scheduled_tasks ADD COLUMN session_id VARCHAR(40) DEFAULT ''")
            )
    provider_columns = {
        "wire_api": "VARCHAR(32) DEFAULT 'chat_completions'",
        "auth_type": "VARCHAR(16) DEFAULT 'bearer'",
        "auth_header": "VARCHAR(64) DEFAULT ''",
        "api_version": "VARCHAR(64) DEFAULT ''",
        "api_version_mode": "VARCHAR(16) DEFAULT 'none'",
        "custom_headers": "TEXT DEFAULT '{}'",
        "extra_body": "TEXT DEFAULT '{}'",
        "model_list_path": "VARCHAR(128) DEFAULT '/models'",
        "reasoning_effort": "VARCHAR(16) DEFAULT ''",
        "model_id": "VARCHAR(256) DEFAULT ''",
        "model_name": "VARCHAR(256) DEFAULT ''",
        "model_reasoning": "BOOLEAN DEFAULT 0",
        "model_input": "TEXT DEFAULT '[\"text\"]'",
        "context_window": "INTEGER DEFAULT 0",
        "max_tokens": "INTEGER DEFAULT 8192",
        "max_tokens_param": "VARCHAR(32) DEFAULT 'auto'",
        "timeout_ms": "INTEGER DEFAULT 120000",
        "max_retries": "INTEGER DEFAULT 3",
        "stream_max_retries": "INTEGER DEFAULT 3",
        "stream_idle_timeout_ms": "INTEGER DEFAULT 300000",
        "supports_temperature": "BOOLEAN DEFAULT 1",
    }
    existing_provider_columns = {
        column["name"] for column in inspect(engine).get_columns("model_providers")
    }
    missing_provider_columns = {
        name: ddl for name, ddl in provider_columns.items()
        if name not in existing_provider_columns
    }
    if missing_provider_columns:
        with engine.begin() as conn:
            for name, ddl in missing_provider_columns.items():
                conn.execute(text(f"ALTER TABLE model_providers ADD COLUMN {name} {ddl}"))
    db = SessionLocal()
    try:
        # 用户明确选择不兼容旧流程：删除原系统内置的流程智能体/设计器及配套 Skill。
        legacy_names = {
            "智能客服流程助手", "智能客服流程助手（示例）",
            "简报助手", "园区能源简报助手（示例）",
            "绿碳能源投运专家", "流程设计师",
        }
        legacy_agents = db.query(Agent).filter(Agent.name.in_(legacy_names)).all()
        legacy_ids = {agent.id for agent in legacy_agents}
        if legacy_ids:
            db.query(Channel).filter(Channel.agent_id.in_(legacy_ids)).update(
                {Channel.agent_id: None}, synchronize_session=False
            )
            db.query(Thread).filter(Thread.agent_id.in_(legacy_ids)).update(
                {Thread.agent_id: None}, synchronize_session=False
            )
            db.query(Turn).filter(Turn.agent_id.in_(legacy_ids)).update(
                {Turn.agent_id: None}, synchronize_session=False
            )
            db.query(Job).filter(Job.agent_id.in_(legacy_ids)).update(
                {Job.agent_id: None}, synchronize_session=False
            )
            for agent in db.query(Agent).filter(Agent.id.notin_(legacy_ids)).all():
                try:
                    child_ids = json.loads(agent.agent_ids or "[]")
                except (json.JSONDecodeError, TypeError):
                    child_ids = []
                if isinstance(child_ids, list) and legacy_ids.intersection(child_ids):
                    agent.agent_ids = json.dumps(
                        [value for value in child_ids if value not in legacy_ids],
                        ensure_ascii=False,
                    )
            db.query(ImprovementProposal).filter(
                ImprovementProposal.agent_id.in_(legacy_ids)
            ).delete(synchronize_session=False)
            db.query(HarnessVersion).filter(
                HarnessVersion.agent_id.in_(legacy_ids)
            ).delete(synchronize_session=False)
            for agent in legacy_agents:
                db.delete(agent)
        legacy_skills = db.query(Skill).filter(Skill.name.in_(
            {"workflow-design", "流程设计顾问"}
        )).all()
        legacy_skill_ids = {skill.id for skill in legacy_skills}
        if legacy_skill_ids:
            for agent in db.query(Agent).all():
                try:
                    skill_ids = json.loads(agent.skill_ids or "[]")
                except (json.JSONDecodeError, TypeError):
                    skill_ids = []
                if isinstance(skill_ids, list) and legacy_skill_ids.intersection(skill_ids):
                    agent.skill_ids = json.dumps(
                        [value for value in skill_ids if value not in legacy_skill_ids],
                        ensure_ascii=False,
                    )
            for skill in legacy_skills:
                db.delete(skill)
        # 原生免费 web_search/web_fetch 已替代默认 Tavily MCP。保留记录供 root
        # 需要时重新启用，但升级后默认停用并从 Agent 绑定中移除。
        tavily_rows = db.query(McpServer).filter(
            McpServer.name == "Tavily 联网搜索"
        ).all()
        tavily_ids = {row.id for row in tavily_rows}
        for row in tavily_rows:
            row.enabled = False
        if tavily_ids:
            for agent in db.query(Agent).all():
                try:
                    mcp_ids = json.loads(agent.mcp_ids or "[]")
                except (json.JSONDecodeError, TypeError):
                    mcp_ids = []
                if isinstance(mcp_ids, list) and tavily_ids.intersection(mcp_ids):
                    agent.mcp_ids = json.dumps(
                        [value for value in mcp_ids if value not in tavily_ids],
                        ensure_ascii=False,
                    )
        db.flush()
        # 旧定时任务绑定到同用户、同智能体最近的已有对话；找不到目标线程则暂停，
        # 防止升级后到期任务继续创建孤立的新对话。
        for task in db.query(ScheduledTask).filter(
            (ScheduledTask.session_id.is_(None)) | (ScheduledTask.session_id == "")
        ).all():
            latest = (
                db.query(Thread)
                .filter(
                    Thread.owner_id == task.owner_id,
                    Thread.agent_id == task.agent_id,
                )
                .order_by(Thread.updated_at.desc())
                .first()
            )
            if latest is not None:
                task.session_id = latest.id
            else:
                task.enabled = False
        old_prompts: dict[int, str] = {}
        columns = {column["name"] for column in inspect(engine).get_columns("agents")}
        if "system_prompt" in columns:
            for row in db.execute(text("SELECT id, system_prompt FROM agents")).all():
                old_prompts[int(row[0])] = str(row[1] or "")
        for agent in db.query(Agent).all():
            if harness_registry.active_version(db, agent) is None:
                harness_registry.create_version(
                    db,
                    agent,
                    system_prompt=old_prompts.get(agent.id, ""),
                    change_summary="迁移至 Harness 架构",
                    created_by=agent.created_by,
                    publish=True,
                )
        db.commit()
    finally:
        db.close()

    if engine.dialect.name == "sqlite":
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            current_tables = {
                row[0] for row in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "agents" in current_tables:
                agent_columns = {
                    row[1] for row in cursor.execute("PRAGMA table_info(agents)").fetchall()
                }
                if "workflow_id" in agent_columns:
                    cursor.execute("UPDATE agents SET workflow_id=NULL")
                legacy_columns = {"agent_type", "system_prompt", "workflow_id"} & agent_columns
                if legacy_columns:
                    cursor.execute("DROP TABLE IF EXISTS agents_harness_new")
                    cursor.execute(
                        """
                        CREATE TABLE agents_harness_new (
                            id INTEGER NOT NULL PRIMARY KEY,
                            name VARCHAR(128) NOT NULL UNIQUE,
                            description TEXT NOT NULL DEFAULT '',
                            opening_statement TEXT NOT NULL DEFAULT '',
                            enabled BOOLEAN NOT NULL DEFAULT 1,
                            provider_id INTEGER REFERENCES model_providers(id),
                            active_version INTEGER NOT NULL DEFAULT 1,
                            mcp_ids TEXT NOT NULL DEFAULT '[]',
                            skill_ids TEXT NOT NULL DEFAULT '[]',
                            agent_ids TEXT NOT NULL DEFAULT '[]',
                            builtin_tools TEXT NOT NULL DEFAULT '[]',
                            memory_enabled BOOLEAN NOT NULL DEFAULT 0,
                            routing TEXT NOT NULL DEFAULT '',
                            is_public BOOLEAN NOT NULL DEFAULT 0,
                            is_default BOOLEAN NOT NULL DEFAULT 0,
                            created_by INTEGER REFERENCES users(id),
                            created_at DATETIME NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO agents_harness_new (
                            id, name, description, opening_statement, enabled, provider_id,
                            active_version, mcp_ids, skill_ids, agent_ids, builtin_tools, memory_enabled,
                            routing, is_public, is_default, created_by, created_at
                        )
                        SELECT
                            id, name, COALESCE(description, ''), COALESCE(opening_statement, ''),
                            COALESCE(enabled, 1), provider_id, COALESCE(active_version, 1),
                            COALESCE(mcp_ids, '[]'), COALESCE(skill_ids, '[]'),
                            COALESCE(agent_ids, '[]'), COALESCE(builtin_tools, '[]'),
                            COALESCE(memory_enabled, 0),
                            COALESCE(routing, ''), COALESCE(is_public, 0),
                            COALESCE(is_default, 0), created_by, created_at
                        FROM agents
                        """
                    )
                    cursor.execute("DROP TABLE agents")
                    cursor.execute("ALTER TABLE agents_harness_new RENAME TO agents")
            cursor.execute("DROP TABLE IF EXISTS flow_steps")
            cursor.execute("DROP TABLE IF EXISTS workflows")
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            raw.close()


def _seed_core(db, root_id: int) -> None:
    if not db.query(Skill).first():
        db.add(
            Skill(
                name="通用分析",
                description="需要结构化分析、核验事实和给出可执行建议时使用。",
                instructions=(
                    "先明确目标与约束，再使用可用工具收集证据；区分事实、推断和建议。"
                    "完成前检查是否回答了用户问题，并明确不确定性。"
                ),
                resources="[]",
                enabled=True,
                is_public=True,
                created_by=root_id,
            )
        )
        db.flush()
    if not db.query(Agent).first():
        skill = db.query(Skill).first()
        agent = Agent(
            name="通用助手",
            description="目标驱动的通用智能体，支持工具、知识、附件和多轮记忆。",
            opening_statement="你好，需要我帮你完成什么？",
            enabled=True,
            skill_ids=json.dumps([skill.id] if skill else []),
            builtin_tools=json.dumps(
                sorted(builtin_tools.PUBLIC_AGENT_SAFE_TOOLS), ensure_ascii=False
            ),
            memory_enabled=True,
            is_public=True,
            is_default=True,
            created_by=root_id,
        )
        db.add(agent)
        db.flush()
        harness_registry.create_version(
            db,
            agent,
            system_prompt=(
                "你是一个可靠的通用智能体。围绕用户目标规划和执行；需要外部事实时使用工具；"
                "完成前核验结果。缺少关键权限或选择时明确询问，不虚构执行结果。"
            ),
            change_summary="初始 Harness",
            created_by=root_id,
            publish=True,
        )


def init_data() -> None:
    from .migrations import run_schema_migrations
    run_schema_migrations()
    db = SessionLocal()
    try:
        from .attachments import backfill_legacy
        promoted = backfill_legacy(db)
        if promoted:
            logger.info("已将 %s 个历史对话附件晋升为持久任务资产", promoted)
        root = db.query(User).filter(User.role == ROLE_ROOT).first()
        creating_root = root is None
        _enforce_security_config(creating_root)
        if creating_root:
            root = User(
                username=settings.ROOT_USERNAME,
                password_hash=hash_password(settings.ROOT_PASSWORD),
                role=ROLE_ROOT,
            )
            db.add(root)
            db.flush()
            logger.warning("已创建初始 root 账号：%s", settings.ROOT_USERNAME)
        _seed_core(db, root.id)
        db.commit()
        from .knowledge_retirement import retire_legacy_datasets
        retire_legacy_datasets(db)
    finally:
        db.close()


@app.get("/healthz", tags=["系统"])
def healthz():
    from .health import collect_health
    snapshot, ready = collect_health(
        engine,
        SessionLocal,
        require_worker=settings.HEALTH_REQUIRE_WORKER,
        require_scheduler=settings.CRON_SCHEDULER_ENABLED,
    )
    snapshot.update({"architecture": "harness", "version": app.version})
    return JSONResponse(status_code=200 if ready else 503, content=snapshot)


class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


app.mount("/static", _NoCacheStaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")
app.mount("/branding", StaticFiles(directory=str(BRANDING_DIR)), name="branding")
_PAGE_HEADERS = {"Cache-Control": "no-cache"}


def _render_frontend_page(filename: str, title_prefix: str = "") -> HTMLResponse:
    """Render current branding into the first HTML response to avoid stale-brand flash."""
    name, logo = _docs_branding()
    escaped_name = html.escape(name, quote=True)
    escaped_logo = html.escape(logo or "/branding/favicon.svg", quote=True)
    brand_markup = escaped_name
    if logo:
        brand_markup = (
            f'<img class="brand-logo" src="{escaped_logo}" '
            f'alt="{escaped_name} Logo" />{escaped_name}'
        )

    page = (FRONTEND_DIR / filename).read_text(encoding="utf-8")
    page = page.replace("__BRAND_DOCUMENT_TITLE__", html.escape(f"{title_prefix}{name}"))
    page = page.replace("__BRAND_LOGO_URL__", escaped_logo)
    page = page.replace("__BRAND_MARKUP__", brand_markup)
    return HTMLResponse(page, headers=_PAGE_HEADERS)


@app.get("/", include_in_schema=False)
def index_page(request: Request):
    from .security import resolve_request_user
    with SessionLocal() as db:
        try:
            resolve_request_user(request, db)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
    return _render_frontend_page("index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return _render_frontend_page("login.html", "登录 - ")


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    from .security import resolve_request_user
    with SessionLocal() as db:
        try:
            user = resolve_request_user(request, db)
        except HTTPException:
            return RedirectResponse("/login?next=%2Fadmin", status_code=303)
        if user.role == "guest":
            return RedirectResponse("/login?next=%2Fadmin", status_code=303)
    return _render_frontend_page("admin.html", "设置 - ")
