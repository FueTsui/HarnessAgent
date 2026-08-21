"""智能体目录、能力绑定与不可变 Harness 版本管理。"""
import json
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile, File, Form, Query, status
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from .. import harness as harness_registry
from ..models import (
    Agent, ApiKey, Channel, HarnessVersion, ImprovementProposal,
    Job, McpServer, ModelProvider, Project, Skill, Thread, Turn, User,
)
from ..capabilities import knowledge
from ..model_governance import resolve_route, validate_policy_routing
from ..resource_governance import resource_state
from ..runtime import builtin_tools
from ..schemas import (
    AgentCreate, AgentOut, AgentUpdate, ApiKeyCreate, ApiKeyOut,
    HarnessVersionCreate, HarnessVersionOut,
)
from ..upload_utils import read_upload_limited
from ..security import (
    generate_api_key,
    can_access_agent,
    can_manage,
    get_current_user,
    hash_api_key,
    is_root,
    require_owner,
    require_module,
    require_root,
    require_use,
    scope_owned,
)

router = APIRouter(prefix="/api/v1/agents", tags=["智能体管理（root/admin）"])
keys_router = APIRouter(prefix="/api/v1/api-keys", tags=["API密钥（root/admin）"])
kb_router = APIRouter(prefix="/api/v1/knowledge", tags=["知识库（root/admin）"])


# ---------- 序列化助手 ----------

def _loads(text: str, default):
    try:
        value = json.loads(text) if text else default
        return value if isinstance(value, type(default)) else default
    except (json.JSONDecodeError, TypeError):
        return default


def _to_out(db: Session, a: Agent, user: User | None = None) -> AgentOut:
    is_default = bool(getattr(a, "is_default", False))
    if user is None:
        manageable = True
    elif is_default:
        manageable = is_root(user)  # 默认智能体仅 root 可管理
    else:
        manageable = can_manage(user, a)
    version = harness_registry.active_version(db, a)
    return AgentOut(
        id=a.id, name=a.name, description=a.description,
        system_prompt=version.system_prompt if version else "",
        opening_statement=a.opening_statement, enabled=a.enabled,
        provider_id=a.provider_id, active_version=a.active_version,
        mcp_ids=_loads(a.mcp_ids, []), skill_ids=_loads(a.skill_ids, []),
        agent_ids=_loads(getattr(a, "agent_ids", "[]"), []),
        builtin_tools=_loads(getattr(a, "builtin_tools", "[]"), []),
        memory_enabled=bool(getattr(a, "memory_enabled", False)),
        routing=_loads(a.routing, {}), is_public=bool(getattr(a, "is_public", False)),
        is_default=is_default,
        can_manage=manageable,
    )


def _key_out(k: ApiKey, user: User) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id, name=k.name, prefix=k.prefix, is_active=k.is_active,
        can_manage=can_manage(user, k),
    )


def _validate_refs(db: Session, user: User, *, provider_id, mcp_ids, skill_ids, agent_ids=None) -> None:
    if provider_id is not None and db.get(ModelProvider, provider_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "指定的模型提供商不存在")
    if provider_id is not None:
        require_use(user, db.get(ModelProvider, provider_id), "无权使用该模型提供商")
    for mid in mcp_ids or []:
        server = db.get(McpServer, mid)
        if server is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MCP 服务 #{mid} 不存在")
        require_use(user, server, f"无权使用 MCP #{mid}")
    for sid in skill_ids or []:
        skill = db.get(Skill, sid)
        if skill is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"技能 #{sid} 不存在")
        require_use(user, skill, f"无权使用 Skills #{sid}")
    for aid in agent_ids or []:
        target = db.get(Agent, aid)
        if target is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"智能体 #{aid} 不存在")
        require_use(user, target, f"无权调用智能体 #{aid}")


def _unique_agent_name(db: Session, base: str) -> str:
    base = (base or "导入智能体").strip() or "导入智能体"
    name, i = base, 1
    while db.query(Agent).filter(Agent.name == name).first():
        i += 1
        name = f"{base} ({i})"
    return name


def _agent_export_dict(db: Session, a: Agent) -> dict:
    version = harness_registry.active_version(db, a)
    return {
        "name": a.name,
        "description": a.description,
        "system_prompt": version.system_prompt if version else "",
        "opening_statement": a.opening_statement,
        "enabled": a.enabled,
        "provider_id": a.provider_id,
        "mcp_ids": _loads(a.mcp_ids, []),
        "skill_ids": _loads(a.skill_ids, []),
        "agent_ids": _loads(getattr(a, "agent_ids", "[]"), []),
        "builtin_tools": _loads(getattr(a, "builtin_tools", "[]"), []),
        "memory_enabled": bool(getattr(a, "memory_enabled", False)),
        "routing": _loads(a.routing, {}),
    }


def _clean_id_list(value) -> list[int]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _clean_tool_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        str(item).strip() for item in value if str(item).strip()
    ))


def _validate_builtin_tools(user: User, names: list[str] | None) -> list[str]:
    """内置工具只能由 root 分配；绑定可保留已被全局停用的工具。"""
    if names is None:
        return []
    if not is_root(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅 root 可分配内置能力")
    clean = _clean_tool_list(names)
    unknown = sorted(set(clean) - set(builtin_tools.TOOLS))
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "未知内置能力：" + "、".join(unknown),
        )
    return clean


def _optional_int(value, label: str) -> int | None:
    if value in ("", None):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{label} 必须为数字")


def _parse_ids_param(ids: str) -> list[int]:
    selected = []
    for raw in (ids or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            selected.append(int(raw))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "导出 ids 必须为逗号分隔的数字")
    if not selected:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先选择要导出的智能体")
    return selected


def _validate_routing_refs(db: Session, user: User, routing: dict) -> dict:
    if isinstance(routing, dict) and routing.get("mode") == "policy":
        def validate_provider(provider_id: int) -> None:
            _validate_refs(
                db, user, provider_id=provider_id, mcp_ids=None, skill_ids=None
            )

        try:
            return validate_policy_routing(db, user, routing, validate_provider)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not isinstance(routing, dict) or routing.get("mode") != "rules":
        return {}
    clean_rules = []
    for rule in routing.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        provider_id = rule.get("provider_id")
        if provider_id is None:
            continue
        provider_id = _optional_int(provider_id, "模型路由 provider_id")
        _validate_refs(db, user, provider_id=provider_id, mcp_ids=None, skill_ids=None)
        clean_rules.append({
            "match": str(rule.get("match") or "keyword"),
            "value": str(rule.get("value") or ""),
            "provider_id": provider_id,
        })
    default_provider_id = routing.get("default_provider_id")
    if default_provider_id in ("", None):
        default_provider_id = None
    else:
        default_provider_id = _optional_int(default_provider_id, "模型路由默认 provider_id")
        _validate_refs(db, user, provider_id=default_provider_id, mcp_ids=None, skill_ids=None)
    return {"mode": "rules", "rules": clean_rules, "default_provider_id": default_provider_id}


# ---------- 运行时解析助手（chat / open_api 调用） ----------

def _match_rule(rule: dict, query: str) -> bool:
    match, value = rule.get("match"), rule.get("value")
    if match == "keyword":
        return bool(value) and str(value) in (query or "")
    if match == "length_gt":
        try:
            return len(query or "") > int(value)
        except (TypeError, ValueError):
            return False
    return False


def resolve_provider(db: Session, agent: Agent, query: str = "") -> Optional[ModelProvider]:
    """按模型路由配置选择提供商；返回 None 表示使用默认本地模型。"""
    routing = _loads(agent.routing, {})
    if routing.get("mode") == "policy":
        return resolve_route(db, agent, query).primary
    provider_id = agent.provider_id
    if routing.get("mode") == "rules":
        chosen = None
        for rule in routing.get("rules", []):
            if isinstance(rule, dict) and _match_rule(rule, query):
                chosen = rule.get("provider_id")
                break
        provider_id = chosen if chosen is not None else (
            routing.get("default_provider_id") or agent.provider_id
        )
    if provider_id is None:
        return None
    provider = db.get(ModelProvider, provider_id)
    return provider if (provider is not None and provider.enabled) else None


def agent_skills(db: Session, agent: Agent, extra_ids=None) -> list[dict]:
    """挂载及本轮显式选择的启用技能。"""
    out = []
    selected = list(dict.fromkeys([*_loads(agent.skill_ids, []), *(extra_ids or [])]))
    for sid in selected:
        skill = db.get(Skill, sid)
        if skill is None or not skill.enabled:
            continue
        out.append({
            "id": skill.id, "name": skill.name, "description": skill.description,
            "instructions": skill.instructions,
            "resources": _loads(skill.resources, []),
        })
    return out


def agent_mcp_servers(db: Session, agent: Agent, extra_ids=None) -> list[McpServer]:
    """挂载及本轮显式选择的启用 MCP 服务对象列表。"""
    out = []
    selected = list(dict.fromkeys([*_loads(agent.mcp_ids, []), *(extra_ids or [])]))
    for mid in selected:
        server = db.get(McpServer, mid)
        governance = resource_state(db, "mcp", mid)
        if server is not None and server.enabled and not governance.get("review_required"):
            out.append(server)
    return out


# ---------- 普通用户可见 ----------

@router.get("/enabled", response_model=list[AgentOut])
def list_enabled_agents(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """对话页可选智能体：root 见全部已启用；其他用户仅见「默认智能体」与「已开放(public)」的。"""
    q = db.query(Agent).filter(Agent.enabled.is_(True))
    if not is_root(user):
        q = q.filter((Agent.is_default.is_(True)) | (Agent.is_public.is_(True)))
    # 默认智能体排在最前，便于前端取首项作为默认载入
    rows = q.order_by(Agent.is_default.desc(), Agent.id).all()
    return [_to_out(db, a, user) for a in rows]


@router.get("/export")
def export_agents(
    ids: str = Query(default=""),
    admin: User = Depends(require_module("agents")),
    db: Session = Depends(get_db),
):
    """导出选中的智能体 JSON。公开只读资源可导出，导入后归当前管理员所有。"""
    selected = _parse_ids_param(ids)
    q = scope_owned(db.query(Agent), Agent, admin).filter(Agent.id.in_(selected))
    return [_agent_export_dict(db, a) for a in q.order_by(Agent.id).all()]


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_agents(
    file: UploadFile = File(...),
    admin: User = Depends(require_module("agents")),
    db: Session = Depends(get_db),
):
    """从 .json 导入智能体（数组或单个对象）。名称冲突时自动追加序号。"""
    raw = await read_upload_limited(file, 10 * 1024 * 1024, "智能体导入文件")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"解析失败：{exc}")
    items = data if isinstance(data, list) else [data]
    created = []
    skipped = 0
    for d in items:
        if not isinstance(d, dict):
            skipped += 1
            continue
        name = str(d.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        provider_id = _optional_int(d.get("provider_id"), "provider_id")
        mcp_ids = _clean_id_list(d.get("mcp_ids"))
        skill_ids = _clean_id_list(d.get("skill_ids"))
        agent_ids = _clean_id_list(d.get("agent_ids"))
        assigned_tools = (
            _validate_builtin_tools(admin, d.get("builtin_tools"))
            if "builtin_tools" in d else []
        )
        routing = _validate_routing_refs(db, admin, d.get("routing") or {})
        _validate_refs(
            db, admin, provider_id=provider_id,
            mcp_ids=mcp_ids, skill_ids=skill_ids, agent_ids=agent_ids,
        )
        agent = Agent(
            name=_unique_agent_name(db, name),
            description=str(d.get("description") or ""),
            opening_statement=str(d.get("opening_statement") or ""),
            enabled=bool(d.get("enabled", True)),
            provider_id=provider_id,
            mcp_ids=json.dumps(mcp_ids, ensure_ascii=False),
            skill_ids=json.dumps(skill_ids, ensure_ascii=False),
            agent_ids=json.dumps(agent_ids, ensure_ascii=False),
            builtin_tools=json.dumps(assigned_tools, ensure_ascii=False),
            memory_enabled=bool(d.get("memory_enabled", False)),
            routing=json.dumps(routing, ensure_ascii=False) if routing else "",
            created_by=admin.id,
        )
        db.add(agent)
        db.flush()
        harness_registry.create_version(
            db, agent,
            system_prompt=str(d.get("system_prompt") or ""),
            change_summary="导入智能体",
            created_by=admin.id,
            publish=True,
        )
        created.append(agent.name)
    db.commit()
    return {"imported": len(created), "skipped": skipped, "names": created}


# ---------- root/admin 管理 ----------

@router.get("", response_model=list[AgentOut])
def list_all_agents(admin: User = Depends(require_module("agents")), db: Session = Depends(get_db)):
    q = scope_owned(db.query(Agent), Agent, admin)
    return [_to_out(db, a, admin) for a in q.order_by(Agent.id).all()]


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(body: AgentCreate, admin: User = Depends(require_module("agents")), db: Session = Depends(get_db)):
    if db.query(Agent).filter(Agent.name == body.name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "智能体名称已存在")
    _validate_refs(
        db, admin, provider_id=body.provider_id,
        mcp_ids=body.mcp_ids, skill_ids=body.skill_ids, agent_ids=body.agent_ids,
    )
    assigned_tools = _validate_builtin_tools(admin, body.builtin_tools)
    agent = Agent(
        name=body.name,
        description=body.description,
        opening_statement=body.opening_statement,
        enabled=body.enabled,
        provider_id=body.provider_id,
        mcp_ids=json.dumps(body.mcp_ids or [], ensure_ascii=False),
        skill_ids=json.dumps(body.skill_ids or [], ensure_ascii=False),
        agent_ids=json.dumps(body.agent_ids or [], ensure_ascii=False),
        builtin_tools=json.dumps(assigned_tools, ensure_ascii=False),
        memory_enabled=body.memory_enabled,
        routing=json.dumps(body.routing or {}, ensure_ascii=False) if body.routing else "",
        is_public=body.is_public,
        created_by=admin.id,
    )
    db.add(agent)
    db.flush()
    harness_registry.create_version(
        db, agent,
        system_prompt=body.system_prompt,
        change_summary="创建智能体",
        created_by=admin.id,
        publish=True,
    )
    db.commit()
    db.refresh(agent)
    return _to_out(db, agent, admin)


@router.patch("/{agent_id}", response_model=AgentOut)
def update_agent(
    agent_id: int, body: AgentUpdate, admin: User = Depends(require_module("agents")), db: Session = Depends(get_db)
):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "智能体不存在")
    # 默认智能体仅 root 可配置；其余按所有权校验
    if agent.is_default and not is_root(admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "默认智能体仅 root 可配置")
    else:
        require_owner(admin, agent)
    # 设置/清除「默认智能体」标记：仅 root，且全局至多一个
    if body.is_default is not None:
        if not is_root(admin):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "仅 root 可设置默认智能体")
        if body.is_default:
            if not agent.enabled and body.enabled is False:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "默认智能体必须为启用状态")
            db.query(Agent).filter(Agent.id != agent.id, Agent.is_default.is_(True)).update(
                {Agent.is_default: False}
            )
            agent.is_default = True
            agent.enabled = True
        else:
            agent.is_default = False
    if body.name and body.name != agent.name:
        if db.query(Agent).filter(Agent.name == body.name).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "智能体名称已存在")
        agent.name = body.name
    if body.description is not None:
        agent.description = body.description
    if body.system_prompt is not None:
        active = harness_registry.active_version(db, agent)
        if active is None or active.system_prompt != body.system_prompt:
            harness_registry.create_version(
                db, agent,
                system_prompt=body.system_prompt,
                tool_policy=_loads(active.tool_policy, {}) if active else {},
                memory_policy=_loads(active.memory_policy, {}) if active else {},
                verification_policy=_loads(active.verification_policy, {}) if active else {},
                output_policy=_loads(active.output_policy, {}) if active else {},
                change_summary="更新系统指令",
                created_by=admin.id,
                publish=True,
            )
    if body.opening_statement is not None:
        agent.opening_statement = body.opening_statement
    if body.enabled is not None:
        if agent.is_default and not body.enabled:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "默认智能体不可停用")
        agent.enabled = body.enabled

    # 模型提供商
    if body.clear_provider:
        agent.provider_id = None
    elif body.provider_id is not None:
        _validate_refs(db, admin, provider_id=body.provider_id, mcp_ids=None, skill_ids=None)
        agent.provider_id = body.provider_id

    if body.mcp_ids is not None:
        _validate_refs(db, admin, provider_id=None, mcp_ids=body.mcp_ids, skill_ids=None)
        agent.mcp_ids = json.dumps(body.mcp_ids, ensure_ascii=False)
    if body.skill_ids is not None:
        _validate_refs(db, admin, provider_id=None, mcp_ids=None, skill_ids=body.skill_ids)
        agent.skill_ids = json.dumps(body.skill_ids, ensure_ascii=False)
    if body.agent_ids is not None:
        if agent.id in (body.agent_ids or []):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "智能体不能调用自身")
        _validate_refs(db, admin, provider_id=None, mcp_ids=None, skill_ids=None, agent_ids=body.agent_ids)
        agent.agent_ids = json.dumps(body.agent_ids, ensure_ascii=False)
    if body.builtin_tools is not None:
        assigned_tools = _validate_builtin_tools(admin, body.builtin_tools)
        agent.builtin_tools = json.dumps(assigned_tools, ensure_ascii=False)
    if body.memory_enabled is not None:
        agent.memory_enabled = body.memory_enabled
    if body.routing is not None:
        routing = _validate_routing_refs(db, admin, body.routing)
        agent.routing = json.dumps(routing, ensure_ascii=False) if routing else ""
    if body.is_public is not None:
        agent.is_public = body.is_public

    db.commit()
    db.refresh(agent)
    return _to_out(db, agent, admin)


@router.get("/{agent_id}/versions", response_model=list[HarnessVersionOut])
def list_harness_versions(
    agent_id: int,
    admin: User = Depends(require_module("agents")),
    db: Session = Depends(get_db),
):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "智能体不存在")
    require_owner(admin, agent)
    rows = (
        db.query(HarnessVersion)
        .filter(HarnessVersion.agent_id == agent_id)
        .order_by(HarnessVersion.version.desc())
        .all()
    )
    return [harness_registry.as_dict(row) for row in rows]


@router.post(
    "/{agent_id}/versions",
    response_model=HarnessVersionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_harness_version(
    agent_id: int,
    body: HarnessVersionCreate,
    admin: User = Depends(require_module("agents")),
    db: Session = Depends(get_db),
):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "智能体不存在")
    require_owner(admin, agent)
    row = harness_registry.create_version(
        db,
        agent,
        system_prompt=body.system_prompt,
        tool_policy=body.tool_policy,
        memory_policy=body.memory_policy,
        verification_policy=body.verification_policy,
        output_policy=body.output_policy,
        change_summary=body.change_summary,
        created_by=admin.id,
        publish=body.publish,
    )
    db.commit()
    db.refresh(row)
    return harness_registry.as_dict(row)


@router.post("/{agent_id}/versions/{version}/publish", response_model=HarnessVersionOut)
def publish_harness_version(
    agent_id: int,
    version: int,
    admin: User = Depends(require_module("agents")),
    db: Session = Depends(get_db),
):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "智能体不存在")
    require_owner(admin, agent)
    try:
        row = harness_registry.publish_version(db, agent, version)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Harness 版本不存在")
    db.commit()
    db.refresh(row)
    return harness_registry.as_dict(row)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: int, admin: User = Depends(require_module("agents")), db: Session = Depends(get_db)):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "智能体不存在")
    require_owner(admin, agent)
    if agent.is_default and not is_root(admin):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "默认智能体不可删除，请先将其它智能体设为默认")
    bound_channels = db.query(Channel).filter(Channel.agent_id == agent_id).count()
    if bound_channels:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"仍有 {bound_channels} 个第三方渠道绑定该智能体，请先解绑",
        )
    # 历史会话与终态任务保留，但解除智能体外键；同时清理其它智能体的 JSON 子智能体引用。
    db.query(Thread).filter(Thread.agent_id == agent_id).update(
        {Thread.agent_id: None}, synchronize_session=False
    )
    db.query(Turn).filter(Turn.agent_id == agent_id).update(
        {Turn.agent_id: None}, synchronize_session=False
    )
    db.query(Job).filter(Job.agent_id == agent_id).update(
        {Job.agent_id: None}, synchronize_session=False
    )
    # Upgraded SQLite databases cannot add this FK with ALTER TABLE, therefore
    # clear project defaults explicitly instead of relying on ON DELETE SET NULL.
    db.query(Project).filter(Project.default_agent_id == agent_id).update(
        {Project.default_agent_id: None}, synchronize_session=False
    )
    for other in db.query(Agent).filter(Agent.id != agent_id).all():
        ids = _loads(other.agent_ids, [])
        if agent_id in ids:
            other.agent_ids = json.dumps([value for value in ids if value != agent_id])
    db.query(ImprovementProposal).filter(ImprovementProposal.agent_id == agent_id).delete()
    db.query(HarnessVersion).filter(HarnessVersion.agent_id == agent_id).delete()
    db.delete(agent)
    db.commit()


# ---------- API 密钥（开放接口凭证） ----------

@keys_router.get("", response_model=list[ApiKeyOut])
def list_api_keys(admin: User = Depends(require_module("keys")), db: Session = Depends(get_db)):
    return [_key_out(k, admin) for k in scope_owned(db.query(ApiKey), ApiKey, admin).order_by(ApiKey.id).all()]


@keys_router.post("", status_code=status.HTTP_201_CREATED)
def create_api_key(body: ApiKeyCreate, admin: User = Depends(require_module("keys")), db: Session = Depends(get_db)):
    raw_key = generate_api_key()
    record = ApiKey(
        name=body.name,
        key_hash=hash_api_key(raw_key),
        prefix=raw_key[:12],
        created_by=admin.id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    # 明文 key 仅在创建时返回一次
    return {"id": record.id, "name": record.name, "api_key": raw_key}


@keys_router.patch("/{key_id}", response_model=ApiKeyOut)
def toggle_api_key(key_id: int, admin: User = Depends(require_module("keys")), db: Session = Depends(get_db)):
    record = db.get(ApiKey, key_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "密钥不存在")
    require_owner(admin, record)
    record.is_active = not record.is_active
    db.commit()
    db.refresh(record)
    return _key_out(record, admin)


@keys_router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(key_id: int, admin: User = Depends(require_module("keys")), db: Session = Depends(get_db)):
    record = db.get(ApiKey, key_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "密钥不存在")
    require_owner(admin, record)
    db.delete(record)
    db.commit()


# ---------- 知识库管理 ----------

@kb_router.get("")
def list_knowledge(admin: User = Depends(require_module("knowledge"))):
    return knowledge.list_documents(admin)


@kb_router.get("/datasets")
def list_knowledge_datasets(admin: User = Depends(require_module("knowledge"))):
    return knowledge.list_datasets(admin)


@kb_router.post("/datasets", status_code=status.HTTP_201_CREATED)
def create_knowledge_dataset(
    body: dict = Body(...), admin: User = Depends(require_module("knowledge"))
):
    try:
        return knowledge.create_dataset(str(body.get("name") or ""), admin)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@kb_router.delete("/datasets/{dataset}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge_dataset(
    dataset: str, admin: User = Depends(require_module("knowledge"))
):
    try:
        if not knowledge.delete_dataset(dataset, admin):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))


@kb_router.patch("/datasets/{dataset}/visibility")
def update_knowledge_dataset_visibility(
    dataset: str, body: dict = Body(...), admin: User = Depends(require_module("knowledge"))
):
    try:
        return knowledge.set_dataset_public(dataset, bool(body.get("is_public")), admin)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))


@kb_router.post("/{dataset}", status_code=status.HTTP_201_CREATED)
async def upload_knowledge(
    dataset: str, file: UploadFile = File(...), admin: User = Depends(require_module("knowledge"))
):
    try:
        raw = await read_upload_limited(
            file, settings.MAX_FILE_MB * 1024 * 1024, "知识库文档"
        )
        path = knowledge.save_document(dataset, file.filename or "doc.txt", raw, admin)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    return {"saved": path.name, "dataset": dataset}


@kb_router.delete("/{dataset}/{filename}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge(dataset: str, filename: str, admin: User = Depends(require_module("knowledge"))):
    try:
        if not knowledge.delete_document(dataset, filename, admin):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
