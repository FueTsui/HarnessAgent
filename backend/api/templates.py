"""模板管理：可复用的 Word / Markdown / PPT / Excel 报告模板。

模板源文件内用 {{占位符}} 标记；渲染时模型按上下文产出取值，系统保留排版地填回。
源文件落盘于 data/templates/{id}{ext}。支持导入/导出（.zip：template.json + 源文件）、
公开开关、按权限作用域（root 全权，获授权用户限本人创建 + 公开）。
可在流程中以 template_render 步骤引用，或在对话中经 @ 选中后渲染为可下载文件。
"""
import io
import json
import urllib.parse
import zipfile
from pathlib import Path

from fastapi import (
    APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, status,
)
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..config import TEMPLATES_DIR
from ..database import get_db
from ..models import Template, User
from ..capabilities import templates as templates_render
from ..schemas import TemplateOut, TemplateRenderBody, TemplateUpdate
from ..security import (
    can_manage, get_current_user, is_root, require_module, require_owner, scope_owned,
)
from ..upload_utils import read_upload_limited

router = APIRouter(prefix="/api/v1/templates", tags=["模板"])

KIND_EXT = templates_render.KIND_EXT
EXT_KIND = templates_render.EXT_KIND
MAX_TEMPLATE_MB = 50
MAX_TEMPLATE_BYTES = MAX_TEMPLATE_MB * 1024 * 1024  # 单个模板源文件上限 50MB


# ---------- 存储辅助 ----------

def _file_path(tpl: Template) -> Path | None:
    if not tpl.ext:
        return None
    return TEMPLATES_DIR / f"{tpl.id}{tpl.ext}"


def _placeholders(tpl: Template) -> list[str]:
    try:
        data = json.loads(tpl.placeholders or "[]")
        return [str(x) for x in data] if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _to_out(tpl: Template, user: User | None = None) -> TemplateOut:
    path = _file_path(tpl)
    return TemplateOut(
        id=tpl.id, name=tpl.name, description=tpl.description, kind=tpl.kind,
        ext=tpl.ext or "", placeholders=_placeholders(tpl),
        has_file=bool(path and path.exists()), enabled=tpl.enabled,
        is_public=bool(getattr(tpl, "is_public", False)),
        can_manage=True if user is None else can_manage(user, tpl),
    )


def _save_source(tpl: Template, filename: str, content: bytes) -> None:
    """落盘源文件并按内容重解析占位符；按文件后缀校正 kind/ext。"""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in EXT_KIND:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "模板仅支持 .docx / .md / .txt / .pptx / .xlsx")
    if len(content) > MAX_TEMPLATE_BYTES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"模板源文件过大（最大 {MAX_TEMPLATE_MB}MB）",
        )
    kind = EXT_KIND[suffix]
    # 删除旧后缀的残留文件（替换为不同类型时）
    old = _file_path(tpl)
    if old is not None and old.exists() and old.suffix.lower() != suffix:
        old.unlink(missing_ok=True)
    tpl.kind, tpl.ext = kind, (".md" if suffix == ".txt" else suffix)
    path = TEMPLATES_DIR / f"{tpl.id}{tpl.ext}"
    path.write_bytes(content)
    tpl.placeholders = json.dumps(
        templates_render.extract_placeholders(path, kind), ensure_ascii=False
    )


def _unique_name(db: Session, base: str) -> str:
    base = (base or "导入模板").strip() or "导入模板"
    name, i = base, 1
    while db.query(Template).filter(Template.name == name).first():
        i += 1
        name = f"{base} ({i})"
    return name


def _disposition(filename: str) -> str:
    quoted = urllib.parse.quote(filename)
    return f"attachment; filename=\"template\"; filename*=UTF-8''{quoted}"


# ---------- 查询 ----------

@router.get("", response_model=list[TemplateOut])
def list_templates(admin: User = Depends(require_module("templates")), db: Session = Depends(get_db)):
    q = scope_owned(db.query(Template), Template, admin)
    return [_to_out(t, admin) for t in q.order_by(Template.id).all()]


@router.get("/enabled", response_model=list[TemplateOut])
def list_enabled_templates(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """对话 @ 菜单可选模板：root 见全部已启用；其他用户仅见「已开放(public)」的。"""
    q = db.query(Template).filter(Template.enabled.is_(True))
    if not is_root(user):
        q = q.filter(Template.is_public.is_(True))
    return [_to_out(t, user) for t in q.order_by(Template.id).all()]


# ---------- 创建 / 修改 ----------

@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    name: str = Form(...),
    description: str = Form(""),
    kind: str = Form("word"),
    is_public: bool = Form(False),
    file: UploadFile | None = File(None),
    admin: User = Depends(require_module("templates")),
    db: Session = Depends(get_db),
):
    name = (name or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请填写模板名称")
    if db.query(Template).filter(Template.name == name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "模板名称已存在")
    if kind not in KIND_EXT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "模板类型须为 word / md / ppt / excel")
    tpl = Template(
        name=name, description=description, kind=kind, ext="", placeholders="[]",
        enabled=True, is_public=bool(is_public), created_by=admin.id,
    )
    db.add(tpl)
    db.flush()  # 取得 id 供源文件命名
    if file is not None and file.filename:
        _save_source(
            tpl, file.filename,
            await read_upload_limited(file, MAX_TEMPLATE_BYTES, "模板文件"),
        )
    db.commit()
    db.refresh(tpl)
    return _to_out(tpl, admin)


@router.patch("/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int, body: TemplateUpdate,
    admin: User = Depends(require_module("templates")), db: Session = Depends(get_db),
):
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    require_owner(admin, tpl)
    if body.name and body.name != tpl.name:
        if db.query(Template).filter(Template.name == body.name).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "模板名称已存在")
        tpl.name = body.name
    if body.description is not None:
        tpl.description = body.description
    if body.enabled is not None:
        tpl.enabled = body.enabled
    if body.is_public is not None:
        tpl.is_public = body.is_public
    db.commit()
    db.refresh(tpl)
    return _to_out(tpl, admin)


@router.post("/{template_id}/file", response_model=TemplateOut)
async def upload_template_file(
    template_id: int, file: UploadFile = File(...),
    admin: User = Depends(require_module("templates")), db: Session = Depends(get_db),
):
    """上传/替换模板源文件，并重新解析占位符。"""
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    require_owner(admin, tpl)
    _save_source(
        tpl, file.filename or "template",
        await read_upload_limited(file, MAX_TEMPLATE_BYTES, "模板文件"),
    )
    db.commit()
    db.refresh(tpl)
    return _to_out(tpl, admin)


@router.patch("/{template_id}/visibility", response_model=TemplateOut)
def set_template_visibility(
    template_id: int, body: dict = Body(...),
    admin: User = Depends(require_module("templates")), db: Session = Depends(get_db),
):
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    require_owner(admin, tpl)
    tpl.is_public = bool(body.get("is_public"))
    db.commit()
    db.refresh(tpl)
    return _to_out(tpl, admin)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: int, admin: User = Depends(require_module("templates")), db: Session = Depends(get_db)
):
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    require_owner(admin, tpl)
    path = _file_path(tpl)
    if path is not None:
        path.unlink(missing_ok=True)
    db.delete(tpl)
    db.commit()


# ---------- 下载 / 导出 / 导入 ----------

@router.get("/{template_id}/download")
def download_template(
    template_id: int, admin: User = Depends(require_module("templates")), db: Session = Depends(get_db)
):
    """下载模板源文件。"""
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    require_owner(admin, tpl)
    path = _file_path(tpl)
    if path is None or not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该模板尚未上传源文件")
    return FileResponse(path, filename=f"{tpl.name}{tpl.ext}")


@router.get("/{template_id}/export")
def export_template(
    template_id: int, admin: User = Depends(require_module("templates")), db: Session = Depends(get_db)
):
    """导出模板为 .zip（template.json 元数据 + 源文件）。"""
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    require_owner(admin, tpl)
    meta = {
        "name": tpl.name, "description": tpl.description, "kind": tpl.kind,
        "ext": tpl.ext or "", "placeholders": _placeholders(tpl), "enabled": tpl.enabled,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("template.json", json.dumps(meta, ensure_ascii=False, indent=2))
        path = _file_path(tpl)
        if path is not None and path.exists():
            z.writestr(f"source{tpl.ext}", path.read_bytes())
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": _disposition(f"{tpl.name}.zip")},
    )


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_template(
    file: UploadFile = File(...),
    admin: User = Depends(require_module("templates")), db: Session = Depends(get_db),
):
    """导入模板：上传 .zip（template.json + 源文件）或直接上传单个模板源文件。
    名称冲突时自动追加序号导入为新模板。"""
    raw = await read_upload_limited(file, MAX_TEMPLATE_BYTES, "模板导入文件")
    fn = (file.filename or "").lower()
    meta: dict = {}
    source_name, source_bytes = "", b""
    if fn.endswith(".zip") or raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = z.namelist()
                if "template.json" in names:
                    meta = json.loads(z.read("template.json").decode("utf-8", "ignore"))
                    meta = meta if isinstance(meta, dict) else {}
                src = next((n for n in names if n.split("/")[-1].startswith("source") and not n.endswith("/")), None)
                if src is None:
                    src = next((n for n in names if Path(n).suffix.lower() in EXT_KIND), None)
                if src is not None:
                    source_name, source_bytes = Path(src).name, z.read(src)
        except (ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"解析失败：{exc}")
    else:
        if Path(fn).suffix.lower() not in EXT_KIND:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "仅支持 .zip 包或 .docx/.md/.txt/.pptx/.xlsx 源文件")
        source_name, source_bytes = file.filename or "template", raw

    base = str(meta.get("name") or Path(source_name).stem or "导入模板").strip()
    name = _unique_name(db, base)
    tpl = Template(
        name=name, description=str(meta.get("description") or ""),
        kind=str(meta.get("kind") or "word"), ext="", placeholders="[]",
        enabled=bool(meta.get("enabled", True)), is_public=False, created_by=admin.id,
    )
    db.add(tpl)
    db.flush()
    if source_bytes:
        _save_source(tpl, source_name, source_bytes)
    db.commit()
    db.refresh(tpl)
    return _to_out(tpl, admin)


# ---------- 渲染 ----------

@router.post("/{template_id}/render")
def render_template(
    template_id: int, body: TemplateRenderBody,
    admin: User = Depends(require_module("templates")), db: Session = Depends(get_db),
):
    """用给定取值渲染模板，返回生成的文件下载。"""
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    require_owner(admin, tpl)
    path = _file_path(tpl)
    if path is None or not path.exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该模板尚未上传源文件")
    try:
        out = templates_render.render(path, tpl.kind, body.values or {}, title=tpl.name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return FileResponse(out, filename=out.name)
