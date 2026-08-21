"""系统设置（站名 / Logo，root）与操作日志（root 查看）。

- 系统设置：键值存于 app_settings 表；Logo 文件落盘 data/branding/logo<ext>。
  /public 端点免鉴权，供登录页与各页渲染品牌（站名、Logo）。
- 操作日志：变更类请求由 main.py 的审计中间件写入 audit_logs，此处提供 root 查询/清空。
"""
import re
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import BRANDING_DIR
from ..database import get_db
from ..models import AppSetting, AuditLog, User, iso_utc
from ..security import require_root
from ..upload_utils import read_upload_limited

router = APIRouter(prefix="/api/v1/settings", tags=["系统设置"])
audit_router = APIRouter(prefix="/api/v1/audit-logs", tags=["操作日志（root）"])

DEFAULT_SITE_NAME = "智能体平台"
SITE_NAME_KEY = "site_name"
LOGO_EXT_KEY = "logo_ext"
# 对外访问基址（含协议/域名/端口），用于拼接 API 与第三方接口/Webhook 的可直接调用地址。
# 例：https://agent.example.com 或 http://1.2.3.4:8000。留空则回退到浏览器当前地址。
PUBLIC_BASE_URL_KEY = "public_base_url"
LOGO_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".webp"}
LOGO_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml", ".webp": "image/webp", ".ico": "image/x-icon",
}
MAX_LOGO_BYTES = 2 * 1024 * 1024  # Logo 上限 2MB
# 后台未上传 logo.<ext> 时，从 data/branding 中发现随部署提供的品牌图。
# 这样直接替换 branding 目录即可生效，同时仍让后台上传的 Logo 拥有最高优先级。
DEFAULT_LOGO_FILENAMES = (
    "logo.svg",
    "logo.png",
    "logo.webp",
    "logo.jpg",
    "logo.jpeg",
    "favicon.svg",
    "favicon-96x96.png",
    "apple-touch-icon.png",
)


# ---------- 键值存取 ----------

def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def get_public_base_url(db: Session) -> str:
    """已配置的对外访问基址（无尾斜杠）；未配置返回空串。"""
    return get_setting(db, PUBLIC_BASE_URL_KEY, "")


def _normalize_base_url(value: str) -> str:
    """校验/规整对外访问基址：空串=清除；否则须为 http(s)://主机[:端口][/前缀]，去掉尾部斜杠。"""
    v = (value or "").strip().rstrip("/")
    if not v:
        return ""
    if len(v) > 256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "对外访问地址过长（≤256 字）")
    if not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", v):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "对外访问地址需形如 https://example.com 或 http://1.2.3.4:8000",
        )
    return v


def _logo_path(ext: str) -> Path:
    return BRANDING_DIR / f"logo{ext}"


def _current_logo(db: Session) -> Path | None:
    ext = get_setting(db, LOGO_EXT_KEY, "")
    if ext in LOGO_SUFFIXES:
        path = _logo_path(ext)
        if path.is_file():
            return path
    for filename in DEFAULT_LOGO_FILENAMES:
        path = BRANDING_DIR / filename
        if path.is_file():
            return path
    return None


def _has_custom_logo(db: Session) -> bool:
    """数据库登记且文件仍存在时才视为后台上传的 Logo。"""
    ext = get_setting(db, LOGO_EXT_KEY, "")
    return ext in LOGO_SUFFIXES and _logo_path(ext).is_file()


def _logo_url(db: Session) -> str:
    path = _current_logo(db)
    if path is None:
        return ""
    return f"/api/v1/settings/logo?v={int(path.stat().st_mtime)}"


# ---------- 公共（免鉴权）品牌读取 ----------

@router.get("/public")
def public_settings(db: Session = Depends(get_db)):
    """品牌信息：站名 + Logo URL。供登录页/各页渲染，无需登录。"""
    return {
        "site_name": get_setting(db, SITE_NAME_KEY, "") or DEFAULT_SITE_NAME,
        "logo_url": _logo_url(db),
        "logo_is_custom": _has_custom_logo(db),
    }


@router.get("/logo")
def get_logo(request: Request, db: Session = Depends(get_db)):
    """返回当前 Logo 文件（免鉴权，供 <img> 直接引用）。"""
    path = _current_logo(db)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "未设置 Logo")
    cache_control = (
        "public, max-age=31536000, immutable"
        if request.query_params.get("v")
        else "no-cache"
    )
    return FileResponse(
        path,
        media_type=LOGO_MIME.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": cache_control},
    )


# ---------- root 管理 ----------

def _settings_payload(db: Session) -> dict:
    return {
        "site_name": get_setting(db, SITE_NAME_KEY, "") or DEFAULT_SITE_NAME,
        "logo_url": _logo_url(db),
        "logo_is_custom": _has_custom_logo(db),
        "public_base_url": get_public_base_url(db),
    }


@router.get("")
def get_settings(_: User = Depends(require_root), db: Session = Depends(get_db)):
    return _settings_payload(db)


@router.put("")
def update_settings(body: dict = Body(...), _: User = Depends(require_root), db: Session = Depends(get_db)):
    """部分更新：仅更新请求中出现的字段（site_name / public_base_url）。"""
    if "site_name" in body:
        name = str(body.get("site_name") or "").strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "系统名称不能为空")
        if len(name) > 64:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "系统名称过长（≤64 字）")
        set_setting(db, SITE_NAME_KEY, name)
    if "public_base_url" in body:
        set_setting(db, PUBLIC_BASE_URL_KEY, _normalize_base_url(str(body.get("public_base_url") or "")))
    db.commit()
    return _settings_payload(db)


@router.post("/logo")
async def upload_logo(
    file: UploadFile = File(...), _: User = Depends(require_root), db: Session = Depends(get_db)
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in LOGO_SUFFIXES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Logo 仅支持 .png / .jpg / .jpeg / .svg / .webp")
    content = await read_upload_limited(file, MAX_LOGO_BYTES, "Logo")
    # 删除旧后缀的残留文件（更换格式时）
    old_ext = get_setting(db, LOGO_EXT_KEY, "")
    if old_ext and old_ext != suffix:
        _logo_path(old_ext).unlink(missing_ok=True)
    _logo_path(suffix).write_bytes(content)
    set_setting(db, LOGO_EXT_KEY, suffix)
    db.commit()
    return {"logo_url": _logo_url(db)}


@router.delete("/logo", status_code=status.HTTP_204_NO_CONTENT)
def delete_logo(_: User = Depends(require_root), db: Session = Depends(get_db)):
    ext = get_setting(db, LOGO_EXT_KEY, "")
    if ext in LOGO_SUFFIXES:
        _logo_path(ext).unlink(missing_ok=True)
    if ext:
        set_setting(db, LOGO_EXT_KEY, "")
        db.commit()


# ---------- 操作日志（root） ----------

@audit_router.get("")
def list_audit_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    method: str = Query(""),
    username: str = Query(""),
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog)
    if method.strip():
        q = q.filter(AuditLog.method == method.strip().upper())
    if username.strip():
        q = q.filter(AuditLog.username == username.strip())
    total = q.count()
    rows = q.order_by(AuditLog.id.desc()).offset(offset).limit(limit).all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [
            {
                "id": r.id, "user_id": r.user_id, "username": r.username, "role": r.role,
                "method": r.method, "path": r.path, "status_code": r.status_code,
                "ip": r.ip, "created_at": iso_utc(r.created_at),
            }
            for r in rows
        ],
    }


@audit_router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_audit_logs(_: User = Depends(require_root), db: Session = Depends(get_db)):
    db.query(AuditLog).delete()
    db.commit()
