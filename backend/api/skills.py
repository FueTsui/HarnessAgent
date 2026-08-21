"""技能管理（root/admin）：Agent Skill —— 可复用、可打包的能力。

- 技能 = 说明（何时使用）+ 指令（SKILL.md 正文）+ 附带资源文件（按需读取）。
- 挂载到智能体后：指令注入系统提示词；资源文件在对话中由模型经 read_skill_resource 工具读取。
- 支持导入/导出：
    JSON 批量（GET /export, POST /import 上传 .json）
    单个技能 .zip 包（GET /{id}/export → SKILL.md + resources/；POST /import 上传 .zip / .md）
普通用户无访问权限。
"""
import io
import hashlib
import json
import mimetypes
import re
import shutil
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..config import SKILLS_DIR, TEMPLATES_DIR
from ..database import get_db
from ..models import Skill, Template, User
from ..resource_governance import public_lifecycle, record_version, versions
from ..schemas import SkillCreate, SkillOut, SkillUpdate
from ..security import can_manage, require_module, require_owner, scope_owned
from ..upload_utils import read_upload_limited

router = APIRouter(prefix="/api/v1/skills", tags=["技能（root/admin）"])

MAX_RESOURCES = 200
MAX_RESOURCE_BYTES = 200_000  # 单个资源文件上限
MAX_SKILL_IMPORT_MB = 50
MAX_SKILL_IMPORT_BYTES = MAX_SKILL_IMPORT_MB * 1024 * 1024


def _safe_package_path(value: str) -> str:
    """返回规范化的 Skill 包内相对路径，并拒绝目录穿越和绝对路径。"""
    raw = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:", raw)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"非法资源路径：{value}")
    return path.as_posix()


def _norm_resources(resources):
    """校验/规整资源元数据；二进制只保存元数据，内容保存在 Skill 资产目录。"""
    if resources is None:
        return None
    out, seen = [], set()
    for r in resources:
        if not isinstance(r, dict):
            continue
        name = _safe_package_path(r.get("package_path") or r.get("name") or "")
        if not name:
            continue
        if name in seen:
            raise ValueError(f"资源文件名重复：{name}")
        seen.add(name)
        normalized = {
            "name": name,
            "package_path": name,
            "binary": bool(r.get("binary", False)),
        }
        content = r.get("content")
        if content is not None:
            if not isinstance(content, str):
                content = str(content)
            if len(content.encode("utf-8")) > MAX_RESOURCE_BYTES:
                raise ValueError(f"资源「{name}」过大（>200KB）")
            normalized["content"] = content
        for key in ("size_bytes", "sha256", "media_type", "template_id", "inline"):
            if key in r:
                normalized[key] = r[key]
        out.append(normalized)
    if len(out) > MAX_RESOURCES:
        raise ValueError(f"资源文件过多（>{MAX_RESOURCES}）")
    return out


def _skill_asset_path(skill_id: int, package_path: str) -> Path:
    root = (SKILLS_DIR / str(int(skill_id))).resolve()
    target = (root / Path(_safe_package_path(package_path))).resolve()
    target.relative_to(root)
    return target


def _skill_version_snapshot(s: Skill) -> dict:
    try:
        resources = json.loads(s.resources or "[]")
    except json.JSONDecodeError:
        resources = []
    return {
        "name": s.name,
        "description_sha256": hashlib.sha256((s.description or "").encode("utf-8")).hexdigest(),
        "instructions_sha256": hashlib.sha256((s.instructions or "").encode("utf-8")).hexdigest(),
        "resources": [
            {
                "name": str(item.get("package_path") or item.get("name") or "")[:255],
                "sha256": str(item.get("sha256") or hashlib.sha256(str(item.get("content") or "").encode("utf-8")).hexdigest()),
                "size_bytes": int(item.get("size_bytes") or len(str(item.get("content") or "").encode("utf-8"))),
            }
            for item in resources if isinstance(item, dict)
        ],
        "enabled": bool(s.enabled),
        "is_public": bool(s.is_public),
    }


def _to_out(s: Skill, user: User | None = None, db: Session | None = None) -> SkillOut:
    try:
        resources = json.loads(s.resources) if s.resources else []
    except json.JSONDecodeError:
        resources = []
    lifecycle = public_lifecycle(db, "skill", s.id) if db is not None else {}
    return SkillOut(
        id=s.id, name=s.name, description=s.description, instructions=s.instructions,
        resources=resources if isinstance(resources, list) else [], enabled=s.enabled,
        is_public=bool(getattr(s, "is_public", False)),
        can_manage=True if user is None else can_manage(user, s),
        **lifecycle,
    )


@router.get("", response_model=list[SkillOut])
def list_skills(admin: User = Depends(require_module("skills")), db: Session = Depends(get_db)):
    q = scope_owned(db.query(Skill), Skill, admin)
    return [_to_out(s, admin, db) for s in q.order_by(Skill.id).all()]


@router.post("", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
def create_skill(body: SkillCreate, admin: User = Depends(require_module("skills")), db: Session = Depends(get_db)):
    if db.query(Skill).filter(Skill.name == body.name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "技能名称已存在")
    try:
        resources = _norm_resources(body.resources) or []
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    skill = Skill(
        name=body.name, description=body.description, instructions=body.instructions,
        resources=json.dumps(resources, ensure_ascii=False), enabled=body.enabled, created_by=admin.id,
    )
    db.add(skill)
    db.flush()
    record_version(db, "skill", skill.id, _skill_version_snapshot(skill), actor_id=admin.id, change="created")
    db.commit()
    db.refresh(skill)
    return _to_out(skill, admin, db)


@router.patch("/{skill_id}", response_model=SkillOut)
def update_skill(
    skill_id: int, body: SkillUpdate, admin: User = Depends(require_module("skills")), db: Session = Depends(get_db)
):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "技能不存在")
    require_owner(admin, skill)
    if body.name and body.name != skill.name:
        if db.query(Skill).filter(Skill.name == body.name).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "技能名称已存在")
        skill.name = body.name
    if body.description is not None:
        skill.description = body.description
    if body.instructions is not None:
        skill.instructions = body.instructions
    if body.resources is not None:
        try:
            skill.resources = json.dumps(_norm_resources(body.resources), ensure_ascii=False)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if body.enabled is not None:
        skill.enabled = body.enabled
    if body.is_public is not None:
        skill.is_public = body.is_public
    record_version(db, "skill", skill.id, _skill_version_snapshot(skill), actor_id=admin.id, change="updated")
    db.commit()
    db.refresh(skill)
    return _to_out(skill, admin, db)


@router.get("/{skill_id}/versions")
def list_skill_versions(
    skill_id: int,
    admin: User = Depends(require_module("skills")),
    db: Session = Depends(get_db),
):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "技能不存在")
    require_owner(admin, skill)
    return {"resource_type": "skill", "resource_id": skill.id, "items": versions(db, "skill", skill.id)}


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(skill_id: int, admin: User = Depends(require_module("skills")), db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "技能不存在")
    require_owner(admin, skill)
    from ..models import Agent
    for agent in db.query(Agent).all():
        try:
            ids = json.loads(agent.skill_ids or "[]")
        except json.JSONDecodeError:
            ids = []
        if isinstance(ids, list) and skill_id in ids:
            agent.skill_ids = json.dumps([value for value in ids if value != skill_id])
    db.delete(skill)
    db.commit()
    asset_root = (SKILLS_DIR / str(skill_id)).resolve()
    if asset_root.parent == SKILLS_DIR.resolve() and asset_root.is_dir():
        shutil.rmtree(asset_root, ignore_errors=True)


# ---------- 导入 / 导出 ----------

def _skill_dict(s: Skill) -> dict:
    out = _to_out(s)
    return {"name": out.name, "description": out.description,
            "instructions": out.instructions, "resources": out.resources, "enabled": out.enabled}


def _skill_markdown(d: dict) -> str:
    """技能 → SKILL.md（frontmatter: name/description；正文: instructions）。"""
    desc = (d.get("description") or "").replace("\n", " ")
    return f"---\nname: {d['name']}\ndescription: {desc}\n---\n\n{d.get('instructions', '')}\n"


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_skill_md(text: str) -> dict:
    name, description, body = "", "", text
    m = _FM_RE.match(text)
    if m:
        fm, body = m.group(1), m.group(2).lstrip("\n")
        for line in fm.splitlines():
            key, sep, val = line.partition(":")
            if not sep:
                continue
            key, val = key.strip().lower(), val.strip()
            if key == "name":
                name = val
            elif key == "description":
                description = val
    return {"name": name, "description": description, "instructions": body, "resources": []}


def _unique_name(db: Session, base: str) -> str:
    base = (base or "导入技能").strip() or "导入技能"
    name, i = base, 1
    while db.query(Skill).filter(Skill.name == name).first():
        i += 1
        name = f"{base} ({i})"
    return name


def _disposition(filename: str) -> str:
    quoted = urllib.parse.quote(filename)
    return f"attachment; filename=\"skill.zip\"; filename*=UTF-8''{quoted}"


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
def export_skills(
    ids: str = Query(default=""),
    admin: User = Depends(require_module("skills")),
    db: Session = Depends(get_db),
):
    """导出技能为 JSON（含资源）。传 ids（逗号分隔）只导出选中项；不传则导出全部。"""
    q = scope_owned(db.query(Skill), Skill, admin)
    selected = _parse_ids_optional(ids)
    if selected:
        q = q.filter(Skill.id.in_(selected))
    return [_skill_dict(s) for s in q.order_by(Skill.id).all()]


@router.get("/{skill_id}/export")
def export_skill_zip(skill_id: int, admin: User = Depends(require_module("skills")), db: Session = Depends(get_db)):
    """导出单个技能为 .zip，并原样保留包内目录和二进制资产。"""
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "技能不存在")
    require_owner(admin, skill)
    d = _skill_dict(skill)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("SKILL.md", _skill_markdown(d))
        for r in d.get("resources", []):
            package_path = _safe_package_path(
                r.get("package_path") or f"resources/{r['name']}"
            )
            if package_path.lower() == "skill.md":
                continue
            stored = _skill_asset_path(skill.id, package_path)
            if stored.is_file():
                z.writestr(package_path, stored.read_bytes())
            elif "content" in r:
                z.writestr(package_path, str(r.get("content") or ""))
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": _disposition(f"{skill.name}.zip")},
    )


def _read_zip_skill(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = [item for item in z.infolist() if not item.is_dir()]
        if len(members) > MAX_RESOURCES + 1:
            raise ValueError(f"Skill 包文件过多（>{MAX_RESOURCES + 1}）")
        if sum(max(0, item.file_size) for item in members) > MAX_SKILL_IMPORT_BYTES:
            raise ValueError(f"Skill 包解压后超过 {MAX_SKILL_IMPORT_MB}MB")
        names = [item.filename.replace("\\", "/") for item in members]
        md_name = next((n for n in names if PurePosixPath(n).name.lower() == "skill.md"), None)
        if md_name is None:
            raise ValueError("zip 包中未找到 SKILL.md")
        _safe_package_path(md_name)
        md_root = PurePosixPath(md_name).parent
        d = _parse_skill_md(z.read(md_name).decode("utf-8-sig", "strict"))
        resources = []
        files: dict[str, bytes] = {}
        for n in names:
            if n == md_name or n.endswith("/"):
                continue
            safe_name = _safe_package_path(n)
            try:
                relative = PurePosixPath(safe_name).relative_to(md_root)
            except ValueError:
                # 一个 ZIP 可能包含多个并列 Skill；单 Skill 导入不得跨包根读取文件。
                continue
            package_path = _safe_package_path(relative.as_posix())
            content_bytes = z.read(n)
            files[package_path] = content_bytes
            media_type = mimetypes.guess_type(package_path)[0] or "application/octet-stream"
            item = {
                "name": package_path,
                "package_path": package_path,
                "size_bytes": len(content_bytes),
                "sha256": hashlib.sha256(content_bytes).hexdigest(),
                "media_type": media_type,
            }
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                item["binary"] = True
                item["inline"] = False
            else:
                item["binary"] = False
                item["inline"] = len(content_bytes) <= MAX_RESOURCE_BYTES
                if item["inline"]:
                    item["content"] = content
            resources.append(item)
        d["resources"] = resources
        d["_files"] = files
        return d


def _create_imported(db: Session, admin_id, d: dict) -> Skill:
    name = _unique_name(db, str(d.get("name") or "").strip())
    resources = _norm_resources(d.get("resources")) or []
    skill = Skill(
        name=name, description=str(d.get("description") or ""),
        instructions=str(d.get("instructions") or ""),
        resources=json.dumps(resources, ensure_ascii=False),
        enabled=bool(d.get("enabled", True)), created_by=admin_id,
    )
    db.add(skill)
    return skill


def _persist_skill_assets(skill: Skill, files: dict[str, bytes]) -> None:
    for package_path, content in (files or {}).items():
        target = _skill_asset_path(skill.id, package_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def reindex_stored_skill_assets(skill: Skill) -> list[dict]:
    """为升级前已复制到磁盘的 Skill 资产重建资源元数据。"""
    root = (SKILLS_DIR / str(int(skill.id))).resolve()
    root.relative_to(SKILLS_DIR.resolve())
    resources: list[dict] = []
    if not root.is_dir():
        return resources
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        package_path = _safe_package_path(path.relative_to(root).as_posix())
        content_bytes = path.read_bytes()
        item = {
            "name": package_path,
            "package_path": package_path,
            "size_bytes": len(content_bytes),
            "sha256": hashlib.sha256(content_bytes).hexdigest(),
            "media_type": mimetypes.guess_type(package_path)[0] or "application/octet-stream",
        }
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            item.update({"binary": True, "inline": False})
        else:
            inline = len(content_bytes) <= MAX_RESOURCE_BYTES
            item.update({"binary": False, "inline": inline})
            if inline:
                item["content"] = content
        resources.append(item)
    skill.resources = json.dumps(_norm_resources(resources), ensure_ascii=False)
    return resources


def _register_artifact_template(
    db: Session, skill: Skill, *, require_source: bool = False
) -> int | None:
    """把 artifact-template.json 声明的 presentation reference 注册并绑定为模板。"""
    try:
        resources = json.loads(skill.resources or "[]")
    except json.JSONDecodeError:
        return None
    manifest_resource = next((
        item for item in resources
        if PurePosixPath(str(item.get("package_path") or item.get("name") or "")).name
        == "artifact-template.json"
    ), None)
    if not manifest_resource or "content" not in manifest_resource:
        return None
    try:
        manifest = json.loads(manifest_resource["content"])
    except (TypeError, json.JSONDecodeError):
        return None
    if manifest.get("kind") != "presentation" or not manifest.get("reference"):
        return None
    manifest_path = PurePosixPath(
        _safe_package_path(manifest_resource.get("package_path") or manifest_resource["name"])
    )
    reference = _safe_package_path(
        (manifest_path.parent / str(manifest["reference"])).as_posix()
    )
    source = _skill_asset_path(skill.id, reference)
    if source.suffix.lower() != ".pptx" or not source.is_file():
        if require_source:
            raise ValueError(f"Artifact Template 引用的 PPT 不存在：{reference}")
        return None

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    template = None
    for candidate in db.query(Template).filter(Template.ext == ".pptx").all():
        if (
            candidate.created_by != skill.created_by
            and not bool(candidate.is_public)
        ):
            continue
        candidate_path = TEMPLATES_DIR / f"{candidate.id}.pptx"
        if (
            candidate_path.is_file()
            and hashlib.sha256(candidate_path.read_bytes()).hexdigest() == source_hash
        ):
            template = candidate
            break
    if template is None:
        from .templates import _save_source, _unique_name as _unique_template_name

        template = Template(
            name=_unique_template_name(db, skill.name),
            description=skill.description,
            kind="ppt",
            ext="",
            placeholders="[]",
            enabled=True,
            is_public=bool(skill.is_public),
            created_by=skill.created_by,
        )
        db.add(template)
        db.flush()
        _save_source(template, source.name, source.read_bytes())
    else:
        template.enabled = True
        if skill.is_public:
            template.is_public = True
    manifest_resource["template_id"] = template.id
    skill.resources = json.dumps(resources, ensure_ascii=False)
    return int(template.id)


def artifact_template_ids(db: Session, skill_ids: list[int] | None) -> list[int]:
    """返回所选 Skill 声明且当前可用的 Artifact Template id，保持选择顺序。"""
    result: list[int] = []
    for skill_id in dict.fromkeys(int(value) for value in (skill_ids or [])):
        skill = db.get(Skill, skill_id)
        if skill is None or not skill.enabled:
            continue
        try:
            resources = json.loads(skill.resources or "[]")
        except json.JSONDecodeError:
            continue
        for item in resources if isinstance(resources, list) else []:
            template_id = item.get("template_id") if isinstance(item, dict) else None
            if not template_id:
                continue
            template = db.get(Template, int(template_id))
            source = TEMPLATES_DIR / f"{template.id}{template.ext}" if template else None
            if template and template.enabled and source and source.is_file():
                result.append(int(template.id))
    return list(dict.fromkeys(result))


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_skills(
    file: UploadFile = File(...),
    admin: User = Depends(require_module("skills")),
    db: Session = Depends(get_db),
):
    """导入技能：上传 .json（批量，导出格式）/ .zip（单个技能包）/ .md（单个 SKILL.md）。
    名称冲突时自动追加序号导入为新技能。"""
    raw = await read_upload_limited(file, MAX_SKILL_IMPORT_BYTES, "技能导入文件")
    fn = (file.filename or "").lower()
    items: list[dict] = []
    try:
        if fn.endswith(".zip") or raw[:2] == b"PK":
            items = [_read_zip_skill(raw)]
        elif fn.endswith(".md"):
            items = [_parse_skill_md(raw.decode("utf-8", "ignore"))]
        else:  # JSON
            data = json.loads(raw.decode("utf-8"))
            items = data if isinstance(data, list) else [data]
    except (ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"解析失败：{exc}")

    created = []
    created_ids: list[int] = []
    try:
        for d in items:
            if not isinstance(d, dict) or not str(d.get("name") or "").strip():
                continue
            skill = _create_imported(db, admin.id, d)
            db.flush()
            created_ids.append(skill.id)
            _persist_skill_assets(skill, d.get("_files") or {})
            _register_artifact_template(
                db, skill, require_source=bool(d.get("_files"))
            )
            record_version(db, "skill", skill.id, _skill_version_snapshot(skill), actor_id=admin.id, change="imported")
            created.append(skill.name)
        db.commit()
    except (ValueError, HTTPException) as exc:
        db.rollback()
        for skill_id in created_ids:
            asset_root = (SKILLS_DIR / str(skill_id)).resolve()
            if asset_root.parent == SKILLS_DIR.resolve() and asset_root.is_dir():
                shutil.rmtree(asset_root)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {"imported": len(created), "names": created}
