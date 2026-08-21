"""本地知识库能力：文档管理、索引与检索。

原 Dify 知识库（dataset_ids 为实例加密 ID）无法导出，这里改为本地文件夹方案：
  data/knowledge/green/  绿色低碳、零碳园区政策与标准文档
  data/knowledge/vpp/    VPP、售电、投运测算资料文档
  data/knowledge/<key>/  自定义知识库

检索算法：BM25 关键词相关度 + 本地概念扩展/字符子词近似语义，取 top_k 段落。
知识库为空时返回空串，提示词中已有「知识库未命中需复核政策」的兜底规则。
"""
from collections import Counter
import hashlib
import json
import math
import os
import re
import threading
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

from ..config import KNOWLEDGE_DIR

TOP_K = 5
SCORE_THRESHOLD = 0.08
CHUNK_SIZE = 600
DATASETS_META = KNOWLEDGE_DIR / "_datasets.json"
# root 删除内置知识库后在此登记，避免 _load_datasets 自动重建
DELETED_BUILTINS_META = KNOWLEDGE_DIR / "_deleted_builtins.json"
META_LOCK_FILE = KNOWLEDGE_DIR / ".datasets.lock"
_THREAD_META_LOCK = threading.RLock()
DEFAULT_DATASETS = {
    "green": {"name": "绿色低碳知识库", "created_by": None, "builtin": True, "is_public": False},
    "vpp": {"name": "VPP与投运知识库", "created_by": None, "builtin": True, "is_public": False},
}
TEXT_SUFFIXES = {".md", ".txt"}
UPLOAD_SUFFIXES = TEXT_SUFFIXES | {".docx"}


def _is_root(user) -> bool:
    return getattr(user, "role", "") == "root"


def _can_manage_dataset(user, meta: dict) -> bool:
    """Only the recorded creator may mutate a knowledge base.

    Knowledge bases are collaborative resources, so root can discover and use
    every base, but must not silently take ownership of content created by
    another user.  This deliberately differs from the generic control-plane
    resource policy where root is normally allowed to manage all records.
    """
    creator_id = meta.get("created_by")
    user_id = getattr(user, "id", None)
    return creator_id is not None and user_id is not None and creator_id == user_id


def _can_see_dataset(user, meta: dict) -> bool:
    # root retains platform-wide read/use access, without receiving edit rights.
    return _is_root(user) or _can_manage_dataset(user, meta) or bool(meta.get("is_public"))


def _normalize_meta(raw) -> dict:
    if isinstance(raw, str):
        return {"name": raw, "created_by": None, "builtin": False}
    if isinstance(raw, dict):
        return {
            "name": str(raw.get("name") or "").strip(),
            "created_by": raw.get("created_by"),
            "builtin": bool(raw.get("builtin", False)),
            "is_public": bool(raw.get("is_public", False)),
        }
    return {"name": "", "created_by": None, "builtin": False, "is_public": False}


@contextmanager
def _locked_meta():
    """跨线程、跨进程串行化知识库元数据的读改写。"""
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    with _THREAD_META_LOCK:
        with META_LOCK_FILE.open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _atomic_json_write(path: Path, value) -> None:
    """同目录临时文件落盘并原子替换，避免中途退出留下半截 JSON。"""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _load_datasets_unlocked() -> dict[str, dict]:
    """返回 key -> 元数据，并确保内置知识库始终存在。"""
    data: dict[str, dict] = {}
    if DATASETS_META.exists():
        try:
            raw = json.loads(DATASETS_META.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {
                    str(k): _normalize_meta(v)
                    for k, v in raw.items()
                    if k and _normalize_meta(v).get("name")
                }
        except (OSError, json.JSONDecodeError):
            data = {}
    changed = False
    deleted_builtins = _load_deleted_builtins_unlocked()
    for key, meta in DEFAULT_DATASETS.items():
        if key in deleted_builtins:
            continue  # root 已删除该内置知识库，不再重建
        if key not in data:
            data[key] = dict(meta)
            changed = True
        elif data[key].get("builtin") is not True:
            data[key]["builtin"] = True
            changed = True
    for key in data:
        (KNOWLEDGE_DIR / key).mkdir(parents=True, exist_ok=True)
    if changed or not DATASETS_META.exists():
        _save_datasets_unlocked(data)
    return data


def _load_datasets() -> dict[str, dict]:
    with _locked_meta():
        return _load_datasets_unlocked()


def _save_datasets_unlocked(data: dict[str, dict]) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(DATASETS_META, data)


def _save_datasets(data: dict[str, dict]) -> None:
    with _locked_meta():
        _save_datasets_unlocked(data)


def _load_deleted_builtins_unlocked() -> set[str]:
    """读取已被 root 删除的内置知识库 key 列表。"""
    if DELETED_BUILTINS_META.exists():
        try:
            raw = json.loads(DELETED_BUILTINS_META.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return {str(k) for k in raw if k}
        except (OSError, json.JSONDecodeError):
            pass
    return set()


def _load_deleted_builtins() -> set[str]:
    with _locked_meta():
        return _load_deleted_builtins_unlocked()


def _save_deleted_builtins_unlocked(keys: set[str]) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(DELETED_BUILTINS_META, sorted(keys))


def _save_deleted_builtins(keys: set[str]) -> None:
    with _locked_meta():
        _save_deleted_builtins_unlocked(keys)


def _dataset_exists(dataset: str) -> bool:
    return dataset in _load_datasets()


def _dataset_meta(dataset: str) -> dict | None:
    return _load_datasets().get(dataset)


def _dataset_key(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    if not base:
        base = "kb-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    key = base[:48]
    i = 2
    while key in existing:
        suffix = f"-{i}"
        key = (base[:48 - len(suffix)] + suffix).strip("-")
        i += 1
    return key


def _token_list(text: str) -> list[str]:
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9]+", text)
    han = re.findall(r"[\u4e00-\u9fff]", text)
    if len(han) == 1:
        words.append(han[0])
    else:
        words.extend(han[index] + han[index + 1] for index in range(len(han) - 1))
    return words


def _tokens(text: str) -> set[str]:
    return set(_token_list(text))


# 不依赖外部模型的保守概念扩展。只用于提高候选召回，最终仍以来源正文为证据；
# 词组覆盖常见中英文同义表达，避免把任意“相似”文本当作精确事实。
_SEMANTIC_GROUPS = (
    {"成本", "费用", "开支", "造价", "cost", "expense", "price"},
    {"采购", "购买", "购置", "采买", "buy", "purchase", "procurement"},
    {"风险", "隐患", "危险", "risk", "hazard", "danger"},
    {"减排", "降碳", "低碳", "脱碳", "emission", "decarbonization"},
    {"收益", "回报", "利润", "营收", "income", "return", "profit", "revenue"},
    {"计划", "规划", "方案", "路线", "plan", "roadmap", "scheme"},
    {"规则", "制度", "规范", "政策", "rule", "policy", "standard"},
)
_SEMANTIC_CANONICAL = {
    token: sorted(group)[0]
    for group in _SEMANTIC_GROUPS
    for token in group
}


def _semantic_features(text: str) -> set[str]:
    value = (text or "").lower()
    raw = _tokens(value)
    features = {f"term:{term}" for term in raw}
    for token, canonical in _SEMANTIC_CANONICAL.items():
        if token in value or token in raw:
            features.add(f"concept:{canonical}")
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value)
    features.update(
        f"sub:{compact[index:index + 3]}"
        for index in range(max(0, len(compact) - 2))
    )
    return features


def _chunks(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    merged, buffer = [], ""
    for para in paragraphs:
        if len(buffer) + len(para) <= CHUNK_SIZE:
            buffer = f"{buffer}\n{para}".strip()
        else:
            if buffer:
                merged.append(buffer)
            buffer = para[:CHUNK_SIZE * 2]
    if buffer:
        merged.append(buffer)
    return merged


# 文件分块 + 分词结果缓存（按 mtime 失效）：避免每次检索都重读、重新分词整个语料。
# 知识库文档不常变动，命中缓存时单次检索从 O(语料分词) 降为 O(语料比对)。
_chunk_cache: "dict[str, tuple[int, int, list[tuple[str, Counter, set[str]]]]]" = {}


def _file_chunks_with_tokens(path: Path) -> list[tuple[str, Counter, set[str]]]:
    """返回段落、词频和语义特征，按 mtime_ns + size 缓存。"""
    try:
        stat = path.stat()
    except OSError:
        return []
    key = str(path)
    cached = _chunk_cache.get(key)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    entries = [
        (chunk, Counter(terms), _semantic_features(chunk))
        for chunk in _chunks(text)
        if (terms := _token_list(chunk))
    ]
    _chunk_cache[key] = (stat.st_mtime_ns, stat.st_size, entries)
    return entries


def search_results(query: str, dataset: str, top_k: int = TOP_K) -> list[dict]:
    """返回单一数据集内的结构化混合检索结果；ACL 由 search_many 统一执行。"""
    folder = KNOWLEDGE_DIR / dataset
    if not folder.exists() or not query.strip():
        return []
    query_terms = _token_list(query)
    if not query_terms:
        return []
    query_counts = Counter(query_terms)
    query_semantic = _semantic_features(query)
    corpus: list[tuple[str, str, Counter, set[str]]] = []
    for path in sorted(folder.glob("*")):
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        for chunk, counts, semantic in _file_chunks_with_tokens(path):
            corpus.append((path.name, chunk, counts, semantic))
    if not corpus:
        return []

    document_frequency = Counter()
    for _name, _chunk, counts, _semantic in corpus:
        document_frequency.update(counts.keys())
    document_count = len(corpus)
    average_length = sum(sum(counts.values()) for _, _, counts, _ in corpus) / document_count
    k1, b = 1.5, .75
    scored: list[dict] = []
    for source, chunk, counts, semantic in corpus:
        length = max(1, sum(counts.values()))
        bm25 = 0.0
        for term, query_frequency in query_counts.items():
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            frequency_in_docs = document_frequency.get(term, 0)
            inverse = math.log(1 + (
                document_count - frequency_in_docs + .5
            ) / (frequency_in_docs + .5))
            denominator = frequency + k1 * (1 - b + b * length / max(1.0, average_length))
            bm25 += query_frequency * inverse * frequency * (k1 + 1) / denominator
        semantic_score = 0.0
        if query_semantic and semantic:
            semantic_score = len(query_semantic & semantic) / math.sqrt(
                len(query_semantic) * len(semantic)
            )
        bm25_normalized = bm25 / (bm25 + 2.0) if bm25 > 0 else 0.0
        score = .72 * bm25_normalized + .28 * semantic_score
        if score >= SCORE_THRESHOLD:
            scored.append({
                "dataset": dataset,
                "source": source,
                "text": chunk,
                "score": round(score, 6),
                "bm25": round(bm25, 6),
                "semantic": round(semantic_score, 6),
            })
    scored.sort(key=lambda item: (
        item["score"], item["bm25"], item["semantic"], item["source"]
    ), reverse=True)
    return scored[:max(1, int(top_k or TOP_K))]


def search(query: str, dataset: str, top_k: int = TOP_K) -> str:
    """兼容旧调用方：返回拼接后的命中段落（含数据集与来源文件名）。"""
    hits = search_results(query, dataset, top_k)
    return "\n\n".join(
        f"[来源：知识库/{dataset}/{item['source']}；"
        f"混合分={item['score']:.3f}]\n{item['text']}"
        for item in hits
    )


def search_many(
    query: str, dataset_keys, user=None, top_k: int = TOP_K
) -> list[dict]:
    """只在显式数据集范围与当前用户可见范围的交集中检索。"""
    requested = list(dict.fromkeys(
        str(key).strip() for key in (dataset_keys or []) if str(key).strip()
    ))[:20]
    if not requested or not (query or "").strip():
        return []
    visible = {
        item["key"]: item["name"]
        for item in list_datasets(user)
    }
    combined = []
    for key in requested:
        if key not in visible:
            continue
        for hit in search_results(query, key, top_k):
            combined.append({**hit, "dataset_name": visible[key]})
    combined.sort(key=lambda item: (
        item["score"], item["bm25"], item["semantic"], item["dataset"], item["source"]
    ), reverse=True)
    return combined[:max(1, int(top_k or TOP_K))]


def list_documents(user=None) -> dict[str, list[str]]:
    return {item["key"]: item["files"] for item in list_datasets(user)}


def list_datasets(user=None) -> list[dict]:
    datasets = _load_datasets()
    items = []
    for key, meta in datasets.items():
        if user is not None and not _can_see_dataset(user, meta):
            continue
        items.append((key, meta))
    return [
        {
            "key": key,
            "name": meta["name"],
            "builtin": bool(meta.get("builtin")),
            "created_by": meta.get("created_by"),
            "is_public": bool(meta.get("is_public", False)),
            "can_manage": True if user is None else _can_manage_dataset(user, meta),
            "files": sorted(
                p.name for p in (KNOWLEDGE_DIR / key).glob("*")
                if p.suffix.lower() in TEXT_SUFFIXES
            ),
        }
        for key, meta in items
    ]


def create_dataset(name: str, user) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("请填写知识库名称")
    with _locked_meta():
        datasets = _load_datasets_unlocked()
        if name in {meta["name"] for meta in datasets.values()}:
            raise ValueError("知识库名称已存在")
        key = _dataset_key(name, set(datasets))
        datasets[key] = {"name": name, "created_by": getattr(user, "id", None), "builtin": False, "is_public": False}
        (KNOWLEDGE_DIR / key).mkdir(parents=True, exist_ok=True)
        _save_datasets_unlocked(datasets)
    return {
        "key": key, "name": name, "builtin": False,
        "created_by": getattr(user, "id", None), "is_public": False,
        "can_manage": True, "files": [],
    }


def _extract_docx(content: bytes) -> str:
    try:
        import docx
    except Exception as exc:
        raise ValueError("未安装 python-docx，无法解析 Word 文档") from exc

    document = docx.Document(BytesIO(content))
    parts: list[str] = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n\n".join(parts).strip()
    if not text:
        raise ValueError("Word 文档未提取到文本内容")
    return text


def save_document(dataset: str, filename: str, content: bytes, user=None) -> Path:
    meta = _dataset_meta(dataset)
    if meta is None:
        raise ValueError("知识库不存在")
    if user is not None and not _can_manage_dataset(user, meta):
        raise PermissionError("无权管理该知识库")
    safe_name = Path(filename).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        raise ValueError("知识库仅支持 .md / .txt / .docx 文档")
    if suffix == ".docx":
        target = KNOWLEDGE_DIR / dataset / (Path(safe_name).stem + ".txt")
        target.write_text(_extract_docx(content), encoding="utf-8")
    else:
        target = KNOWLEDGE_DIR / dataset / safe_name
        target.write_bytes(content)
    return target


def delete_document(dataset: str, filename: str, user=None) -> bool:
    meta = _dataset_meta(dataset)
    if meta is None:
        return False
    if user is not None and not _can_manage_dataset(user, meta):
        raise PermissionError("无权管理该知识库")
    target = KNOWLEDGE_DIR / dataset / Path(filename).name
    if target.exists():
        target.unlink()
        return True
    return False


def delete_dataset(dataset: str, user) -> bool:
    with _locked_meta():
        datasets = _load_datasets_unlocked()
        meta = datasets.get(dataset)
        if meta is None:
            return False
        if not _can_manage_dataset(user, meta):
            raise PermissionError("无权管理该知识库")
        import shutil

        shutil.rmtree(KNOWLEDGE_DIR / dataset, ignore_errors=True)
        del datasets[dataset]
        _save_datasets_unlocked(datasets)
        # 内置知识库登记墓碑，防止 _load_datasets 再次重建
        if dataset in DEFAULT_DATASETS:
            deleted = _load_deleted_builtins_unlocked()
            deleted.add(dataset)
            _save_deleted_builtins_unlocked(deleted)
    return True


def set_dataset_public(dataset: str, is_public: bool, user) -> dict:
    with _locked_meta():
        datasets = _load_datasets_unlocked()
        meta = datasets.get(dataset)
        if meta is None:
            raise KeyError("知识库不存在")
        if not _can_manage_dataset(user, meta):
            raise PermissionError("无权管理该知识库")
        meta["is_public"] = bool(is_public)
        _save_datasets_unlocked(datasets)
        result_meta = dict(meta)
    return {
        "key": dataset,
        "name": result_meta["name"],
        "builtin": bool(result_meta.get("builtin")),
        "created_by": result_meta.get("created_by"),
        "is_public": bool(result_meta.get("is_public", False)),
        "can_manage": True,
        "files": sorted(
            p.name for p in (KNOWLEDGE_DIR / dataset).glob("*")
            if p.suffix.lower() in TEXT_SUFFIXES
        ),
    }
