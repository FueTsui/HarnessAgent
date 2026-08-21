"""对话附件的持久身份、Thread 延续解析与每轮工作区物化。"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import uuid
from pathlib import Path

from .config import UPLOAD_DIR
from .models import Attachment, Item, Job, Turn


MAX_THREAD_ATTACHMENTS = 10
MAX_EXTRACTED_CHARS = 20_000


def display_name(path: Path) -> str:
    sidecar = path.with_suffix(".name")
    try:
        if sidecar.is_file():
            return sidecar.read_text(encoding="utf-8").strip() or path.name
    except OSError:
        pass
    return path.name


def _extract(path: Path) -> str:
    from .capabilities import documents

    extractors = {
        ".pdf": documents._extract_pdf,
        ".docx": documents._extract_docx,
        ".xlsx": documents._extract_xlsx,
    }
    try:
        text = (
            extractors[path.suffix.lower()](path)
            if path.suffix.lower() in extractors
            else path.read_text(encoding="utf-8", errors="ignore")
        )
    except Exception as exc:  # 单附件失败仍持久化原文件，运行时可以明确展示原因
        return f"[读取失败：{exc}]"
    value = (text or "").strip()
    if len(value) > MAX_EXTRACTED_CHARS:
        value = value[:MAX_EXTRACTED_CHARS] + (
            f"\n…（内容过长，已截断，仅保留前 {MAX_EXTRACTED_CHARS} 字）"
        )
    return value


def pending_record(path: Path, kind: str) -> dict:
    """为刚落盘的文件生成可在 jobs.enqueue 事务中持久化的记录。"""
    resolved = Path(path).resolve()
    resolved.relative_to(UPLOAD_DIR.resolve())
    if resolved.parent != UPLOAD_DIR.resolve() or not resolved.is_file():
        raise ValueError("附件不在受控上传目录")
    name = display_name(resolved)
    return {
        "id": uuid.uuid4().hex,
        "storage_name": resolved.name,
        "original_name": Path(name).name[:255],
        "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "kind": "image" if kind == "image" else "document",
        "size_bytes": resolved.stat().st_size,
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "extracted_text": "" if kind == "image" else _extract(resolved),
    }


def public_record(value: Attachment | dict, *, inherited: bool = False) -> dict:
    get = value.get if isinstance(value, dict) else lambda key, default=None: getattr(value, key, default)
    attachment_id = str(get("id") or "")
    return {
        "id": attachment_id,
        "name": str(get("original_name") or "附件"),
        "kind": str(get("kind") or "document"),
        "media_type": str(get("media_type") or "application/octet-stream"),
        "size_bytes": int(get("size_bytes") or 0),
        "sha256": str(get("sha256") or ""),
        "inherited": bool(inherited),
        "source_turn_id": str(get("turn_id") or "")[:32],
        "url": f"/api/v1/chat/attachments/{attachment_id}" if attachment_id else "",
    }


def persist_pending_records(
    db, *, owner_id: int, thread_id: str, turn_id: str, records: list[dict]
) -> list[Attachment]:
    rows: list[Attachment] = []
    for record in records or []:
        attachment_id = str(record.get("id") or "")[:32]
        storage_name = Path(str(record.get("storage_name") or "")).name
        if not attachment_id or not storage_name:
            continue
        path = (UPLOAD_DIR / storage_name).resolve()
        if path.parent != UPLOAD_DIR.resolve() or not path.is_file():
            continue
        existing = db.get(Attachment, attachment_id)
        if existing is not None:
            rows.append(existing)
            continue
        row = Attachment(
            id=attachment_id,
            owner_id=owner_id,
            thread_id=thread_id,
            turn_id=turn_id,
            storage_name=storage_name,
            original_name=Path(str(record.get("original_name") or storage_name)).name[:255],
            media_type=str(record.get("media_type") or "application/octet-stream")[:128],
            kind="image" if record.get("kind") == "image" else "document",
            size_bytes=int(record.get("size_bytes") or path.stat().st_size),
            sha256=str(record.get("sha256") or "")[:64],
            extracted_text=str(record.get("extracted_text") or ""),
        )
        db.add(row)
        rows.append(row)
    return rows


def path_for(value: Attachment | dict) -> Path | None:
    storage_name = (
        value.get("storage_name") if isinstance(value, dict)
        else value.storage_name
    )
    path = (UPLOAD_DIR / Path(str(storage_name or "")).name).resolve()
    return path if path.parent == UPLOAD_DIR.resolve() and path.is_file() else None


def _legacy_context(db, *, owner_id: int, thread_id: str) -> dict:
    """升级前 Turn 没有 Attachment 行时，从加密 Job 快照尽力恢复最近附件。"""
    turns = (
        db.query(Turn)
        .filter(Turn.thread_id == thread_id, Turn.owner_id == owner_id)
        .order_by(Turn.sequence.desc())
        .limit(20)
        .all()
    )
    for turn in turns:
        job = db.get(Job, turn.id)
        if job is None:
            continue
        try:
            payload = json.loads(job.payload or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        images = [Path(value) for value in payload.get("attachment_images") or []]
        docs = [Path(value) for value in payload.get("attachment_docs") or []]
        images = [path for path in images if path.is_file()]
        docs = [path for path in docs if path.is_file()]
        if images or docs:
            metadata = [
                {
                    "id": "",
                    "name": display_name(path),
                    "kind": kind,
                    "media_type": mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                    "size_bytes": path.stat().st_size,
                    "sha256": "",
                    "inherited": True,
                    "source_turn_id": turn.id,
                    "url": "",
                }
                for kind, paths in (("image", images), ("document", docs))
                for path in paths
            ]
            return {
                "images": images,
                "documents": docs,
                "attachment_ids": [],
                "metadata": metadata,
                "continuation_of_turn_id": turn.id,
                "legacy": True,
            }
    return {
        "images": [], "documents": [], "attachment_ids": [], "metadata": [],
        "continuation_of_turn_id": None, "legacy": False,
    }


def thread_context(db, *, owner_id: int, thread_id: str) -> dict:
    rows = (
        db.query(Attachment)
        .filter(Attachment.thread_id == thread_id, Attachment.owner_id == owner_id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
        .limit(MAX_THREAD_ATTACHMENTS)
        .all()
    )
    rows.reverse()
    if not rows:
        return _legacy_context(db, owner_id=owner_id, thread_id=thread_id)
    images: list[Path] = []
    documents: list[Path] = []
    usable: list[Attachment] = []
    for row in rows:
        path = path_for(row)
        if path is None:
            continue
        (images if row.kind == "image" else documents).append(path)
        usable.append(row)
    return {
        "images": images,
        "documents": documents,
        "attachment_ids": [row.id for row in usable],
        "metadata": [public_record(row, inherited=True) for row in usable],
        "continuation_of_turn_id": usable[-1].turn_id if usable else None,
        "legacy": False,
    }


def latest_job_payload(db, *, owner_id: int, thread_id: str) -> dict:
    turns = (
        db.query(Turn)
        .filter(Turn.thread_id == thread_id, Turn.owner_id == owner_id)
        .order_by(Turn.sequence.desc())
        .limit(20)
        .all()
    )
    fallback: dict = {}
    for turn in turns:
        job = db.get(Job, turn.id)
        try:
            value = json.loads(job.payload or "{}") if job else {}
        except (json.JSONDecodeError, TypeError):
            value = {}
        if not isinstance(value, dict):
            continue
        if not fallback:
            fallback = value
        if any(value.get(key) for key in (
            "attachment_ids", "attachment_images", "attachment_docs",
            "skill_ids", "mcp_ids", "template_ids", "dataset_ids",
            "invoked_agent_ids",
        )):
            return value
    return fallback


def backfill_legacy(db) -> int:
    """把升级前加密 Job 快照中的仍存附件晋升为 Thread 任务资产。"""
    created = 0
    jobs = db.query(Job).filter(Job.kind == "chat").order_by(Job.created_at).all()
    for job in jobs:
        turn = db.get(Turn, job.id)
        if turn is None or job.owner_id is None:
            continue
        try:
            payload = json.loads(job.payload or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        candidates = [
            (kind, Path(raw))
            for kind, values in (
                ("image", payload.get("attachment_images") or []),
                ("document", payload.get("attachment_docs") or []),
            )
            for raw in values
        ]
        records: list[dict] = []
        for kind, path in candidates:
            try:
                resolved = path.resolve()
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            existing = db.query(Attachment).filter(
                Attachment.thread_id == turn.thread_id,
                Attachment.owner_id == job.owner_id,
                Attachment.storage_name == resolved.name,
            ).first()
            if existing is not None:
                continue
            records.append(pending_record(resolved, kind))
        rows = persist_pending_records(
            db,
            owner_id=job.owner_id,
            thread_id=turn.thread_id,
            turn_id=turn.id,
            records=records,
        )
        if not rows:
            continue
        db.flush()
        context = [public_record(row) for row in rows]
        payload["attachment_ids"] = [row.id for row in rows]
        payload["attachment_context"] = context
        payload.setdefault("attachment_records", [])
        payload.setdefault("attachments_inherited", False)
        job.payload = json.dumps(payload, ensure_ascii=False)
        message = (
            db.query(Item)
            .filter_by(turn_id=turn.id, kind="message", role="user")
            .order_by(Item.sequence)
            .first()
        )
        if message is not None:
            try:
                message_payload = json.loads(message.payload or "{}")
            except (json.JSONDecodeError, TypeError):
                message_payload = {}
            message_payload["attachments"] = context
            message.payload = json.dumps(message_payload, ensure_ascii=False)
        created += len(rows)
    return created


def _safe_workspace_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(name).name).strip(" .")
    return (cleaned or fallback)[:180]


def materialize(paths: list[Path], root: Path) -> dict[str, str]:
    """把授权附件复制到隔离的 run/inputs，返回原路径到相对工作区路径映射。"""
    inputs = (Path(root) / "inputs").resolve()
    inputs.relative_to(Path(root).resolve())
    inputs.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for index, raw in enumerate(paths or [], 1):
        source = Path(raw).resolve()
        if not source.is_file():
            continue
        base = _safe_workspace_name(display_name(source), f"attachment_{index}{source.suffix}")
        candidate = base
        counter = 2
        while candidate.lower() in used:
            stem, suffix = Path(base).stem, Path(base).suffix
            candidate = f"{stem}_{counter}{suffix}"
            counter += 1
        used.add(candidate.lower())
        target = (inputs / candidate).resolve()
        target.relative_to(inputs)
        shutil.copy2(source, target)
        mapping[str(source)] = target.relative_to(Path(root).resolve()).as_posix()
    return mapping
