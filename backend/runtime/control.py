"""低能力模型的确定性控制层：工具路由、参数修复、失败分析和完成验证。"""
from dataclasses import dataclass
import json
import re
from typing import Any


_WORD_RE = re.compile(r"[a-z0-9_]{2,}", re.IGNORECASE)
_ERROR_PATTERNS = (
    re.compile(
        r"(?:^|\n)\s*(?:error|failed|failure|exception)\s*(?:[:：]|\b)|Traceback\s*\(",
        re.IGNORECASE,
    ),
    re.compile(r"(?:工具调用失败|工具返回错误|参数校验失败|未找到资源|未知工具)"),
    re.compile(r"\bHTTP\s*[45]\d\d\b", re.IGNORECASE),
    re.compile(r"\bexit\s*code\s*[:=]?\s*[1-9]\d*\b", re.IGNORECASE),
    re.compile(r"\b[1-9]\d*\s+failed\b", re.IGNORECASE),
)
_COMMON_ALIASES = {
    "q": ("query", "keyword", "keywords", "text", "search"),
    "query": ("q", "keyword", "keywords", "text", "search"),
    "path": ("file", "filename", "filepath"),
    "file": ("path", "filename", "filepath"),
    "url": ("uri", "link"),
    "id": ("identifier",),
}

# 明确依赖实时外部信息的意图必须保留联网搜索。仅靠中英文分词相似度时，
# “深圳天气”与工具描述“免费联网搜索”没有共同词，容易被目录前部的文件工具挤出候选。
_LIVE_WEB_INTENT = re.compile(
    r"(?:"
    r"天气|气温|降雨|台风|空气质量|股价|股票|股市|证券市场|指数|收盘|开盘|盘中|板块|"
    r"A股|港股|美股|韩股|日股|欧股|台股|英股|德股|法股|印股|行情|大盘|"
    r"KOSPI|KOSDAQ|NIKKEI|NASDAQ|DOW\s*JONES|S&P\s*500|FTSE|DAX|CAC\s*40|STOXX|"
    r"市场表现|涨跌|成交额|资金(?:净?流[入出向])|主力资金|大额流[入出]|汇率|"
    r"新闻|热搜|比分|赛果|航班|列车|路况|联网|互联网|在线搜索|网上查|"
    r"实时|(?:昨日|今日|明日|昨天|今天|明天|最新|当前).*(?:新闻|价格|行情|市场|状态|数据|结果)|"
    r"weather|forecast|temperature|stock|quote|market|news|score|flight|"
    r"exchange\s*rate|online|internet|latest|real[ -]?time"
    r")",
    re.IGNORECASE,
)
_FETCH_EVIDENCE_INTENT = re.compile(
    r"(?:股价|股票|股市|证券市场|指数|收盘|开盘|盘中|板块|A股|港股|美股|韩股|日股|欧股|"
    r"KOSPI|KOSDAQ|NIKKEI|NASDAQ|DOW\s*JONES|S&P\s*500|FTSE|DAX|CAC\s*40|STOXX|"
    r"市场表现|行情|新闻|stock|quote|market|index|news)",
    re.IGNORECASE,
)
_LOCAL_WORKSPACE_INTENT = re.compile(
    r"(?:工作区|仓库|代码|文件|目录|文档|测试|git|源码|写入|修改|编辑|"
    r"workspace|repository|repo|code|file|directory|document|test)",
    re.IGNORECASE,
)
_DOCUMENT_EDIT_INTENT = re.compile(
    r"(?:Word|DOCX|文档).{0,24}(?:格式|排版|字体|字号|行距|缩进|美化|规范|编辑|修改)|"
    r"(?:格式|排版|字体|字号|行距|缩进|美化|规范).{0,24}(?:Word|DOCX|文档)|"
    r"(?:format|restyle|edit).{0,24}(?:word|docx|document)",
    re.IGNORECASE,
)
_DOCUMENT_CREATE_INTENT = re.compile(
    r"(?:生成|创建|新建|导出|制作|转成|转换|转为|保存为|输出|整理为|作为)"
    r".{0,24}(?:Word|DOCX|文档)|"
    r"(?:Word|DOCX).{0,24}(?:生成|创建|新建|导出|制作|保存|输出)|"
    r"(?:图片|图像|照片|截图).{0,32}(?:识别|提取|OCR).{0,32}(?:Word|DOCX|文档)|"
    r"(?:recognize|extract|ocr|convert).{0,40}(?:word|docx|document)",
    re.IGNORECASE,
)
_PRESENTATION_CREATE_INTENT = re.compile(
    r"(?:生成|创建|新建|导出|制作|设计|输出|整理为|套用|使用).{0,32}"
    r"(?:PPTX?|PowerPoint|演示稿|演示文稿|幻灯片)|"
    r"(?:PPTX?|PowerPoint|演示稿|演示文稿|幻灯片).{0,32}"
    r"(?:生成|创建|新建|导出|制作|设计|输出|套用)|"
    r"(?:create|generate|build|export|make).{0,40}(?:pptx?|powerpoint|presentation|slides?)",
    re.IGNORECASE,
)
FALSE_WEB_DENIAL_ISSUE = "已经取得联网证据，但答复仍错误声称无法访问外部数据"
_FALSE_WEB_DENIAL = re.compile(
    r"(?:(?:我|本系统|当前系统)(?:目前|当前|暂时|现阶段)?|抱歉[，,\s]*)"
    r".{0,4}(?:无法|不能|不具备).{0,12}(?:访问|获取|查询|检索|浏览)"
    r".{0,24}(?:实时|最新|联网|互联网|外部|市场|财经)"
    r".{0,24}(?:数据|信息|行情|资讯|网站|网页)|"
    r"(?:(?:我|本系统|当前系统)(?:目前|当前|暂时|现阶段)?|抱歉[，,\s]*)"
    r".{0,4}(?:无法联网|不能联网|无法访问互联网|不具备联网能力)|"
    r"工作区.{0,16}(?:没有|不含|不存在).{0,16}(?:行情|市场|资金流)|"
    r"(?:i\s+)?(?:do\s+not|don[’']t|cannot|can[’']t)\s+have\s+enough"
    r".{0,48}(?:evidence|information|data)|"
    r"(?:i\s+)?(?:cannot|can[’']t|am\s+unable\s+to)\s+"
    r"(?:access|retrieve|compile|provide).{0,48}(?:latest|current|up[\s\-‐‑–—]*to[\s\-‐‑–—]*date)|"
    r"(?:证据|资料|信息).{0,12}(?:不足|不够).{0,24}(?:无法|不能).{0,16}(?:整理|提供|回答)",
    re.IGNORECASE,
)


def _mandatory_tool_names(context: str) -> set[str]:
    names: set[str] = set()
    if _LIVE_WEB_INTENT.search(context or ""):
        names.add("web_search")
    if _DOCUMENT_EDIT_INTENT.search(context or ""):
        names.add("document_format")
    if requires_document_artifact(context):
        names.add("document_create")
    return names


def requires_document_artifact(context: str) -> bool:
    """判断用户是否明确要求新建或导出 Word，而不把普通文档问答误判为产物任务。"""
    return bool(_DOCUMENT_CREATE_INTENT.search(context or ""))


def requires_presentation_artifact(context: str) -> bool:
    """判断用户是否明确要求生成或导出 PowerPoint 演示文稿。"""
    return bool(_PRESENTATION_CREATE_INTENT.search(context or ""))


def required_evidence_tools(context: str, available_names: set[str]) -> tuple[str, ...]:
    """由规则引擎给出必须取得的证据工具；空元组表示没有确定性要求。

    时效性外部信息至少要求 ``web_search``。即使当前 Agent 未绑定该工具也保留要求，
    让任务以“能力不足”失败，而不是在零证据下错误完成。市场、指数和新闻类任务在
    已提供 ``web_fetch`` 时进一步要求打开来源，避免只依赖搜索摘要。
    """
    if not _LIVE_WEB_INTENT.search(context or ""):
        return ()
    required = ["web_search"]
    if _FETCH_EVIDENCE_INTENT.search(context or "") and "web_fetch" in available_names:
        required.append("web_fetch")
    return tuple(required)


_EVIDENCE_TOOL_ALIASES = {
    "web_search": "web_search",
    "tavily_search": "web_search",
    "web_fetch": "web_fetch",
    "tavily_extract": "web_fetch",
}


def evidence_capability_name(value: str) -> str:
    """把实际执行工具归一化为完成门禁使用的证据能力。

    审计事件仍保留真实工具名；这里只解决内置工具与同能力 MCP 工具名称不同，
    导致已经取得正文证据却被误判失败的问题。MCP function 名会带服务名前缀，
    因而只接受明确别名本身或以下划线分隔的明确后缀。
    """
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    direct = _EVIDENCE_TOOL_ALIASES.get(normalized)
    if direct:
        return direct
    for alias, capability in _EVIDENCE_TOOL_ALIASES.items():
        if normalized.endswith("_" + alias):
            return capability
    return str(value or "").strip()


def preflight_arguments(tool_name: str, context: str) -> dict:
    """为确定性前置工具生成无歧义参数模板。"""
    if tool_name == "web_search":
        requested = re.search(r"(?:前\s*|top\s*)(\d{1,3})(?:\s*[条个篇项])?", context or "", re.I)
        count = min(20, max(8, int(requested.group(1)))) if requested else 8
        return {"query": (context or "").strip(), "count": count, "language": "zh-CN"}
    return {}


def canonical_tool_name(value: str, available_names: set[str]) -> str:
    """仅在能唯一映射到已提供工具时修复裸露的 Harmony channel 后缀。"""
    name = str(value or "").strip()
    if name in available_names:
        return name
    lowered = {item.lower(): item for item in available_names}
    for suffix in ("analysis", "commentary", "final"):
        if name.lower().endswith(suffix):
            candidate = name[:-len(suffix)].rstrip(" _:-")
            resolved = lowered.get(candidate.lower())
            if resolved:
                return resolved
    return name


def _tokens(text: str) -> set[str]:
    value = (text or "").lower()
    result = set(_WORD_RE.findall(value))
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", value)
    result.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
    return result


def route_tools(tools: list[dict], context: str, *, threshold: int, limit: int) -> list[dict]:
    """从大工具目录中确定性选出少量候选；小目录保持原样。

    分数只依赖当前目标、近期观察和工具 Schema，可复现且不额外消耗一次模型调用。
    内建控制工具始终保留，避免路由器切断渐进式 Skill 资源读取。
    """
    if len(tools) <= threshold or limit >= len(tools):
        return list(tools)
    wanted = _tokens(context)
    required_names = _mandatory_tool_names(context)
    # 时效性外部问题先进入窄 Web 工具簇，避免弱模型被目录/Git 工具的高频词吸引。
    # 当用户同时明确要求处理工作区时保留通用评分结果，支持“检索后写入文件”等复合任务。
    if "web_search" in required_names and not _LOCAL_WORKSPACE_INTENT.search(context or ""):
        web_cluster = [
            tool for tool in tools
            if str((tool.get("function") or {}).get("name") or "")
            in {"web_search", "web_fetch"}
        ]
        if web_cluster:
            return web_cluster[:limit]
    scored: list[tuple[float, int, dict]] = []
    mandatory: list[dict] = []
    for index, tool in enumerate(tools):
        function = tool.get("function") or {}
        name = str(function.get("name") or "")
        if name in {"read_skill_resource", "create_skill", "update_plan"} or name in required_names:
            mandatory.append(tool)
            continue
        parameters = function.get("parameters") or {}
        properties = " ".join(str(key) for key in (parameters.get("properties") or {}))
        description = str(function.get("description") or "")
        name_tokens = _tokens(name.replace("_", " "))
        description_tokens = _tokens(description + " " + properties)
        score = 4.0 * len(wanted & name_tokens) + 1.5 * len(wanted & description_tokens)
        # 稳定的轻微位置偏置仅用于同分排序。
        scored.append((score, -index / 10000.0, tool))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    slots = max(0, limit - min(limit, len(mandatory)))
    return mandatory[:limit] + [item[2] for item in scored[:slots]]


def _coerce(value: Any, schema: dict) -> Any:
    expected = schema.get("type")
    if isinstance(expected, list):
        expected = next((item for item in expected if item != "null"), None)
    if expected == "string" and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    if expected == "boolean" and not isinstance(value, bool):
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
    if expected == "integer" and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if expected == "number" and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if expected == "array" and not isinstance(value, list):
        return [value]
    if expected == "object" and isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else value
        except json.JSONDecodeError:
            return value
    return value


def _valid_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_valid_type(value, item) for item in expected)
    return {
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
        "null": value is None,
    }.get(expected, True)


def repair_arguments(
    schema: dict | None, arguments: Any, *, enabled: bool = True
) -> tuple[dict, list[str], list[str]]:
    """按 JSON Schema 修复常见弱模型参数错误，并返回不可修复问题。

    只做无歧义修复：默认值、常见同义字段、基础类型转换。不会臆造必填业务值。
    """
    spec = schema if isinstance(schema, dict) else {}
    properties = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}
    args = dict(arguments) if isinstance(arguments, dict) else {}
    repairs: list[str] = []
    errors: list[str] = []
    if not isinstance(arguments, dict):
        errors.append("工具参数必须是 JSON 对象")

    if enabled:
        for target in properties:
            if target in args:
                continue
            aliases = _COMMON_ALIASES.get(target, ())
            source = next((alias for alias in aliases if alias in args), None)
            if source:
                args[target] = args.pop(source)
                repairs.append(f"{source}→{target}")
        for name, field in properties.items():
            if name not in args and "default" in field:
                args[name] = field["default"]
                repairs.append(f"{name}=默认值")
            if name in args:
                before = args[name]
                args[name] = _coerce(before, field)
                if args[name] != before or type(args[name]) is not type(before):
                    repairs.append(f"{name}:类型转换")
                enum = field.get("enum")
                if isinstance(enum, list) and args[name] not in enum and isinstance(args[name], str):
                    match = next(
                        (item for item in enum if str(item).lower() == args[name].lower()), None
                    )
                    if match is not None:
                        args[name] = match
                        repairs.append(f"{name}:枚举规范化")

    for name in spec.get("required") or []:
        if name not in args or args[name] in (None, ""):
            errors.append(f"缺少必填参数 {name}")
    for name, value in args.items():
        field = properties.get(name)
        if field and not _valid_type(value, field.get("type")):
            errors.append(f"参数 {name} 类型应为 {field.get('type')}")
        if field and isinstance(field.get("enum"), list) and value not in field["enum"]:
            errors.append(f"参数 {name} 必须是 {field['enum']} 之一")
    if spec.get("additionalProperties") is False:
        unknown = [name for name in args if name not in properties]
        for name in unknown:
            args.pop(name, None)
            repairs.append(f"移除未知参数 {name}")
    return args, repairs, errors


@dataclass(frozen=True)
class Observation:
    ok: bool
    summary: str
    raw: str
    error_type: str = ""
    error_code: str = ""


def analyze_observation(result: Any, max_chars: int = 6000) -> Observation:
    """由规则引擎解析工具结果，不把大段日志直接交给模型做 Reflection。"""
    raw = str(result or "")
    structured = None
    try:
        structured = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        pass
    structured_failure = (
        isinstance(structured, dict)
        and (
            structured.get("ok") is False
            or bool(structured.get("error"))
        )
    )
    matched = "structured_error" if structured_failure else next(
        (pattern.pattern for pattern in _ERROR_PATTERNS if pattern.search(raw)), ""
    )
    ok = not matched
    error_type = ""
    error_code = ""
    if structured_failure:
        error_type = "structured_error"
        structured_error = structured.get("error")
        if isinstance(structured_error, dict):
            error_type = str(
                structured_error.get("type") or structured_error.get("name")
                or error_type
            )[:64]
            error_code = str(structured_error.get("code") or "")[:64]
        else:
            error_code = str(structured.get("code") or "")[:64]
    elif matched:
        http_match = re.search(r"\bHTTP\s*([45]\d\d)\b", raw, re.IGNORECASE)
        timeout_match = re.search(
            r"(?:超过\s*[\d.]+\s*秒|timed?\s*out|timeout)", raw, re.IGNORECASE
        )
        if timeout_match:
            error_type = "timeout"
            error_code = "timeout"
        elif http_match:
            error_type = "http_error"
            error_code = http_match.group(1)
        elif "参数校验失败" in raw:
            error_type = "argument_validation"
        elif "未知工具" in raw:
            error_type = "unknown_tool"
        else:
            error_type = "tool_error"
    clipped = raw[:max_chars]
    if len(raw) > max_chars:
        clipped += f"\n[输出已截断：原始 {len(raw)} 字符]"
    if ok:
        summary = "[结构化观察]\n状态：成功\n结果：\n" + clipped
    else:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        evidence = "\n".join(lines[-12:])[:2000]
        summary = (
            "[结构化观察]\n状态：失败\n"
            f"错误证据：\n{evidence or clipped[:2000]}\n"
            "下一步约束：不要原样重复；修正参数、选择替代工具或基于已知证据降级。"
        )
    return Observation(
        ok=ok,
        summary=summary,
        raw=clipped,
        error_type=error_type,
        error_code=error_code,
    )


def compact_tool_observations(
    messages: list[dict],
    *,
    budget_chars: int,
    compact_chars: int,
    keep_recent: int = 2,
) -> int:
    """压缩早期工具观察，返回减少的字符数。

    保留 assistant/tool 消息结构以满足 Chat Completions 协议，只缩短已经
    完成的旧工具结果；最近观察保持完整，避免弱模型根据过期状态继续规划。
    """
    tool_indexes = [
        index for index, message in enumerate(messages)
        if message.get("role") == "tool" and isinstance(message.get("content"), str)
    ]
    total = sum(len(messages[index]["content"]) for index in tool_indexes)
    if total <= budget_chars:
        return 0
    before = total
    older = tool_indexes[:-max(0, keep_recent)] if keep_recent else tool_indexes
    for index in older:
        content = messages[index]["content"]
        if len(content) <= compact_chars:
            continue
        messages[index]["content"] = (
            content[:compact_chars]
            + "\n[历史工具观察已由 Harness 压缩；如需细节请重新检索]"
        )
        total -= len(content) - len(messages[index]["content"])
        if total <= budget_chars:
            break
    # 极端情况下单个/最近工具返回已超过预算，仍做有界压缩。
    if total > budget_chars:
        for index in tool_indexes:
            content = messages[index]["content"]
            target = max(300, min(compact_chars, budget_chars // max(1, len(tool_indexes))))
            if len(content) <= target:
                continue
            messages[index]["content"] = content[:target] + "\n[工具观察已压缩]"
            total -= len(content) - len(messages[index]["content"])
            if total <= budget_chars:
                break
    return max(0, before - total)


def verify_answer(
    answer: str,
    *,
    min_chars: int = 1,
    required_terms: tuple[str, ...] = (),
    forbidden_terms: tuple[str, ...] = (),
    require_successful_tool: bool = False,
    successful_tools: int = 0,
    required_evidence_tools: tuple[str, ...] = (),
    successful_tool_names: set[str] | None = None,
) -> list[str]:
    """返回确定性的完成条件缺口；空列表表示通过。"""
    text = (answer or "").strip()
    issues: list[str] = []
    if len(text) < min_chars:
        issues.append(f"答复长度少于 {min_chars} 字符")
    for term in required_terms:
        if term not in text:
            issues.append(f"缺少必需内容：{term}")
    for term in forbidden_terms:
        if term in text:
            issues.append(f"包含禁止内容：{term}")
    if require_successful_tool and successful_tools < 1:
        issues.append("没有任何工具成功证据")
    used = {
        evidence_capability_name(name)
        for name in (successful_tool_names or set())
        if str(name or "").strip()
    }
    missing_evidence = [name for name in required_evidence_tools if name not in used]
    if missing_evidence:
        issues.append("缺少任务所需证据工具：" + "、".join(missing_evidence))
    if required_evidence_tools and not missing_evidence and _FALSE_WEB_DENIAL.search(text):
        issues.append(FALSE_WEB_DENIAL_ISSUE)
    return issues
