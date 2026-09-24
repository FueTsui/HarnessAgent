"""最小 MCP（Model Context Protocol）客户端：列出并调用 MCP 服务的工具。

支持三种传输：
- http  Streamable HTTP（现行 MCP 标准，推荐）：JSON-RPC 直接 POST 到同一 url，
        响应可能是 application/json 或 text/event-stream。
- sse   旧式 HTTP+SSE 两通道：先 GET url 拿 endpoint 事件给出的消息地址，
        再 POST 请求、从 SSE 流读取对应 id 的响应。
- stdio root 授权的本地进程：以 UTF-8 换行 JSON-RPC 完成握手、工具发现与调用；
        Windows 隐藏窗口，并在退出、超时或取消时清理进程树。

HTTP/SSE 依赖 httpx，stdio 使用标准库子进程与线程；本地服务须有可用的运行时。
异常统一抛 RuntimeError（含原因），由调用方决定降级策略。
工具在对话循环中以 `服务名__工具名` 命名空间暴露给模型。
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from typing import Any, Optional

import httpx

from ..net_guard import validate_outbound_url

logger = logging.getLogger(__name__)

# MCP 的 url / 请求头里允许写 ${ENV_VAR} 占位符，发请求时从环境变量（含 .env）替换，
# 便于把 API Key 等密钥放进 .env 而不落库、不随导出泄露。
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "green-carbon-agent", "version": "1.0.0"}
MCP_TIMEOUT = 30.0
NAMESPACE_SEP = "__"

_READ_ONLY_TOOL_RE = re.compile(
    r"(?:^|[_\-\s])(get|list|read|search|fetch|query|find|lookup|inspect|describe|status|"
    r"health|weather|news|quote|download)(?:$|[_\-\s])",
    re.IGNORECASE,
)
_MUTATING_TOOL_RE = re.compile(
    r"(?:^|[_\-\s])(create|add|insert|update|edit|write|delete|remove|move|send|post|"
    r"publish|submit|approve|reject|cancel|upload|execute|run|trigger)(?:$|[_\-\s])",
    re.IGNORECASE,
)
_IDEMPOTENCY_FIELDS = ("idempotency_key", "idempotencyKey", "request_id", "requestId")
_SENSITIVE_ARGUMENT_RE = re.compile(
    r"(?:token|secret|password|authorization|api[_-]?key|credential|cookie)",
    re.IGNORECASE,
)


def tool_risk_metadata(tool: dict, *, risk_policy: str = "auto") -> dict:
    """把 MCP annotations 与工具语义收敛为统一风险元数据。

    MCP 未声明 annotations 时采用保守策略：明确只读动词按只读处理；明确写动词或
    无法判断的工具按有副作用处理，避免未知外部能力绕过审批。
    """
    annotations = tool.get("annotations") or {}
    name = str(tool.get("name") or "")
    description = str(tool.get("description") or "")
    semantic = f"{name} {description}".lower()
    read_only_hint = annotations.get("readOnlyHint")
    destructive = bool(annotations.get("destructiveHint"))
    if destructive or read_only_hint is False:
        mutating = True
        source = "annotation"
    elif read_only_hint is True:
        mutating = False
        source = "annotation"
    elif _MUTATING_TOOL_RE.search(semantic):
        mutating = True
        source = "heuristic"
    elif risk_policy == "read_only":
        # 这是显式的管理员信任边界，而不是按服务名硬编码。具有写入动词、
        # destructiveHint 或 readOnlyHint=false 的工具仍在上方被判为写操作。
        mutating = False
        source = "server_policy"
    elif _READ_ONLY_TOOL_RE.search(semantic):
        mutating = False
        source = "heuristic"
    else:
        mutating = True
        source = "safe_default"
    return {
        "mutating": mutating,
        "destructive": destructive,
        "idempotent": bool(annotations.get("idempotentHint")),
        "risk": "high" if destructive else ("write" if mutating else "read"),
        "classification_source": source,
    }


def inject_idempotency_key(
    arguments: dict,
    input_schema: dict,
    *,
    seed: str,
) -> tuple[dict, str | None]:
    """仅在远端 Schema 明确支持时注入稳定幂等键，避免违反 additionalProperties。"""
    properties = (input_schema or {}).get("properties") or {}
    field = next((name for name in _IDEMPOTENCY_FIELDS if name in properties), None)
    if field is None or arguments.get(field):
        return dict(arguments), field if arguments.get(field) else None
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value = hashlib.sha256(f"{seed}|{canonical}".encode("utf-8")).hexdigest()
    updated = dict(arguments)
    updated[field] = value
    return updated, field


def safe_argument_preview(arguments: dict, limit: int = 240) -> str:
    """生成审批界面的目标预览，排除凭据并限制长度。"""
    visible = {
        str(key): ("<redacted>" if _SENSITIVE_ARGUMENT_RE.search(str(key)) else value)
        for key, value in list((arguments or {}).items())[:8]
    }
    text = json.dumps(visible, ensure_ascii=False, default=str, sort_keys=True)
    return text[:limit] + ("…" if len(text) > limit else "")


async def _validate_request(request: httpx.Request) -> None:
    """httpx 会对初始请求和每一跳重定向触发 request hook。"""
    await asyncio.to_thread(validate_outbound_url, str(request.url))


def _safe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=MCP_TIMEOUT,
        follow_redirects=True,
        event_hooks={"request": [_validate_request]},
    )


def _load_headers(raw: Any) -> dict:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _win_registry_env(name: str) -> Optional[str]:
    """Windows：从注册表读取「用户级」「系统级」环境变量（HKCU\\Environment、HKLM\\…\\Environment）。

    用途：Windows 上通过「系统属性 → 环境变量」新设的变量不会注入到已在运行的进程；
    若服务进程启动时未继承到该变量（.env 也未配置），这里直接读注册表兜底，
    使新设的系统/用户环境变量无需重启服务即可被 ${VAR} 取到。非 Windows 返回 None。
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # 极少数裁剪环境无 winreg
        return None
    # 先用户级、后系统级（与 Windows 解析顺序一致：用户变量优先）
    for root, path in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(root, path) as key:
                val, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue  # 该作用域无此变量
        if val:
            # REG_EXPAND_SZ 形式可能含 %OTHER% 引用，按当前环境展开后返回
            return os.path.expandvars(str(val))
    return None


def resolve_env_var(name: str) -> Optional[str]:
    """解析单个环境变量：优先进程环境（含 .env 经 setdefault 注入），其次 Windows 用户/系统级环境变量。

    返回 None 表示三处皆未设置（调用方据此告警/降级）。空串视为「未设置」，继续向后兜底。
    """
    val = os.environ.get(name)
    if val:
        return val
    return _win_registry_env(name)


def _expand_env(value: str) -> str:
    """把字符串里的 ${VAR} 替换为环境变量值；未定义的变量替换为空串并告警一次。

    取值顺序见 resolve_env_var：进程环境（.env）→ Windows 用户/系统级环境变量（注册表）。
    """
    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        val = resolve_env_var(name)
        if val is None:
            logger.warning(
                "MCP 配置引用了未设置的环境变量 ${%s}（进程环境/.env/系统环境变量均无），已替换为空串", name
            )
            return ""
        return val
    return _ENV_REF_RE.sub(_sub, value or "")


def resolve_endpoint(server) -> tuple[str, dict]:
    """解析 MCP 服务实际的 url 与请求头，展开其中的 ${ENV_VAR} 占位符（从环境变量/.env 取值）。"""
    url = _expand_env(server.url or "")
    headers = {k: _expand_env(v) for k, v in _load_headers(server.headers).items()}
    return url, headers


def flatten_tool_result(result: dict) -> str:
    """把 MCP tools/call 结果（content 数组）压平为字符串供模型阅读。"""
    if not isinstance(result, dict):
        return str(result)
    if "structuredContent" in result:
        # 保留机器可读结果和 isError。旧文本调用者仍收到 str，但证据不会被丢弃。
        return json.dumps(result, ensure_ascii=False)
    parts: list[str] = []
    for item in result.get("content", []) or []:
        if not isinstance(item, dict):
            parts.append(str(item))
        elif item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    text = "\n".join(p for p in parts if p)
    if result.get("isError"):
        text = f"[工具返回错误] {text}"
    return text or json.dumps(result, ensure_ascii=False)


# ---------- Local stdio transport ----------

_STDIO_MAX_MESSAGE = 4 * 1024 * 1024
_STDIO_STDERR_LIMIT = 4096
_STDIO_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "LANG", "LC_ALL", "LC_CTYPE",
}


def _json_field(raw, expected, default):
    if isinstance(raw, expected):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            if isinstance(value, expected):
                return value
        except (ValueError, TypeError):
            pass
    return default


def validate_stdio_config(command: str, args: list, env: dict, cwd: str) -> None:
    if not command or not command.strip() or any(char in command for char in "\0\r\n"):
        raise ValueError("stdio 必须填写已安装的可执行文件路径或名称")
    if len(args) > 100 or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 65536 for arg in args):
        raise ValueError("stdio 参数必须是字符串数组，最多 100 项，且不得包含空字符")
    if len(env) > 100 or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key))
        or not isinstance(value, str) or "\0" in value or len(value) > 65536
        for key, value in env.items()
    ):
        raise ValueError("stdio 环境变量须为有效名称与字符串值，最多 100 项")
    if any(char in cwd for char in "\0\r\n"):
        raise ValueError("stdio 工作目录无效")


def stdio_environment(configured: dict) -> dict:
    # Never inherit the application .env wholesale. Only deployment basics and
    # explicitly configured values are passed to the authorized subprocess.
    result = {key: value for key, value in os.environ.items() if key.upper() in _STDIO_ENV_KEYS}
    result.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    result.update({key: _expand_env(value) for key, value in configured.items()})
    return result


def resolve_stdio_argv(command: str, args: list[str], environment: dict) -> list[str]:
    """Resolve an explicitly authorized command without invoking a shell.

    npm ships Windows .cmd wrappers. Execute its installed JS entry point with
    Node directly; never interpret arbitrary batch files or change root's args.
    """
    search_path = next((value for key, value in environment.items() if key.upper() == "PATH"), None)
    resolved = shutil.which(command, path=search_path)
    if not resolved:
        raise RuntimeError("stdio 可执行文件不存在；请先在服务器部署该 MCP 服务")
    if os.name != "nt" or os.path.splitext(resolved)[1].lower() not in {".cmd", ".bat"}:
        return [resolved, *args]
    wrapper = os.path.basename(resolved).lower()
    if wrapper not in {"npm.cmd", "npx.cmd"}:
        raise RuntimeError("stdio 不通过命令解释器启动 .cmd/.bat；请使用 node.exe、python.exe 或服务的 .exe")
    wrapper_dir = os.path.dirname(os.path.abspath(resolved))
    node = os.path.join(wrapper_dir, "node.exe")
    if not os.path.isfile(node):
        node = shutil.which("node.exe", path=search_path)
    entry_name = "npx-cli.js" if wrapper == "npx.cmd" else "npm-cli.js"
    candidates = [os.path.join(wrapper_dir, "node_modules", "npm", "bin", entry_name)]
    if node:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(node)), "node_modules", "npm", "bin", entry_name))
    entry = next((path for path in candidates if os.path.isfile(path)), None)
    if not node or not os.path.isfile(node) or not entry:
        raise RuntimeError("无法解析已安装的 npm/npx 入口；请将可执行程序设为 node.exe 的完整路径，并在参数中填写已安装的 npm-cli.js 或 npx-cli.js 路径")
    return [node, entry, *args]


class _WindowsProcessJob:
    """A kill-on-close Windows Job keeps descendants owned after parent exit."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise RuntimeError("无法建立 stdio 子进程隔离作业")
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            self.close()
            raise RuntimeError("无法将 stdio 子进程纳入可清理的进程树")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def attach_windows_process_tree(process):
    """Attach a Popen to kill-on-close ownership; fail closed on Windows.

    Returns None on other platforms, where callers must create and clean their
    own process group. The returned Windows owner's close() is idempotent.
    """
    if os.name != "nt":
        return None
    try:
        return _WindowsProcessJob(process)
    except BaseException:
        if process.poll() is None:
            try:
                subprocess.run(
                    [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False,
                )
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        raise


class _StdioSession:
    """Newline JSON-RPC using blocking pipes off the event loop.

    This also works with Windows Selector loops, which do not implement asyncio
    subprocess transports. Request cancellation closes the whole owned process
    tree so no pipe reader or unapproved background server remains running.
    """
    def __init__(self, server):
        self.server = server
        self.process = None
        self._job = None
        self._id = 0
        self._lock = asyncio.Lock()
        self._stderr_thread = None
        self.stderr_tail = bytearray()
        self._closed = False

    def _spawn(self):
        if not getattr(self.server, "stdio_authorized", False):
            raise RuntimeError("stdio 服务尚未经 root 授权，不能启动本地进程")
        command = str(getattr(self.server, "command", "") or "")
        args = _json_field(getattr(self.server, "args", "[]"), list, [])
        env = _json_field(getattr(self.server, "env", "{}"), dict, {})
        cwd = str(getattr(self.server, "cwd", "") or "")
        validate_stdio_config(command, args, env, cwd)
        child_env = stdio_environment(env)
        args = [_expand_env(value) for value in args]
        argv = resolve_stdio_argv(command, args, child_env)
        if cwd and not os.path.isdir(cwd):
            raise RuntimeError("stdio 工作目录不存在")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            self.process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=child_env, cwd=cwd or None,
                shell=False, bufsize=-1, **options,
            )
            if os.name == "nt":
                self._job = attach_windows_process_tree(self.process)
        except BaseException:
            self._close_blocking()
            raise
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True, name="mcp-stderr")
        self._stderr_thread.start()

    def _drain_stderr(self):
        try:
            while True:
                chunk = self.process.stderr.read(1024)
                if not chunk:
                    return
                self.stderr_tail.extend(chunk)
                if len(self.stderr_tail) > _STDIO_STDERR_LIMIT:
                    del self.stderr_tail[:-_STDIO_STDERR_LIMIT]
        except (OSError, ValueError):
            pass

    def _write(self, body):
        wire = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(wire) > _STDIO_MAX_MESSAGE:
            raise RuntimeError("MCP stdio 请求超过消息大小上限")
        self.process.stdin.write(wire)
        self.process.stdin.flush()

    def _exchange(self, body, expect):
        try:
            self._write(body)
            if not expect:
                return {}
            while True:
                raw = self.process.stdout.readline(_STDIO_MAX_MESSAGE + 1)
                if not raw:
                    raise RuntimeError("MCP stdio 服务已退出或关闭输出")
                if len(raw) > _STDIO_MAX_MESSAGE or not raw.endswith(b"\n"):
                    raise RuntimeError("MCP stdio 响应超过上限或缺少换行分隔")
                try:
                    item = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError) as exc:
                    raise RuntimeError("MCP stdio 标准输出包含无效 JSON-RPC；日志应写入 stderr") from exc
                if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
                    raise RuntimeError("MCP stdio 收到无效 JSON-RPC 消息")
                if "method" in item:
                    if "id" in item:
                        reply = {"jsonrpc": "2.0", "id": item["id"]}
                        if item["method"] == "ping":
                            reply["result"] = {}
                        else:
                            reply["error"] = {"code": -32601, "message": "Client capability not supported"}
                        self._write(reply)
                    continue
                if item.get("id") != body.get("id"):
                    continue
                if "error" in item:
                    # A server can echo credentials in errors; do not propagate it.
                    raise RuntimeError("MCP stdio 服务返回 JSON-RPC 错误")
                result = item.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("MCP stdio 返回的 result 必须是对象")
                return result
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RuntimeError("MCP stdio 管道连接已关闭") from exc

    async def _request(self, method, params, expect=True):
        async with self._lock:
            if self._closed or self.process is None:
                raise RuntimeError("MCP stdio 连接已关闭")
            body = {"jsonrpc": "2.0", "method": method, "params": params}
            if expect:
                self._id += 1
                body["id"] = self._id
            try:
                return await asyncio.wait_for(asyncio.to_thread(self._exchange, body, expect), MCP_TIMEOUT)
            except BaseException as exc:
                await self.aclose(graceful=False)
                if isinstance(exc, asyncio.TimeoutError):
                    raise RuntimeError("MCP stdio 请求超时，已终止该服务进程树") from exc
                raise

    async def initialize(self):
        # Popen is quick and synchronous so cancellation cannot orphan a process
        # spawned after a cancelled to_thread call has returned to its caller.
        self._spawn()
        result = await self._request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": _CLIENT_INFO})
        if result.get("protocolVersion") not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
            await self.aclose()
            raise RuntimeError("MCP stdio 服务返回不支持的协议版本")
        await self._request("notifications/initialized", {}, expect=False)
        return result

    async def list_tools(self):
        tools, cursor, seen = [], None, set()
        for _ in range(100):
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools", [])
            if not isinstance(page, list) or any(not isinstance(tool, dict) for tool in page):
                raise RuntimeError("MCP stdio 工具目录格式无效")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen:
                raise RuntimeError("MCP stdio 工具目录返回重复或无效游标")
            seen.add(cursor)
        raise RuntimeError("MCP stdio 工具目录页数超过上限")

    async def call_tool(self, name, arguments):
        return await self._request("tools/call", {"name": name, "arguments": arguments or {}})

    def _close_blocking(self, graceful=False):
        process = self.process
        if process is None:
            return
        if graceful and not self._lock.locked() and process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=0.3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        # Closing the owned job/process group kills descendants even if the MCP
        # parent has already exited. Never target unrelated host processes.
        if self._job is not None:
            self._job.close()
            self._job = None
        elif os.name == "nt" and process.poll() is None:
            subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"), "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        if self._stderr_thread and self._stderr_thread is not threading.current_thread():
            self._stderr_thread.join(timeout=1)

    async def aclose(self, graceful=True):
        if self._closed:
            return
        self._closed = True
        cleanup = asyncio.create_task(asyncio.to_thread(self._close_blocking, graceful))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


# ---------- Streamable HTTP 传输 ----------

def _extract_result(resp: httpx.Response) -> dict:
    """从 HTTP 响应（json 或 sse）提取 JSON-RPC 的 result，遇 error 抛出。"""
    ctype = resp.headers.get("content-type", "")
    payload: Optional[dict] = None
    if "text/event-stream" in ctype:
        for block in resp.text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                ln[5:].lstrip() for ln in block.split("\n") if ln.startswith("data:")
            )
            if not data:
                continue
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                payload = msg
    else:
        payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("MCP 响应解析失败")
    if payload.get("error"):
        raise RuntimeError(f"MCP 错误：{payload['error'].get('message', payload['error'])}")
    return payload.get("result", {})


class _HttpSession:
    def __init__(self, url: str, headers: dict) -> None:
        self.url = url
        self.headers = {
            **headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id: Optional[str] = None
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _post(self, client: httpx.AsyncClient, payload: dict, expect: bool = True) -> dict:
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = await client.post(self.url, json=payload, headers=headers)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        resp.raise_for_status()
        return _extract_result(resp) if expect else {}

    async def initialize(self, client: httpx.AsyncClient) -> dict:
        result = await self._post(client, {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": _CLIENT_INFO},
        })
        try:
            await self._post(
                client,
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                expect=False,
            )
        except Exception as exc:  # noqa: BLE001 - 通知失败不致命
            logger.debug("initialized 通知发送失败: %s", exc)
        return result

    async def list_tools(self, client: httpx.AsyncClient) -> list[dict]:
        return (await self._post(client, {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {},
        })).get("tools", [])

    async def call_tool(self, client: httpx.AsyncClient, name: str, arguments: dict) -> dict:
        return await self._post(client, {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })


# ---------- 旧式 SSE 传输 ----------

class _SseSession:
    def __init__(self, client: httpx.AsyncClient, sse_url: str, headers: dict) -> None:
        self.client = client
        self.sse_url = sse_url
        self.headers = headers
        self.endpoint: Optional[httpx.URL] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._endpoint_ready = asyncio.Event()
        self._stream_cm = None
        self._reader: Optional[asyncio.Task] = None
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def __aenter__(self) -> "_SseSession":
        self._stream_cm = self.client.stream(
            "GET", self.sse_url, headers={**self.headers, "Accept": "text/event-stream"}
        )
        resp = await self._stream_cm.__aenter__()
        resp.raise_for_status()
        self._reader = asyncio.create_task(self._read_loop(resp))
        await asyncio.wait_for(self._endpoint_ready.wait(), timeout=MCP_TIMEOUT)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
        if self._stream_cm:
            try:
                await self._stream_cm.__aexit__(*exc)
            except Exception:  # noqa: BLE001
                pass

    async def _read_loop(self, resp: httpx.Response) -> None:
        event, data_lines = None, []
        try:
            async for line in resp.aiter_lines():
                if line == "":
                    if data_lines:
                        data = "\n".join(data_lines)
                        if event == "endpoint":
                            self.endpoint = httpx.URL(self.sse_url).join(data)
                            self._endpoint_ready.set()
                        else:
                            try:
                                self._queue.put_nowait(json.loads(data))
                            except json.JSONDecodeError:
                                pass
                    event, data_lines = None, []
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        except Exception as exc:  # noqa: BLE001
            logger.debug("SSE 读取结束: %s", exc)

    async def _request(self, method: str, params: dict, expect: bool = True) -> dict:
        rid = self._next_id() if expect else None
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if rid is not None:
            body["id"] = rid
        resp = await self.client.post(
            self.endpoint, json=body, headers={**self.headers, "Content-Type": "application/json"}
        )
        resp.raise_for_status()
        if not expect:
            return {}
        while True:
            msg = await asyncio.wait_for(self._queue.get(), timeout=MCP_TIMEOUT)
            if isinstance(msg, dict) and msg.get("id") == rid:
                if msg.get("error"):
                    raise RuntimeError(f"MCP 错误：{msg['error'].get('message', msg['error'])}")
                return msg.get("result", {})

    async def initialize(self) -> dict:
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": _CLIENT_INFO,
        })
        try:
            await self._request("notifications/initialized", {}, expect=False)
        except Exception:  # noqa: BLE001
            pass
        return result

    async def list_tools(self) -> list[dict]:
        return (await self._request("tools/list", {})).get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._request("tools/call", {"name": name, "arguments": arguments or {}})


# ---------- 对外接口 ----------

async def list_tools(server) -> list[dict]:
    """列出某 MCP 服务的工具（连通性测试也用它）。返回 MCP 原始 tool 定义数组。"""
    if server.transport == "stdio":
        async with McpConnection(server) as connection:
            return await connection.list_tools()
    url, headers = resolve_endpoint(server)
    await asyncio.to_thread(validate_outbound_url, url)
    async with _safe_client() as client:
        if server.transport == "sse":
            async with _SseSession(client, url, headers) as sess:
                await sess.initialize()
                return await sess.list_tools()
        sess = _HttpSession(url, headers)
        await sess.initialize(client)
        return await sess.list_tools(client)


async def call_tool(server, name: str, arguments: dict) -> str:
    """调用 MCP 工具，返回压平后的文本结果。"""
    if server.transport == "stdio":
        async with McpConnection(server) as connection:
            return await connection.call_tool(name, arguments)
    url, headers = resolve_endpoint(server)
    await asyncio.to_thread(validate_outbound_url, url)
    async with _safe_client() as client:
        if server.transport == "sse":
            async with _SseSession(client, url, headers) as sess:
                await sess.initialize()
                return flatten_tool_result(await sess.call_tool(name, arguments))
        sess = _HttpSession(url, headers)
        await sess.initialize(client)
        return flatten_tool_result(await sess.call_tool(client, name, arguments))


class McpConnection:
    """单连接复用（M2）：一次对话内对某 MCP 服务保持 httpx 客户端 + 已初始化会话，
    避免每次 tools/call 都重新 initialize + 建连。用作异步上下文管理器。"""

    def __init__(self, server) -> None:
        self.server = server
        self.name = server.name
        self._client: Optional[httpx.AsyncClient] = None
        self._session = None
        self._sse = False
        self._stdio = False

    async def __aenter__(self) -> "McpConnection":
        if self.server.transport == "stdio":
            self._stdio = True
            self._session = _StdioSession(self.server)
            try:
                await self._session.initialize()
            except BaseException:
                await self.aclose()
                raise
            return self
        url, headers = resolve_endpoint(self.server)
        await asyncio.to_thread(validate_outbound_url, url)
        self._client = _safe_client()
        try:
            if self.server.transport == "sse":
                self._sse = True
                self._session = _SseSession(self._client, url, headers)
                await self._session.__aenter__()
                await self._session.initialize()
            else:
                self._session = _HttpSession(url, headers)
                await self._session.initialize(self._client)
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._stdio and self._session is not None:
            await self._session.aclose()
        if self._sse and self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    async def list_tools(self) -> list[dict]:
        if self._sse or self._stdio:
            return await self._session.list_tools()
        return await self._session.list_tools(self._client)

    async def call_tool(self, name: str, arguments: dict) -> str:
        return flatten_tool_result(await self.call_tool_result(name, arguments))

    async def call_tool_result(self, name: str, arguments: dict) -> dict:
        """返回 MCP 原始信封供运行时判断 isError 和 structuredContent。"""
        if self._sse or self._stdio:
            return await self._session.call_tool(name, arguments)
        return await self._session.call_tool(self._client, name, arguments)


# OpenAI / 兼容接口对 function 名的约束：^[a-zA-Z0-9_-]{1,64}$。
# MCP 服务名可能含空格/中文（如「Tavily 联网搜索」），直接拼进 function 名会被上游拒绝，
# 因此暴露给模型的名字必须先规整；调用回溯则走 build_tool_specs 返回的路由表，不再靠字符串拆分。
_FUNC_NAME_BAD_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def sanitize_function_name(raw: str, used: set) -> str:
    """规整为合法且在本次对话内唯一的 function 名。"""
    base = _FUNC_NAME_BAD_RE.sub("_", raw or "").strip("_") or "tool"
    base = base[:60]
    name = base
    i = 1
    while name in used:
        i += 1
        name = f"{base[:60 - len(str(i)) - 1]}_{i}"
    used.add(name)
    return name


def build_tool_specs(
    server_name: str,
    mcp_tools: list[dict],
    used_names: set,
    *,
    risk_policy: str = "auto",
) -> list[dict]:
    """把某服务的 MCP 工具转成 OpenAI tools 规格，function 名经规整且全局唯一。

    返回项含 `_orig_name`（原始 MCP 工具名，用于回调）；调用方据此建立 function名→(服务,工具) 路由。
    """
    out = []
    for tool in mcp_tools:
        name = tool.get("name")
        if not name:
            continue
        func_name = sanitize_function_name(f"{server_name}_{name}", used_names)
        out.append({
            "type": "function",
            "_orig_name": name,
            "_risk": tool_risk_metadata(tool, risk_policy=risk_policy),
            "_input_schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
            "_output_schema": tool.get("outputSchema"),
            "function": {
                "name": func_name,
                "description": (tool.get("description") or name)[:1024],
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
            },
        })
    return out


def to_openai_tools(server_name: str, mcp_tools: list[dict]) -> list[dict]:
    """兼容旧用法：把 MCP 工具转成 OpenAI tools 数组（function 名经规整）。"""
    return [
        {"type": s["type"], "function": s["function"]}
        for s in build_tool_specs(server_name, mcp_tools, set())
    ]


def split_namespaced(tool_name: str) -> tuple[str, str]:
    """`服务名__工具名` -> (服务名, 工具名)。仅用于历史路径；新路径走显式路由表。"""
    server, _, tool = tool_name.partition(NAMESPACE_SEP)
    return server, tool
