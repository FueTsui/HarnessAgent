"""Artifact 模板渲染：产出 Word / Markdown / PPT / Excel 文件并保留原排版。

模板源文件内用 {{占位符}} 标记可填充位。渲染流程：
  1. extract_placeholders 解析源文件中的占位符名（上传时缓存到 Template.placeholders）。
  2. fill_values 让模型按上下文为占位符生成取值（纯文本键值对）。
  3. render 把取值填回源文件副本，写入 EXPORT_DIR，返回路径。

word/ppt 用 python-docx / python-pptx 做 run 级替换；excel 用 openpyxl 做单元格替换；
md/txt 直接做字符串替换。缺对应库时抛 ValueError（其余类型不受影响）。
"""
import datetime
import json
import logging
import re
import uuid
from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from ..config import EXPORT_DIR

logger = logging.getLogger(__name__)

# 占位符语法 {{key}}：键允许中英文、数字、下划线、点、连字符。
PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w一-鿿.\-]+)\s*\}\}")

# kind -> 源文件后缀
KIND_EXT = {"word": ".docx", "md": ".md", "ppt": ".pptx", "excel": ".xlsx"}
EXT_KIND = {".docx": "word", ".md": "md", ".txt": "md", ".pptx": "ppt", ".xlsx": "excel"}


def _new_basename(title: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r'[\\/:*?"<>|]', "_", title or "")[:60] or "template"
    return f"{safe}_{stamp}_{uuid.uuid4().hex[:6]}"


def _dedup(seq) -> list[str]:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _sub_text(text: str, values: dict) -> str:
    """替换字符串中的占位符；缺失取值留空串，未知占位符原样保留。"""
    def repl(m):
        key = m.group(1)
        return str(values[key]) if key in values else m.group(0)
    return PLACEHOLDER_RE.sub(repl, text or "")


# ---------- 占位符解析 ----------

def extract_placeholders(path: Path, kind: str) -> list[str]:
    """扫描源文件，去重保序返回占位符名。解析失败返回空列表（不阻断上传）。"""
    try:
        if kind == "md":
            return _dedup(PLACEHOLDER_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
        if kind == "word":
            return _dedup(_docx_placeholders(path))
        if kind == "excel":
            return _dedup(_xlsx_placeholders(path))
        if kind == "ppt":
            return _dedup(_pptx_placeholders(path))
    except Exception as exc:  # noqa: BLE001 - 解析失败不阻断上传，渲染时再报具体错
        logger.warning("解析模板占位符失败（%s）：%s", path.name, exc)
    return []


def extract_reference_text(path: Path, kind: str, limit: int = 12000) -> str:
    """提取模板可见文字，供模型理解无占位符模板的结构和示例格式。"""
    try:
        if kind == "md":
            text = path.read_text(encoding="utf-8", errors="ignore")
        elif kind == "word":
            import docx
            document = docx.Document(str(path))
            text = "\n".join(
                para.text.strip()
                for para in _iter_docx_paragraphs(document)
                if para.text.strip()
            )
        elif kind == "excel":
            import openpyxl
            workbook = openpyxl.load_workbook(str(path), data_only=True)
            lines = []
            for sheet in workbook.worksheets:
                lines.append(f"[工作表：{sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    values = [str(value) for value in row if value not in (None, "")]
                    if values:
                        lines.append("\t".join(values))
            text = "\n".join(lines)
        elif kind == "ppt":
            from pptx import Presentation
            lines = []
            for index, slide in enumerate(Presentation(str(path)).slides, 1):
                lines.append(f"[第 {index} 页]")
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        value = shape.text.strip()
                        if value:
                            lines.append(value)
            text = "\n".join(lines)
        else:
            text = ""
    except Exception as exc:  # noqa: BLE001 - 模板参考提取失败不应阻断主回答
        logger.warning("提取模板参考内容失败（%s）：%s", path.name, exc)
        return ""
    text = (text or "").strip()
    return text[:limit] + ("\n…（模板示例过长，已截断）" if len(text) > limit else "")


def _iter_docx_paragraphs(document):
    """遍历文档正文 + 所有表格单元格 + 页眉页脚中的段落。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def _cell_paragraphs(table):
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
                for nested in cell.tables:
                    yield from _cell_paragraphs(nested)

    for para in document.paragraphs:
        yield para
    for table in document.tables:
        yield from _cell_paragraphs(table)
    for section in document.sections:
        for hf in (section.header, section.footer):
            for para in hf.paragraphs:
                yield para
            for table in hf.tables:
                yield from _cell_paragraphs(table)


def _docx_placeholders(path: Path) -> list[str]:
    import docx
    found: list[str] = []
    for para in _iter_docx_paragraphs(docx.Document(str(path))):
        found.extend(PLACEHOLDER_RE.findall(para.text))
    return found


def _xlsx_placeholders(path: Path) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(str(path))
    found: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    found.extend(PLACEHOLDER_RE.findall(cell.value))
    return found


def _iter_pptx_shapes(shapes):
    """Yield every PowerPoint shape, including children of grouped shapes."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_pptx_shapes(shape.shapes)


def _iter_pptx_text_frames(prs, include_layouts: bool = True):
    """Yield editable text frames from slides and, optionally, their layouts.

    Corporate templates commonly group their authoring text and sometimes put
    placeholders directly on a custom layout.  Looking only at top-level slide
    shapes silently misses both cases.
    """
    shape_sets = [slide.shapes for slide in prs.slides]
    if include_layouts:
        shape_sets.extend(layout.shapes for layout in prs.slide_layouts)
    for shapes in shape_sets:
        for shape in _iter_pptx_shapes(shapes):
            if shape.has_text_frame:
                yield shape.text_frame
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        yield cell.text_frame


def _pptx_placeholders(path: Path) -> list[str]:
    from pptx import Presentation
    found: list[str] = []
    for tf in _iter_pptx_text_frames(Presentation(str(path))):
        for para in tf.paragraphs:
            found.extend(PLACEHOLDER_RE.findall("".join(r.text for r in para.runs)))
    return found


# ---------- 渲染 ----------

def render(
    src_path: Path,
    kind: str,
    values: dict,
    title: str = "",
    append_body: str = "",
    replace_body: bool = False,
) -> Path:
    """把占位符取值填回源文件副本，写入 EXPORT_DIR，返回输出路径。

    append_body：可选的模型生成正文。
    replace_body：无占位符模板作为格式示例使用时，移除示例正文并写入新正文，避免把原模板
    与生成内容叠加。文档级样式、页面设置、主题和母版仍沿用模板。
    """
    values = {str(k): ("" if v is None else str(v)) for k, v in (values or {}).items()}
    body = (append_body or "").strip()
    base = _new_basename(title or src_path.stem)
    if kind == "md":
        out = EXPORT_DIR / f"{base}.md"
        text = _sub_text(src_path.read_text(encoding="utf-8", errors="ignore"), values)
        if replace_body and body:
            text = f"{body}\n"
        elif body:
            text = f"{text.rstrip()}\n\n{body}\n"
        out.write_text(text, encoding="utf-8")
        return out
    if kind == "word":
        return _render_docx(src_path, values, base, body, replace_body)
    if kind == "excel":
        return _render_xlsx(src_path, values, base, body, replace_body)
    if kind == "ppt":
        return _render_pptx(src_path, values, base, body, replace_body)
    raise ValueError(f"未知模板类型：{kind}")


def _replace_in_paragraph(para, values: dict) -> None:
    """段落级占位符替换：拼接 run 文本→替换→写回首个 run、清空其余（保留段落样式）。

    占位符常被 Word 拆进多个 run，逐 run 替换会漏；段内若无占位符则不动，保留原混排格式。
    """
    runs = para.runs
    if not runs:
        return
    full = "".join(r.text for r in runs)
    if "{{" not in full:
        return
    replaced = _sub_text(full, values)
    if replaced == full:
        return
    runs[0].text = replaced
    for r in runs[1:]:
        r.text = ""


def _append_docx_body(document, body: str, separate: bool = True) -> None:
    """把回答正文按轻量 Markdown 追加到 Word 文档末尾：# 标题→Heading、- 项→列表、其余→段落。"""
    if separate:
        document.add_paragraph()  # 与原模板内容隔开
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            document.add_heading(h.group(2).strip(), level=min(len(h.group(1)), 4))
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)", line)
        if bullet:
            document.add_paragraph(bullet.group(1).strip(), style="List Bullet")
            continue
        document.add_paragraph(line)


def _clear_docx_body(document) -> None:
    body = document._element.body
    for child in list(body):
        if child.tag.endswith("}sectPr"):
            continue
        body.remove(child)


_OFFICIAL_HEADING_RE = re.compile(
    r"^(?:[一二三四五六七八九十]+[、.]|（[一二三四五六七八九十]+）|\([一二三四五六七八九十]+\))"
)


def _replace_docx_paragraph_text(paragraph, text: str) -> None:
    """Replace visible text while retaining the paragraph and its first text run formatting.

    Template paragraphs can contain several runs because of mixed fonts or Word's own
    run splitting.  Rebuilding the paragraph with ``paragraph.text = ...`` discards
    that direct formatting.  Empty runs that contain drawings are deliberately left
    alone so template furniture cannot be removed by a text replacement.
    """
    target = next((run for run in paragraph.runs if run.text), None)
    if target is None:
        target = paragraph.add_run()
    target_element = target._r
    target.text = text
    for run in paragraph.runs:
        if run._r is not target_element and run.text:
            run.text = ""


def _paragraph_max_font_pt(paragraph) -> float:
    sizes = [run.font.size.pt for run in paragraph.runs if run.font.size is not None]
    return max(sizes, default=0.0)


def _find_docx_title_slot(paragraphs) -> int | None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    for index, paragraph in enumerate(paragraphs):
        text = paragraph.text.strip()
        if not text or paragraph.alignment != WD_ALIGN_PARAGRAPH.CENTER:
            continue
        if _paragraph_max_font_pt(paragraph) >= 20 or (
            "关于" in text and any(word in text for word in ("请示", "报告", "通知", "函"))
        ):
            return index
    return None


def _find_docx_addressee_slot(paragraphs, title_index: int) -> int | None:
    for index in range(title_index + 1, len(paragraphs)):
        text = paragraphs[index].text.strip()
        if not text:
            continue
        if "主送" in text or text.endswith(("：", ":")):
            return index
        # An official template normally places the addressee in the first non-empty
        # paragraph after the title.  This fallback also supports templates that use
        # sample organization names without an explicit "主送单位" label.
        return index
    return None


def _find_docx_suffix_start(paragraphs, body_start: int) -> int | None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    right_aligned = [
        index for index in range(body_start, len(paragraphs))
        if paragraphs[index].text.strip()
        and paragraphs[index].alignment == WD_ALIGN_PARAGRAPH.RIGHT
    ]
    if right_aligned:
        return right_aligned[0]
    contact_index = next((
        index for index in range(body_start, len(paragraphs))
        if any(marker in paragraphs[index].text for marker in ("联系人", "联系电话"))
    ), None)
    if contact_index is not None:
        index = contact_index
        while index > body_start and paragraphs[index - 1].text.strip():
            index -= 1
        return index
    return None


def _source_docx_slots(document):
    non_empty = [(index, paragraph) for index, paragraph in enumerate(document.paragraphs)
                 if paragraph.text.strip()]
    if len(non_empty) < 3:
        raise ValueError("源 Word 文档缺少可识别的标题、主送单位或正文")
    title_pos, title = non_empty[0]
    addressee_at = next((
        offset for offset, (_index, paragraph) in enumerate(non_empty[1:], 1)
        if paragraph.text.strip().endswith(("：", ":"))
    ), 1)
    _addressee_pos, addressee = non_empty[addressee_at]
    body = [paragraph for index, paragraph in non_empty[addressee_at + 1:] if index > title_pos]
    if not body:
        raise ValueError("源 Word 文档未识别到正文")
    return title, addressee, body


_GENERATED_EXPORT_LINK_RE = re.compile(
    r"\[[^\]]*\]\((?:sandbox:)?/api/v1/exports/[^)]+\)", re.IGNORECASE
)
_GENERATED_TITLE_RE = re.compile(r"关于.+(?:请示|报告|通知|函)$")
_GENERATED_DATE_RE = re.compile(
    r"^(?:\d{4}|[二〇○零一二三四五六七八九十]{4})年"
    r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})月"
    r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})日$"
)
_GENERATED_DELIVERY_RE = re.compile(
    r"^(?:已|已经|现已).{0,12}(?:完成|生成|处理|制作)(?:完毕)?[。！!：:]?$"
)


def _plain_generated_line(raw: str) -> tuple[str, int]:
    """Return clean visible text and its Markdown heading level.

    The main model is asked for a complete answer and commonly uses Markdown for
    readability.  A Word sample template needs the semantic text, not Markdown
    markers or an artifact delivery link.
    """
    value = (raw or "").strip()
    if not value or value.startswith("```"):
        return "", 0
    heading = re.match(r"^(#{1,6})\s+(.*)$", value)
    level = len(heading.group(1)) if heading else 0
    value = heading.group(2).strip() if heading else value
    value = re.sub(r"^\s*[-*]\s+", "", value)
    value = _GENERATED_EXPORT_LINK_RE.sub("", value).strip()
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    return value.strip(), level


def _generated_docx_slots(text: str) -> tuple[str, str, list[str]]:
    """Parse a complete model answer into title, addressee and body slots.

    This intentionally rejects delivery-only prose.  Rendering a pristine copy of
    the sample template would look successful while leaving all example content in
    place, which is worse than producing no artifact and logging the render error.
    """
    lines = [
        (value, level)
        for value, level in (_plain_generated_line(raw) for raw in (text or "").splitlines())
        if value
    ]
    if len(lines) < 3:
        raise ValueError("模型回答缺少可写入模板的完整标题、主送单位和正文")

    title_index = next((
        index for index, (value, _level) in enumerate(lines)
        if _GENERATED_TITLE_RE.search(value)
    ), None)
    if title_index is None:
        title_index = next((
            index for index, (_value, level) in enumerate(lines) if level == 1
        ), 0)
    title = lines[title_index][0]

    addressee_index = next((
        index for index in range(title_index + 1, len(lines))
        if lines[index][0].endswith(("：", ":"))
    ), None)
    if addressee_index is None:
        raise ValueError("模型回答中未识别到主送单位")
    addressee = lines[addressee_index][0]
    body = [value for value, _level in lines[addressee_index + 1:]]

    # The template owns its right-aligned issuing organization/date/contact block.
    # Drop a generated trailing date and a likely unsigned organization label so
    # that content is not duplicated immediately before the template furniture.
    while body and (
        _GENERATED_DELIVERY_RE.match(body[-1])
        or body[-1].startswith(("（联系人", "(联系人"))
    ):
        body.pop()
    if body and _GENERATED_DATE_RE.match(body[-1].replace(" ", "")):
        body.pop()
        if body:
            candidate = body[-1]
            if (
                not candidate.endswith(("。", "；", ";", "：", ":"))
                and not candidate.startswith(("附件", "妥否"))
                and not _OFFICIAL_HEADING_RE.match(candidate)
            ):
                body.pop()
    while body and _GENERATED_DELIVERY_RE.match(body[-1]):
        body.pop()
    if not body:
        raise ValueError("模型回答中未识别到可写入模板的正文")
    return title, addressee, body


def _render_word_template_content(
    template_path: Path,
    source_title: str,
    source_addressee: str,
    source_body: list[str],
    title: str = "",
) -> Path:
    """Write semantic content into a sample Word template package.

    Every inserted paragraph is cloned from the template's own body/heading
    prototype.  No generic font, line-spacing or style override is applied: the
    selected template file is the formatting authority.
    """
    try:
        import docx
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise ValueError("未安装 python-docx，无法套用 Word 模板") from exc

    template_path = Path(template_path)
    if template_path.suffix.lower() != ".docx":
        raise ValueError("样式套用仅支持 DOCX 模板")
    if not source_title.strip() or not source_addressee.strip() or not source_body:
        raise ValueError("套版内容缺少标题、主送单位或正文")

    document = docx.Document(str(template_path))
    paragraphs = document.paragraphs
    title_index = _find_docx_title_slot(paragraphs)
    if title_index is None:
        raise ValueError("模板中未找到居中的标题样例段落")
    addressee_index = _find_docx_addressee_slot(paragraphs, title_index)
    if addressee_index is None:
        raise ValueError("模板中未找到主送单位样例段落")
    body_start = addressee_index + 1
    suffix_index = _find_docx_suffix_start(paragraphs, body_start)
    if suffix_index is None or suffix_index <= body_start:
        raise ValueError("模板中未找到落款/日期区域，无法安全确定正文边界")

    sample_region = paragraphs[body_start:suffix_index]
    regular_prototype = next((
        paragraph for paragraph in sample_region
        if paragraph.text.strip()
        and not _OFFICIAL_HEADING_RE.match(paragraph.text.strip())
        and not paragraph.text.strip().startswith(("附件", "妥否"))
    ), None)
    heading_prototype = next((
        paragraph for paragraph in sample_region
        if _OFFICIAL_HEADING_RE.match(paragraph.text.strip())
    ), regular_prototype)
    if regular_prototype is None:
        raise ValueError("模板正文区域中没有可复用的正文样例段落")

    gap_prototypes = []
    for paragraph in reversed(sample_region):
        if paragraph.text.strip():
            break
        gap_prototypes.append(deepcopy(paragraph._p))
    gap_prototypes.reverse()
    gap_prototypes = gap_prototypes[-3:]

    regular_xml = deepcopy(regular_prototype._p)
    heading_xml = deepcopy((heading_prototype or regular_prototype)._p)
    suffix_anchor = paragraphs[suffix_index]._p

    _replace_docx_paragraph_text(paragraphs[title_index], source_title.strip())
    _replace_docx_paragraph_text(paragraphs[addressee_index], source_addressee.strip())
    for paragraph in sample_region:
        paragraph._element.getparent().remove(paragraph._element)

    for text in source_body:
        value = str(text or "").strip()
        if not value:
            continue
        prototype = heading_xml if _OFFICIAL_HEADING_RE.match(value) else regular_xml
        new_xml = deepcopy(prototype)
        suffix_anchor.addprevious(new_xml)
        inserted = Paragraph(new_xml, document._body)
        _replace_docx_paragraph_text(inserted, value)

    for gap_xml in gap_prototypes:
        new_xml = deepcopy(gap_xml)
        suffix_anchor.addprevious(new_xml)

    out = EXPORT_DIR / f"{_new_basename(title or template_path.stem)}.docx"
    _save_template_document_xml(template_path, out, document)
    return out


def _save_template_document_xml(template_path: Path, out: Path, document) -> None:
    """Save only the edited main document part into a byte-preserved template copy.

    ``python-docx.Document.save`` rewrites relationships, styles, numbering,
    headers, footers and other package parts even when only body paragraphs changed.
    A sample-template render edits only ``word/document.xml``; copying every other
    ZIP member byte-for-byte keeps the selected template's opaque features intact.
    """
    from lxml import etree

    document_xml = etree.tostring(
        document._element,
        encoding="UTF-8",
        xml_declaration=True,
        standalone=True,
    )
    with ZipFile(template_path, "r") as source, ZipFile(out, "w", ZIP_DEFLATED) as target:
        found_document = False
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "word/document.xml":
                data = document_xml
                found_document = True
            target.writestr(info, data)
        if not found_document:
            raise ValueError("模板包缺少 word/document.xml")


def render_word_template_from_document(
    template_path: Path,
    source_path: Path,
    title: str = "",
) -> Path:
    """Apply a sample-style Word template to an existing Word document.

    This path is for Word templates without ``{{placeholders}}``.  The template is
    always the package base, so its sections, theme, styles, drawings, headers,
    footers, numbering area and closing furniture survive.  Only the title,
    addressee and sample body region are replaced with text from ``source_path``.
    Assistant response text is intentionally not accepted by this API.
    """
    try:
        import docx
    except ImportError as exc:
        raise ValueError("未安装 python-docx，无法套用 Word 模板") from exc

    template_path = Path(template_path)
    source_path = Path(source_path)
    if template_path.suffix.lower() != ".docx" or source_path.suffix.lower() != ".docx":
        raise ValueError("样式套用仅支持 DOCX 模板和 DOCX 源文档")

    source = docx.Document(str(source_path))
    source_title, source_addressee, source_body = _source_docx_slots(source)
    return _render_word_template_content(
        template_path,
        source_title.text,
        source_addressee.text,
        [paragraph.text for paragraph in source_body],
        title=title,
    )


def render_word_template_from_text(
    template_path: Path,
    source_text: str,
    title: str = "",
) -> Path:
    """Apply a sample-style Word template to a complete generated answer."""
    source_title, source_addressee, source_body = _generated_docx_slots(source_text)
    return _render_word_template_content(
        template_path,
        source_title,
        source_addressee,
        source_body,
        title=title,
    )


def _render_docx(
    src_path: Path,
    values: dict,
    base: str,
    body: str = "",
    replace_body: bool = False,
) -> Path:
    try:
        import docx  # noqa: F401
    except ImportError as exc:
        raise ValueError("未安装 python-docx，无法渲染 Word 模板") from exc
    import docx as _docx

    document = _docx.Document(str(src_path))
    if replace_body and body:
        _clear_docx_body(document)
    else:
        for para in _iter_docx_paragraphs(document):
            _replace_in_paragraph(para, values)
    if body:
        _append_docx_body(document, body, separate=not replace_body)
    out = EXPORT_DIR / f"{base}.docx"
    document.save(str(out))
    return out


def _render_xlsx(
    src_path: Path,
    values: dict,
    base: str,
    body: str = "",
    replace_body: bool = False,
) -> Path:
    try:
        import openpyxl
    except ImportError as exc:
        raise ValueError("未安装 openpyxl，无法渲染 Excel 模板") from exc

    wb = openpyxl.load_workbook(str(src_path))
    if replace_body and body:
        for ws in wb.worksheets:
            if ws.max_row:
                ws.delete_rows(1, ws.max_row)
    else:
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and "{{" in cell.value:
                        cell.value = _sub_text(cell.value, values)
    if body:
        ws = wb.active or wb.worksheets[0]
        ws.append([])
        for raw in body.splitlines():
            line = raw.rstrip()
            if line.strip():
                ws.append([line])
    out = EXPORT_DIR / f"{base}.xlsx"
    wb.save(str(out))
    return out


_PPTX_PAGE_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s*第\s*(\d+)\s*页(?:\s*[｜|]\s*(.*?))?\s*$",
    re.IGNORECASE,
)
_PPTX_TABLE_DIVIDER_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def _plain_pptx_line(raw: str) -> str:
    """Convert one lightweight-Markdown line to audience-facing slide text."""
    value = str(raw or "").strip()
    if not value or value == "---" or value.startswith("```"):
        return ""
    if _PPTX_TABLE_DIVIDER_RE.match(value):
        return ""
    value = re.sub(r"^#{1,6}\s+", "", value)
    value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"__([^_]+)__", r"\1", value)
    # 模型可能把粗体标记跨行书写（首行只有开头 **、末行只有结尾 **）。
    # 单行正则无法成对匹配，最终兜底必须移除残留定界符。
    value = value.replace("**", "").replace("__", "").replace("`", "")
    if value.startswith("|") and value.endswith("|"):
        cells = [cell.strip() for cell in value.strip("|").split("|")]
        value = " ｜ ".join(cell for cell in cells if cell)
    bullet = re.match(r"^\s*[-*+]\s+(.*)$", value)
    if bullet:
        value = f"• {bullet.group(1).strip()}"
    return value.strip()


def _pptx_slide_spec(label: str, raw_lines: list[str]) -> dict:
    lines = [value for value in (_plain_pptx_line(line) for line in raw_lines) if value]
    title = ""
    if lines and not lines[0].startswith("• ") and len(lines[0]) <= 100:
        title = lines.pop(0)
    title = title or _plain_pptx_line(label) or "内容"
    if lines and lines[0].replace(" ", "") == title.replace(" ", ""):
        lines.pop(0)
    return {"title": title, "body": lines}


def _parse_pptx_body(body: str) -> list[dict]:
    """Parse a generated deck outline into ordered title/body slide records."""
    source_lines = str(body or "").splitlines()
    headings = []
    for index, line in enumerate(source_lines):
        match = _PPTX_PAGE_HEADING_RE.match(line)
        if match:
            headings.append((index, match.group(2) or f"第{match.group(1)}页"))
    if headings:
        slides = []
        for offset, (start, label) in enumerate(headings):
            end = headings[offset + 1][0] if offset + 1 < len(headings) else len(source_lines)
            slides.append(_pptx_slide_spec(label, source_lines[start + 1:end]))
        return slides

    # A model may use ordinary Markdown headings or horizontal rules instead of
    # explicit "第 N 页" labels.  Preserve those page boundaries as a fallback.
    heading_indexes = [
        index for index, line in enumerate(source_lines)
        if re.match(r"^\s*#{1,2}\s+\S", line)
    ]
    if len(heading_indexes) >= 2:
        slides = []
        for offset, start in enumerate(heading_indexes):
            end = heading_indexes[offset + 1] if offset + 1 < len(heading_indexes) else len(source_lines)
            label = re.sub(r"^\s*#{1,6}\s+", "", source_lines[start]).strip()
            slides.append(_pptx_slide_spec(label, source_lines[start + 1:end]))
        return slides

    chunks = re.split(r"(?m)^\s*---\s*$", str(body or ""))
    chunks = [chunk for chunk in chunks if chunk.strip()]
    if len(chunks) >= 2:
        return [_pptx_slide_spec("", chunk.splitlines()) for chunk in chunks]
    return [_pptx_slide_spec("", source_lines)] if str(body or "").strip() else []


def _pptx_shape_font_size(shape) -> float:
    if not getattr(shape, "has_text_frame", False):
        return 0.0
    sizes = [
        run.font.size.pt
        for paragraph in shape.text_frame.paragraphs
        for run in paragraph.runs
        if run.font.size is not None
    ]
    return max(sizes, default=0.0)


def _is_pptx_furniture(shape) -> bool:
    if not getattr(shape, "is_placeholder", False):
        return False
    from pptx.enum.shapes import PP_PLACEHOLDER

    return shape.placeholder_format.type in {
        PP_PLACEHOLDER.DATE,
        PP_PLACEHOLDER.FOOTER,
        PP_PLACEHOLDER.SLIDE_NUMBER,
    }


def _pptx_text_shapes(shapes) -> list:
    return [
        shape for shape in _iter_pptx_shapes(shapes)
        if getattr(shape, "has_text_frame", False) and not _is_pptx_furniture(shape)
    ]


def _replace_pptx_text_frame(text_frame, text: str) -> None:
    """Replace text while retaining the frame's first paragraph/run formatting."""
    lines = str(text or "").splitlines() or [""]
    first = text_frame.paragraphs[0]
    prototype_ppr = deepcopy(first._p.pPr) if first._p.pPr is not None else None
    prototype_rpr = None
    if first.runs and first.runs[0]._r.rPr is not None:
        prototype_rpr = deepcopy(first.runs[0]._r.rPr)

    for paragraph in list(text_frame.paragraphs[1:]):
        text_frame._txBody.remove(paragraph._p)
    if first.runs:
        first.runs[0].text = lines[0]
        for run in first.runs[1:]:
            run.text = ""
    else:
        first.text = lines[0]

    for line in lines[1:]:
        paragraph = text_frame.add_paragraph()
        if prototype_ppr is not None:
            if paragraph._p.pPr is not None:
                paragraph._p.remove(paragraph._p.pPr)
            paragraph._p.insert(0, deepcopy(prototype_ppr))
        run = paragraph.add_run()
        if prototype_rpr is not None:
            if run._r.rPr is not None:
                run._r.remove(run._r.rPr)
            run._r.insert(0, deepcopy(prototype_rpr))
        run.text = line


def _pptx_title_shape(shapes):
    candidates = [
        shape for shape in _pptx_text_shapes(shapes)
        if str(getattr(shape, "text", "") or "").strip()
        and not str(getattr(shape, "text", "") or "").strip().isdigit()
    ]
    if not candidates:
        return None
    explicit_prompt = next((
        shape for shape in candidates
        if "请输入您的标题" in str(getattr(shape, "text", "")).replace(" ", "")
        or "请输入你的标题" in str(getattr(shape, "text", "")).replace(" ", "")
    ), None)
    if explicit_prompt is not None:
        return explicit_prompt
    return max(
        candidates,
        key=lambda shape: (
            _pptx_shape_font_size(shape),
            -int(getattr(shape, "top", 0)),
            int(getattr(shape, "width", 0)) * int(getattr(shape, "height", 0)),
        ),
    )


def _add_pptx_body_text(slide, prs, title_shape, lines: list[str]) -> None:
    if not lines:
        return
    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Inches, Pt

    top = max(
        int(Inches(1.18)),
        int(getattr(title_shape, "top", 0) + getattr(title_shape, "height", 0) + Inches(0.12)),
    )
    left = int(Inches(0.72))
    width = int(prs.slide_width - Inches(1.44))
    height = int(prs.slide_height - top - Inches(0.48))
    box = slide.shapes.add_textbox(left, top, width, max(height, int(Inches(1))))
    frame = box.text_frame
    frame.clear()
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = Inches(0.08)
    frame.margin_top = frame.margin_bottom = Inches(0.04)
    frame.vertical_anchor = MSO_ANCHOR.TOP
    longest = max((len(line) for line in lines), default=0)
    if len(lines) <= 9 and longest <= 70:
        size = 20
    elif len(lines) <= 14 and longest <= 95:
        size = 17
    else:
        size = 14
    for index, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        run = paragraph.add_run()
        run.text = line
        run.font.name = "微软雅黑"
        run.font.size = Pt(size)
        run.font.color.rgb = RGBColor(31, 55, 75)
        paragraph.space_after = Pt(5 if size >= 17 else 3)


def _fill_pptx_cover(slide, spec: dict) -> None:
    shapes = _pptx_text_shapes(slide.shapes)
    title_shape = _pptx_title_shape(slide.shapes)
    if title_shape is not None:
        _replace_pptx_text_frame(title_shape.text_frame, spec["title"])
    others = [
        shape for shape in shapes
        if title_shape is None or shape._element is not title_shape._element
    ]
    if others and spec["body"]:
        _replace_pptx_text_frame(others[0].text_frame, "\n".join(spec["body"][:4]))


def _fill_pptx_directory(prs, slide, specs: list[dict]) -> None:
    title_shapes = [
        shape for shape in _pptx_text_shapes(slide.shapes)
        if "请输入" in str(getattr(shape, "text", "")).replace(" ", "")
    ]
    title_shapes.sort(key=lambda shape: (int(shape.top), int(shape.left)))
    section_titles = [
        specs[index]["title"]
        for index, source_slide in enumerate(prs.slides)
        if index < len(specs) and "章节" in (source_slide.slide_layout.name or "")
    ]
    for index, shape in enumerate(title_shapes):
        value = section_titles[index] if index < len(section_titles) else ""
        _replace_pptx_text_frame(shape.text_frame, value)


def _fill_pptx_section_slide(slide, spec: dict) -> None:
    shapes = _pptx_text_shapes(slide.shapes)
    title_shape = _pptx_title_shape(slide.shapes)
    if title_shape is not None:
        # Some section layouts intentionally extend their sample title box past
        # the slide edge.  That works for the short placeholder text, but clips
        # real Chinese headings.  Keep a small, consistent safe margin.
        from pptx.util import Inches

        safe_left = Inches(0.55)
        if int(title_shape.left) < int(safe_left):
            right = int(title_shape.left) + int(title_shape.width)
            title_shape.left = safe_left
            title_shape.width = max(Inches(2.2), right - int(safe_left))
        _replace_pptx_text_frame(title_shape.text_frame, spec["title"])
        if len(spec["title"]) > 8:
            from pptx.util import Pt

            title_size = 24 if len(spec["title"]) > 12 else 28
            for paragraph in title_shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(title_size)
    detail_shapes = [
        shape for shape in shapes
        if title_shape is None or shape._element is not title_shape._element
    ]
    details = []
    for raw in spec["body"]:
        value = raw[2:] if raw.startswith("• ") else raw
        value = re.sub(r"^\d+[.、]\s*", "", value)
        if value.startswith("通过") and "访问" in value:
            value = value[value.index("访问"):]
        elif value.startswith("通过") and "进入" in value:
            value = value[value.index("进入"):]
        if "扫码登录" in value:
            value = value[value.index("扫码登录"):]
        value = value.replace("输入账号密码", "输入账号")
        if "｜" in value:
            value = value.split("｜", 1)[0].strip()
        separator = "：" if "：" in value else (":" if ":" in value else "")
        if separator and len(value) > 10:
            value = value.split(separator, 1)[0].strip()
        # 章节页槽位足以容纳常见的 16 字短语。固定截成 11 字会把
        # “aTrust VPN”和“文件的存储”等完整语义切断，属于静默数据损坏。
        details.append(value if len(value) <= 16 else value[:15].rstrip() + "…")
    for index, shape in enumerate(detail_shapes):
        value = details[index] if index < len(details) else ""
        _replace_pptx_text_frame(shape.text_frame, value)


def _fill_pptx_content_slide(prs, slide, spec: dict) -> None:
    shapes = _pptx_text_shapes(slide.shapes)
    title_shape = _pptx_title_shape(slide.shapes)
    if title_shape is not None:
        _replace_pptx_text_frame(title_shape.text_frame, spec["title"])
    body_shapes = [
        shape for shape in shapes
        if title_shape is None or shape._element is not title_shape._element
    ]
    if body_shapes:
        primary = max(
            body_shapes,
            key=lambda shape: int(shape.width) * int(shape.height),
        )
        _replace_pptx_text_frame(primary.text_frame, "\n".join(spec["body"]))
        for shape in body_shapes:
            if shape._element is not primary._element:
                _replace_pptx_text_frame(shape.text_frame, "")
    else:
        _add_pptx_body_text(slide, prs, title_shape, spec["body"])


def _fill_pptx_closing_slide(slide, spec: dict) -> None:
    shapes = [
        shape for shape in _pptx_text_shapes(slide.slide_layout.shapes)
        if str(getattr(shape, "text", "") or "").strip()
    ]
    if not shapes:
        return
    title_shape = next(
        (shape for shape in shapes if "谢谢" in str(getattr(shape, "text", ""))),
        _pptx_title_shape(slide.slide_layout.shapes),
    )
    if title_shape is not None:
        closing_title = spec["title"]
        if len(closing_title) > 10:
            closing_title = re.split(r"[，,；;]", closing_title, maxsplit=1)[0]
        _replace_pptx_text_frame(title_shape.text_frame, closing_title[:10])
    other = next((
        shape for shape in shapes
        if title_shape is None or shape._element is not title_shape._element
    ), None)
    if other is not None and spec["body"]:
        _replace_pptx_text_frame(other.text_frame, "\n".join(spec["body"][:4]))


def _fill_pptx_sample_template(prs, specs: list[dict]) -> None:
    """Fill a sample deck in place instead of deleting its template slides."""
    for index, slide in enumerate(prs.slides):
        if index >= len(specs):
            break
        spec = specs[index]
        layout_name = slide.slide_layout.name or ""
        if index == 0:
            _fill_pptx_cover(slide, spec)
        elif "目录" in layout_name:
            _fill_pptx_directory(prs, slide, specs)
        elif "章节" in layout_name:
            _fill_pptx_section_slide(slide, spec)
        elif "正文" in layout_name:
            _fill_pptx_content_slide(prs, slide, spec)
        elif "结尾" in layout_name or index == len(prs.slides) - 1:
            _fill_pptx_closing_slide(slide, spec)
        else:
            _fill_pptx_content_slide(prs, slide, spec)


def _append_pptx_body(prs, body: str) -> None:
    """Create one readable fallback slide when no structured deck outline exists."""
    specs = _parse_pptx_body(body)
    if not specs:
        return
    # Prefer a true title/body layout.  Counting all placeholders incorrectly
    # selects date/footer-only or decorative closing layouts in many templates.
    from pptx.enum.shapes import PP_PLACEHOLDER

    title_types = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}
    body_types = {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.SUBTITLE}
    layout = next((
        candidate for candidate in prs.slide_layouts
        if any(ph.placeholder_format.type in title_types for ph in candidate.placeholders)
        and any(ph.placeholder_format.type in body_types for ph in candidate.placeholders)
    ), prs.slide_layouts[-1])
    slide = prs.slides.add_slide(layout)
    title_shape = slide.shapes.title
    if title_shape is not None:
        _replace_pptx_text_frame(title_shape.text_frame, specs[0]["title"])
    body_placeholder = next((
        ph for ph in slide.placeholders
        if ph.placeholder_format.type in body_types
    ), None)
    if body_placeholder is not None:
        _replace_pptx_text_frame(body_placeholder.text_frame, "\n".join(specs[0]["body"]))
    else:
        _add_pptx_body_text(slide, prs, title_shape, specs[0]["body"])


def _clear_pptx_slides(prs) -> None:
    for slide_id in list(prs.slides._sldIdLst):
        prs.part.drop_rel(slide_id.rId)
        prs.slides._sldIdLst.remove(slide_id)


def _render_pptx(
    src_path: Path,
    values: dict,
    base: str,
    body: str = "",
    replace_body: bool = False,
) -> Path:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise ValueError("未安装 python-pptx，无法渲染 PPT 模板") from exc

    prs = Presentation(str(src_path))
    structured_specs = _parse_pptx_body(body) if replace_body and body else []
    if replace_body and body and len(structured_specs) >= 2:
        _fill_pptx_sample_template(prs, structured_specs)
    elif replace_body and body:
        _clear_pptx_slides(prs)
    else:
        for tf in _iter_pptx_text_frames(prs):
            for para in tf.paragraphs:
                runs = para.runs
                if not runs:
                    continue
                full = "".join(r.text for r in runs)
                if "{{" not in full:
                    continue
                replaced = _sub_text(full, values)
                if replaced == full:
                    continue
                runs[0].text = replaced
                for r in runs[1:]:
                    r.text = ""
    if body and not (replace_body and len(structured_specs) >= 2):
        _append_pptx_body(prs, body)
    out = EXPORT_DIR / f"{base}.pptx"
    prs.save(str(out))
    return out


# ---------- 模型填充 ----------

def _extract_json_obj(text: str) -> dict:
    """从模型输出中稳健提取 JSON 对象：剥离 <think>、代码围栏，取首个 {...}。"""
    text = "" if text is None else str(text)
    text = re.sub(r"<think(?:ing)?[^>]*>.*?</think(?:ing)?>", "", text, flags=re.DOTALL | re.IGNORECASE)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start == -1:
        return {}
    depth, end = 0, -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return {}
    try:
        data = json.loads(text[start:end])
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def fill_values(llm, placeholders: list[str], source_text: str, extra: str = "") -> dict:
    """让模型按内容为占位符生成取值。无占位符时直接返回空 dict（不调用模型）。"""
    placeholders = [p for p in (placeholders or []) if str(p).strip()]
    if not placeholders:
        return {}
    system = (
        "你是报告模板填充助手。请根据提供的内容，为下列占位符逐一生成取值。\n"
        "只输出一个 JSON 对象：键为占位符名（与给定完全一致），值为纯文本字符串；"
        "无法从内容确定的占位符，值留空字符串。不要输出 JSON 以外的任何内容，不要输出思考过程。\n"
        f"占位符列表：{json.dumps(placeholders, ensure_ascii=False)}"
    )
    user = source_text or "（无额外内容，尽量根据占位符名合理留空）"
    if extra:
        user = f"{extra}\n\n{user}"
    raw = await llm.chat(system=system, user=user, temperature=0.2)
    data = _extract_json_obj(raw)
    # 只保留声明过的占位符，缺失补空串
    return {k: ("" if data.get(k) is None else str(data.get(k, ""))) for k in placeholders}
