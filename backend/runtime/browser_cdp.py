"""无第三方浏览器驱动的最小 Chrome DevTools Protocol 客户端。

复用系统 Chrome/Edge 与已安装的 ``websockets``，提供打开、快照、点击、输入、
截图和关闭。它使用临时无痕配置，不读取用户浏览器资料。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..config import DATA_DIR, EXPORT_DIR, settings
from ..net_guard import validate_outbound_url


class BrowserError(RuntimeError):
    pass


@dataclass
class BrowserSession:
    id: str
    process: subprocess.Popen
    websocket: Any
    profile_dir: str
    owner_key: str
    sequence: int = 0
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task | None = None
    event_tasks: set[asyncio.Task] = field(default_factory=set)


SESSIONS: dict[str, BrowserSession] = {}


def _chrome() -> str:
    candidates = [
        shutil.which("chrome"),
        shutil.which("msedge"),
        os.getenv("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise BrowserError("未找到 Chrome 或 Edge；可通过 CHROME_PATH 配置")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _new_profile() -> str:
    profile_root = DATA_DIR / "browser_profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="run-", dir=profile_root)


async def _connect_websocket(url: str):
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        from websockets import connect
    return await connect(url, max_size=8 * 1024 * 1024)


async def _send(
    session: BrowserSession,
    method: str,
    params: dict | None = None,
    *,
    wait: bool,
) -> dict:
    future = None
    async with session.send_lock:
        session.sequence += 1
        message_id = session.sequence
        if wait:
            future = asyncio.get_running_loop().create_future()
            session.pending[message_id] = future
        try:
            await session.websocket.send(json.dumps({
                "id": message_id, "method": method, "params": params or {},
            }))
        except Exception:
            session.pending.pop(message_id, None)
            raise
    if future is None:
        return {}
    try:
        message = await asyncio.wait_for(
            future, timeout=settings.BROWSER_TIMEOUT_SECONDS
        )
    finally:
        session.pending.pop(message_id, None)
    if message.get("error"):
        error = message["error"]
        raise BrowserError(error.get("message") or str(error))
    return message.get("result") or {}


async def _handle_paused_request(session: BrowserSession, paused: dict) -> None:
    """在浏览器实际发包前校验每个主文档、重定向和子资源请求。"""
    request = paused.get("request") or {}
    request_id = paused.get("requestId")
    if not request_id:
        return
    try:
        await asyncio.to_thread(
            validate_outbound_url, str(request.get("url") or "")
        )
        method = "Fetch.continueRequest"
        params = {"requestId": request_id}
    except Exception:
        # 校验器异常时同样失败关闭，不能因 DNS/解析故障放行请求。
        method = "Fetch.failRequest"
        params = {"requestId": request_id, "errorReason": "BlockedByClient"}
    try:
        await _send(session, method, params, wait=False)
    except Exception:
        # 会话关闭时请求可能仍在队列中；关闭流程负责回收整个浏览器进程。
        pass


async def _reader(session: BrowserSession) -> None:
    """持续泵送 CDP 消息，避免后台导航在没有工具命令时绕过或卡住拦截。"""
    try:
        while True:
            raw = await session.websocket.recv()
            message = json.loads(raw)
            if message.get("method") == "Fetch.requestPaused":
                task = asyncio.create_task(
                    _handle_paused_request(session, message.get("params") or {})
                )
                session.event_tasks.add(task)
                task.add_done_callback(session.event_tasks.discard)
                continue
            message_id = message.get("id")
            future = session.pending.get(message_id)
            if future is not None and not future.done():
                future.set_result(message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = BrowserError(f"浏览器调试连接已断开：{exc}")
        for future in list(session.pending.values()):
            if not future.done():
                future.set_exception(error)


async def _command(session: BrowserSession, method: str, params: dict | None = None) -> dict:
    if session.reader_task is None:
        session.reader_task = asyncio.create_task(_reader(session))
    return await _send(session, method, params, wait=True)


async def _evaluate(session: BrowserSession, expression: str) -> Any:
    result = await _command(session, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
    })
    exception = result.get("exceptionDetails")
    if exception:
        raise BrowserError(exception.get("text") or "页面脚本执行失败")
    return (result.get("result") or {}).get("value")


async def _wait_ready(session: BrowserSession) -> None:
    deadline = asyncio.get_running_loop().time() + settings.BROWSER_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = await _evaluate(session, "document.readyState")
            if state in {"interactive", "complete"}:
                return
        except Exception:
            pass
        await asyncio.sleep(.15)
    raise BrowserError("等待页面加载超时")


async def open_page(url: str, owner_key: str = "") -> BrowserSession:
    await asyncio.to_thread(validate_outbound_url, url)
    port = _port()
    profile = _new_profile()
    process = subprocess.Popen(
        [
        _chrome(),
        "--headless=new",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--no-first-run",
        "--no-default-browser-check",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    target = None
    session = None
    try:
        async with httpx.AsyncClient(timeout=.5) as client:
            for _ in range(60):
                if process.poll() is not None:
                    raise BrowserError(
                        f"浏览器进程启动失败（退出码 {process.returncode}）"
                    )
                try:
                    response = await client.get(endpoint + "/json")
                    pages = response.json()
                    target = next((row for row in pages if row.get("type") == "page"), None)
                    if target:
                        break
                except Exception:
                    pass
                await asyncio.sleep(.1)
        if not target:
            raise BrowserError("浏览器调试端口启动失败")
        websocket = await _connect_websocket(target["webSocketDebuggerUrl"])
        session = BrowserSession(
            uuid.uuid4().hex[:16], process, websocket, profile, owner_key
        )
        SESSIONS[session.id] = session
        await _command(session, "Page.enable")
        await _command(session, "Runtime.enable")
        await _command(session, "Fetch.enable", {
            "patterns": [{"urlPattern": "*", "requestStage": "Request"}],
        })
        await _command(session, "Page.navigate", {"url": url})
        await _wait_ready(session)
        return session
    except Exception:
        if session is not None:
            await close(session)
        else:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(process.wait, 5)
            with contextlib.suppress(Exception):
                shutil.rmtree(profile)
        raise


def get(session_id: str, owner_key: str = "") -> BrowserSession:
    session = SESSIONS.get(session_id)
    if not session or session.process.poll() is not None:
        raise BrowserError("浏览器会话不存在或已经关闭")
    if owner_key and session.owner_key != owner_key:
        raise BrowserError("浏览器会话不属于当前运行")
    return session


async def snapshot(session: BrowserSession) -> dict:
    expression = r"""(() => {
      const nodes = [...document.querySelectorAll(
        'a[href],button,input,textarea,select,[role="button"],[onclick]'
      )].slice(0, 200);
      const elements = nodes.map((el, index) => {
        const id = String(index);
        el.setAttribute('data-harness-id', id);
        return {
          id,
          tag: el.tagName.toLowerCase(),
          type: el.getAttribute('type') || '',
          text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 300),
          href: el.href || '',
          disabled: !!el.disabled
        };
      });
      return {
        url: location.href,
        title: document.title || '',
        text: (document.body?.innerText || '').slice(0, 20000),
        elements
      };
    })()"""
    value = await _evaluate(session, expression)
    return {"session_id": session.id, **(value or {})}


async def click(session: BrowserSession, element_id: str) -> dict:
    # 先读取绝对链接并执行 SSRF 校验；非链接控件只允许当前页面内事件。
    href = await _evaluate(
        session,
        f"document.querySelector('[data-harness-id={json.dumps(str(element_id))}]')?.href || ''",
    )
    if href:
        await asyncio.to_thread(validate_outbound_url, href)
    clicked = await _evaluate(session, f"""(() => {{
      const el = document.querySelector('[data-harness-id={json.dumps(str(element_id))}]');
      if (!el) return false;
      el.click();
      return true;
    }})()""")
    if not clicked:
        raise BrowserError("element_id 不存在；请重新获取 snapshot")
    await asyncio.sleep(.3)
    await _wait_ready(session)
    current_url = str(await _evaluate(session, "location.href") or "")
    await asyncio.to_thread(validate_outbound_url, current_url)
    return await snapshot(session)


async def type_text(
    session: BrowserSession, element_id: str, text: str, submit: bool = False
) -> dict:
    expression = f"""(() => {{
      const el = document.querySelector('[data-harness-id={json.dumps(str(element_id))}]');
      if (!el) return false;
      el.focus();
      const setter = Object.getOwnPropertyDescriptor(
        Object.getPrototypeOf(el), 'value'
      )?.set;
      if (setter) setter.call(el, {json.dumps(text)});
      else el.value = {json.dumps(text)};
      el.dispatchEvent(new Event('input', {{bubbles:true}}));
      el.dispatchEvent(new Event('change', {{bubbles:true}}));
      if ({json.dumps(bool(submit))}) {{
        if (el.form) el.form.requestSubmit();
        else el.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', bubbles:true}}));
      }}
      return true;
    }})()"""
    if not await _evaluate(session, expression):
        raise BrowserError("element_id 不存在；请重新获取 snapshot")
    if submit:
        await asyncio.sleep(.3)
        await _wait_ready(session)
        current_url = str(await _evaluate(session, "location.href") or "")
        await asyncio.to_thread(validate_outbound_url, current_url)
    return await snapshot(session)


async def screenshot(session: BrowserSession) -> dict:
    result = await _command(session, "Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": False,
    })
    data = result.get("data")
    if not data:
        raise BrowserError("浏览器没有返回截图")
    name = f"browser_{uuid.uuid4().hex[:10]}.png"
    (EXPORT_DIR / name).write_bytes(base64.b64decode(data))
    return {"file": name, "download_url": f"/api/v1/exports/{name}"}


async def close(session: BrowserSession) -> None:
    SESSIONS.pop(session.id, None)
    try:
        await session.websocket.close()
    finally:
        if session.reader_task is not None:
            session.reader_task.cancel()
        for task in list(session.event_tasks):
            task.cancel()
        await asyncio.gather(
            *(
                [session.reader_task] if session.reader_task is not None else []
            ),
            *list(session.event_tasks),
            return_exceptions=True,
        )
        if session.process.poll() is None:
            session.process.terminate()
            try:
                await asyncio.to_thread(session.process.wait, 3)
            except subprocess.TimeoutExpired:
                session.process.kill()
                await asyncio.to_thread(session.process.wait, 3)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(shutil.rmtree, session.profile_dir)


async def close_owner(owner_key: str) -> None:
    """强制关闭某个 Turn 遗留的所有会话并删除其一次性 Profile。"""
    owned = [
        session for session in list(SESSIONS.values())
        if session.owner_key == owner_key
    ]
    if owned:
        await asyncio.gather(*(close(session) for session in owned),
                             return_exceptions=True)


async def close_all() -> None:
    await asyncio.gather(*(close(session) for session in list(SESSIONS.values())),
                         return_exceptions=True)
