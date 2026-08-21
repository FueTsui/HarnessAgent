"""MCP 服务管理（root/admin）：通用工具接口，供智能体挂载调用。

- 卡片式 CRUD；headers 以 JSON 存储（鉴权头等）
- 「测试」按钮实际连接并列出工具，验证连通性
普通用户无访问权限。
"""
import json
import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..llm import mcp_client
from ..models import Agent, McpServer, User
from ..resource_governance import (
    public_lifecycle, record_version, resource_state, update_resource_state, versions,
)
from ..schemas import McpServerCreate, McpServerOut, McpServerUpdate
from ..security import can_manage, require_module, require_owner, scope_owned
from ..upload_utils import read_upload_limited

router = APIRouter(prefix="/api/v1/mcp-servers", tags=["MCP服务（root/admin）"])


def _mcp_version_snapshot(s: McpServer, catalog_hash: str = "") -> dict:
    return {
        "name": s.name,
        "description_sha256": hashlib.sha256((s.description or "").encode("utf-8")).hexdigest(),
        "transport": s.transport,
        "risk_policy": s.risk_policy,
        "enabled": bool(s.enabled),
        "is_public": bool(s.is_public),
        "catalog_hash": catalog_hash,
    }


def _to_out(s: McpServer, user: User | None = None, db: Session | None = None) -> McpServerOut:
    try:
        headers = json.loads(s.headers) if s.headers else {}
    except json.JSONDecodeError:
        headers = {}
    manageable = True if user is None else can_manage(user, s)
    if not manageable:
        # 公开 MCP 可被其它管理员挂载使用，但不能读取其鉴权头。
        headers = {str(key): "********" for key in headers}
    lifecycle = public_lifecycle(db, "mcp", s.id) if db is not None else {}
    governance = resource_state(db, "mcp", s.id) if db is not None else {}
    return McpServerOut(
        id=s.id, name=s.name, description=s.description, transport=s.transport,
        url=s.url, headers=headers if isinstance(headers, dict) else {}, enabled=s.enabled,
        risk_policy=str(getattr(s, "risk_policy", "auto") or "auto"),
        is_public=bool(getattr(s, "is_public", False)),
        can_manage=manageable,
        catalog_hash=str(governance.get("catalog_hash") or ""),
        review_required=bool(governance.get("review_required")),
        **lifecycle,
    )


@router.get("", response_model=list[McpServerOut])
def list_servers(admin: User = Depends(require_module("mcp")), db: Session = Depends(get_db)):
    q = scope_owned(db.query(McpServer), McpServer, admin)
    return [_to_out(s, admin, db) for s in q.order_by(McpServer.id).all()]


@router.post("", response_model=McpServerOut, status_code=status.HTTP_201_CREATED)
def create_server(
    body: McpServerCreate, admin: User = Depends(require_module("mcp")), db: Session = Depends(get_db)
):
    if db.query(McpServer).filter(McpServer.name == body.name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "MCP 服务名称已存在")
    server = McpServer(
        name=body.name, description=body.description, transport=body.transport,
        url=body.url.rstrip("/") if body.transport == "http" else body.url,
        headers=json.dumps(body.headers, ensure_ascii=False),
        risk_policy=body.risk_policy,
        enabled=body.enabled, created_by=admin.id,
    )
    db.add(server)
    db.flush()
    record_version(db, "mcp", server.id, _mcp_version_snapshot(server), actor_id=admin.id, change="created")
    db.commit()
    db.refresh(server)
    return _to_out(server, admin, db)


@router.patch("/{server_id}", response_model=McpServerOut)
def update_server(
    server_id: int, body: McpServerUpdate, admin: User = Depends(require_module("mcp")), db: Session = Depends(get_db)
):
    server = db.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP 服务不存在")
    require_owner(admin, server)
    if body.name and body.name != server.name:
        if db.query(McpServer).filter(McpServer.name == body.name).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "MCP 服务名称已存在")
        server.name = body.name
    if body.description is not None:
        server.description = body.description
    if body.transport is not None:
        server.transport = body.transport
    if body.url is not None:
        server.url = body.url
    if body.headers is not None:
        server.headers = json.dumps(body.headers, ensure_ascii=False)
    if body.risk_policy is not None:
        server.risk_policy = body.risk_policy
    if body.enabled is not None:
        server.enabled = body.enabled
    if body.is_public is not None:
        server.is_public = body.is_public
    state = resource_state(db, "mcp", server.id)
    record_version(db, "mcp", server.id, _mcp_version_snapshot(server, str(state.get("catalog_hash") or "")), actor_id=admin.id, change="updated")
    db.commit()
    db.refresh(server)
    return _to_out(server, admin, db)


@router.get("/{server_id}/versions")
def list_server_versions(
    server_id: int,
    admin: User = Depends(require_module("mcp")),
    db: Session = Depends(get_db),
):
    server = db.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP 服务不存在")
    require_owner(admin, server)
    return {"resource_type": "mcp", "resource_id": server.id, "items": versions(db, "mcp", server.id)}


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(server_id: int, admin: User = Depends(require_module("mcp")), db: Session = Depends(get_db)):
    server = db.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP 服务不存在")
    require_owner(admin, server)
    for agent in db.query(Agent).all():
        try:
            ids = json.loads(agent.mcp_ids or "[]")
        except json.JSONDecodeError:
            ids = []
        if isinstance(ids, list) and server_id in ids:
            agent.mcp_ids = json.dumps([value for value in ids if value != server_id])
    db.delete(server)
    db.commit()


# ---------- 导入 / 导出（JSON） ----------

def _mcp_dict(s: McpServer) -> dict:
    out = _to_out(s)
    return {"name": out.name, "description": out.description, "transport": out.transport,
            "url": out.url, "headers": {}, "risk_policy": out.risk_policy,
            "enabled": out.enabled}


def _unique_mcp_name(db: Session, base: str) -> str:
    base = (base or "导入MCP").strip() or "导入MCP"
    name, i = base, 1
    while db.query(McpServer).filter(McpServer.name == name).first():
        i += 1
        name = f"{base} ({i})"
    return name


def _parse_ids_optional(ids: str) -> list[int]:
    """解析逗号分隔的 id；为空返回 []（表示不过滤、导出全部）。非数字报 400。"""
    selected = []
    for raw in (ids or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if not raw.lstrip("-").isdigit():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "导出 ids 必须为逗号分隔的数字")
        selected.append(int(raw))
    return selected


@router.get("/export")
def export_servers(
    ids: str = Query(default=""),
    admin: User = Depends(require_module("mcp")),
    db: Session = Depends(get_db),
):
    """导出 MCP 服务为 JSON（含附加请求头）。传 ids（逗号分隔）只导出选中项；不传则导出全部。"""
    q = scope_owned(db.query(McpServer), McpServer, admin)
    selected = _parse_ids_optional(ids)
    if selected:
        q = q.filter(McpServer.id.in_(selected))
    return [_mcp_dict(s) for s in q.order_by(McpServer.id).all()]


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_servers(
    file: UploadFile = File(...),
    admin: User = Depends(require_module("mcp")),
    db: Session = Depends(get_db),
):
    """从 .json 导入 MCP 服务（数组或单个对象）。名称冲突时自动追加序号导入为新条目。"""
    raw = await read_upload_limited(file, 10 * 1024 * 1024, "MCP 导入文件")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"解析失败：{exc}")
    items = data if isinstance(data, list) else [data]
    created = []
    for d in items:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        url = str(d.get("url") or "").strip()
        if not name or not url:
            continue
        transport = d.get("transport") if d.get("transport") in ("http", "sse") else "http"
        headers = d.get("headers") if isinstance(d.get("headers"), dict) else {}
        risk_policy = d.get("risk_policy") if d.get("risk_policy") in ("auto", "read_only") else "auto"
        srv = McpServer(
            name=_unique_mcp_name(db, name), description=str(d.get("description") or ""),
            transport=transport, url=url.rstrip("/") if transport == "http" else url,
            headers=json.dumps(headers, ensure_ascii=False),
            risk_policy=risk_policy,
            enabled=bool(d.get("enabled", True)), created_by=admin.id,
        )
        db.add(srv)
        db.flush()
        record_version(db, "mcp", srv.id, _mcp_version_snapshot(srv), actor_id=admin.id, change="imported")
        created.append(srv.name)
    db.commit()
    return {"imported": len(created), "names": created}


@router.post("/{server_id}/test")
async def test_server(server_id: int, admin: User = Depends(require_module("mcp")), db: Session = Depends(get_db)):
    """实际连接并列出工具，验证连通性。"""
    server = db.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP 服务不存在")
    require_owner(admin, server)
    try:
        tools = await mcp_client.list_tools(server)
    except Exception as exc:  # noqa: BLE001 - 透传失败原因
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"连接失败: {exc}")
    catalog = [
        {"name": str(tool.get("name") or "")[:128], "description": str(tool.get("description") or "")[:500]}
        for tool in tools
    ]
    catalog_hash = hashlib.sha256(json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    previous = resource_state(db, "mcp", server.id)
    changed = bool(previous.get("catalog_hash")) and previous.get("catalog_hash") != catalog_hash
    governance = update_resource_state(
        db, "mcp", server.id,
        catalog_hash=catalog_hash,
        review_required=bool(previous.get("review_required")) or changed,
        tool_count=len(catalog),
    )
    record_version(db, "mcp", server.id, _mcp_version_snapshot(server, catalog_hash), actor_id=admin.id, change="catalog_changed" if changed else "catalog_checked")
    db.commit()
    return {
        "ok": True,
        "tool_count": len(tools),
        "risk_policy": str(getattr(server, "risk_policy", "auto") or "auto"),
        "risk_summary": {
            "read": sum(
                1 for tool in tools
                if not mcp_client.tool_risk_metadata(
                    tool, risk_policy=getattr(server, "risk_policy", "auto")
                )["mutating"]
            ),
            "write_or_unknown": sum(
                1 for tool in tools
                if mcp_client.tool_risk_metadata(
                    tool, risk_policy=getattr(server, "risk_policy", "auto")
                )["mutating"]
            ),
        },
        "catalog_hash": catalog_hash,
        "catalog_changed": changed,
        "review_required": bool(governance.get("review_required")),
        "tools": [{"name": t.get("name"), "description": t.get("description", "")} for t in tools],
    }


@router.post("/{server_id}/acknowledge-catalog")
def acknowledge_catalog(
    server_id: int,
    admin: User = Depends(require_module("mcp")),
    db: Session = Depends(get_db),
):
    server = db.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP 服务不存在")
    require_owner(admin, server)
    current = resource_state(db, "mcp", server.id)
    if not current.get("catalog_hash"):
        raise HTTPException(status.HTTP_409_CONFLICT, "请先测试连接并获取工具目录")
    governance = update_resource_state(
        db, "mcp", server.id,
        review_required=False,
        acknowledged_by=admin.id,
        acknowledged_catalog_hash=current.get("catalog_hash"),
    )
    db.commit()
    return {"server_id": server.id, "catalog_hash": governance.get("catalog_hash"), "review_required": False}
