"""腾讯微信个人消息渠道运行时。

协议与 ``@tencent-weixin/openclaw-weixin`` 2.x 保持一致：二维码授权取得
``ilink_bot_token``，随后通过 getUpdates 长轮询收消息、sendMessage 回消息。
本模块只实现本应用需要的文本/语音转写消息路径；媒体凭据不会暴露给浏览器。

隔离边界是 ``Channel.created_by + account_id + account_user_id``：
- 每个平台用户至多由自己的 Channel 管理凭据；
- 只处理本次扫码身份发来的私信；
- 消息进入该平台用户拥有的 Thread，并路由到 Channel 绑定的 Agent。
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import io
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from . import jobs
from .database import SessionLocal
from .models import Agent, Channel, User, iso_utc
from .runtime import TaskInput

logger = logging.getLogger(__name__)

CHANNEL_TYPE = "openclaw_weixin"
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 6)
LOGIN_TTL_SECONDS = 8 * 60
MAX_TEXT_CHARS = 4000
_TERMINAL_LOGIN = {"connected", "error", "expired", "already_connected"}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _allowed_base_url(value: str | None) -> str:
    """只接受腾讯微信 HTTPS 主机，阻止服务端重定向值形成 SSRF。"""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_BASE_URL
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "weixin.qq.com" or host.endswith(".weixin.qq.com")
    ):
        raise ValueError("微信服务返回了不受信任的 API 地址")
    return raw


def _headers(token: str = "", *, authenticated: bool = True) -> dict[str, str]:
    uin = secrets.randbits(32)
    result = {
        "Content-Type": "application/json",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_CLIENT_VERSION,
    }
    if authenticated:
        result.update({
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(uin).encode("ascii")).decode("ascii"),
        })
        if token.strip():
            result["Authorization"] = f"Bearer {token.strip()}"
    return result


def _base_info() -> dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": "HarnessAgent/2.0.0"}


def _qr_data_url(content: str) -> str:
    """把腾讯返回的登录内容编码为本地 PNG，避免 CSP 放行第三方图片域名。"""
    import qrcode

    image = qrcode.make(content)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _session_id(channel_id: int, sender_id: str, agent_id: int) -> str:
    # 确定性 Thread：同一微信身份续聊复用历史，不同渠道/身份永不合并。
    return hashlib.sha256(
        f"weixin:{channel_id}:{sender_id}:{agent_id}".encode("utf-8")
    ).hexdigest()[:32]


def _extract_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in message.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or 0) == 1:
            text = str((item.get("text_item") or {}).get("text") or "").strip()
        elif int(item.get("type") or 0) == 3:
            text = str((item.get("voice_item") or {}).get("text") or "").strip()
        else:
            text = ""
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


@dataclass
class LoginSession:
    channel_id: int
    owner_id: int
    session_key: str
    qrcode: str
    qr_data_url: str
    started_at: datetime.datetime
    status: str = "waiting"
    message: str = "请使用手机微信扫码并确认连接。"
    verify_code: str = ""
    redirect_base_url: str = DEFAULT_BASE_URL
    account_id: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "status": self.status,
            "message": self.message,
            "qr_data_url": self.qr_data_url if self.status not in _TERMINAL_LOGIN else "",
            "needs_pair_code": self.status == "needs_pair_code",
            "account_id": self.account_id if self.status == "connected" else "",
        }


class WeixinChannelManager:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._logins: dict[int, LoginSession] = {}
        self._monitors: dict[int, asyncio.Task] = {}
        self._message_tasks: set[asyncio.Task] = set()
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False)
        db = SessionLocal()
        try:
            ids = [
                row[0]
                for row in db.query(Channel.id).filter(
                    Channel.type == CHANNEL_TYPE,
                    Channel.enabled.is_(True),
                    Channel.connection_status == "connected",
                )
            ]
        finally:
            db.close()
        for channel_id in ids:
            self.start_monitor(channel_id)

    async def stop(self) -> None:
        self._stopping = True
        tasks = [
            *(session.task for session in self._logins.values() if session.task),
            *self._monitors.values(),
            *self._message_tasks,
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._logins.clear()
        self._monitors.clear()
        self._message_tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        token: str = "",
        body: dict[str, Any] | None = None,
        timeout: float = 40.0,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        if self._client is None:
            await self.start()
        assert self._client is not None
        response = await self._client.request(
            method,
            url,
            headers=_headers(token, authenticated=authenticated),
            json=body,
            timeout=httpx.Timeout(timeout),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("微信服务返回格式无效")
        return data

    def _local_tokens(self, owner_id: int) -> list[str]:
        """只上送腾讯已签发且仍处于连接态的 Token。

        Channel.token 历史上同时用于 Webhook 密钥；旧版本创建个人微信渠道时也会
        预填随机值。该随机值不是 iLink bot token，传入 local_token_list 会让腾讯
        返回 ret 但不返回二维码。
        """
        db = SessionLocal()
        try:
            rows = (
                db.query(Channel)
                .filter(
                    Channel.type == CHANNEL_TYPE,
                    Channel.created_by == owner_id,
                    Channel.connection_status == "connected",
                    Channel.account_id != "",
                )
                .order_by(Channel.id.desc())
                .limit(10)
                .all()
            )
            return [row.token for row in rows if row.token]
        finally:
            db.close()

    async def start_login(self, channel_id: int, owner_id: int) -> dict[str, Any]:
        previous = self._logins.pop(channel_id, None)
        if previous and previous.task:
            previous.task.cancel()
        self.stop_monitor(channel_id)
        data = await self._request_json(
            "POST",
            f"{DEFAULT_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
            body={"local_token_list": self._local_tokens(owner_id)},
            timeout=30,
        )
        qrcode_value = str(data.get("qrcode") or "").strip()
        qrcode_content = str(data.get("qrcode_img_content") or "").strip()
        if not qrcode_value or not qrcode_content:
            ret = data.get("ret")
            detail = str(data.get("errmsg") or data.get("message") or "")[:300]
            raise RuntimeError(
                f"微信服务未返回有效二维码(ret={ret!r}, message={detail!r})"
            )
        session = LoginSession(
            channel_id=channel_id,
            owner_id=owner_id,
            session_key=uuid.uuid4().hex,
            qrcode=qrcode_value,
            qr_data_url=_qr_data_url(qrcode_content),
            started_at=_now(),
        )
        self._logins[channel_id] = session
        db = SessionLocal()
        try:
            channel = db.get(Channel, channel_id)
            if channel is None or channel.created_by != owner_id:
                raise RuntimeError("消息渠道不存在")
            channel.connection_status = "binding"
            channel.last_error = ""
            db.commit()
        finally:
            db.close()
        session.task = asyncio.create_task(self._poll_login(session))
        return session.public()

    def login_status(self, channel_id: int, owner_id: int) -> dict[str, Any]:
        session = self._logins.get(channel_id)
        if session and session.owner_id == owner_id:
            return session.public()
        db = SessionLocal()
        try:
            channel = db.get(Channel, channel_id)
            if channel is None or channel.created_by != owner_id:
                raise KeyError(channel_id)
            return {
                "session_key": "",
                "status": channel.connection_status,
                "message": channel.last_error or (
                    "微信已连接。" if channel.connection_status == "connected" else "尚未开始扫码。"
                ),
                "qr_data_url": "",
                "needs_pair_code": False,
                "account_id": channel.account_id if channel.connection_status == "connected" else "",
            }
        finally:
            db.close()

    def submit_pair_code(self, channel_id: int, owner_id: int, code: str) -> dict[str, Any]:
        session = self._logins.get(channel_id)
        if session is None or session.owner_id != owner_id:
            raise KeyError(channel_id)
        session.verify_code = code.strip()
        session.status = "verifying"
        session.message = "配对码已提交，正在验证。"
        return session.public()

    async def _poll_login(self, session: LoginSession) -> None:
        deadline = asyncio.get_running_loop().time() + LOGIN_TTL_SECONDS
        try:
            while asyncio.get_running_loop().time() < deadline and not self._stopping:
                endpoint = (
                    f"{session.redirect_base_url}/ilink/bot/get_qrcode_status"
                    f"?qrcode={quote(session.qrcode, safe='')}"
                )
                if session.verify_code:
                    endpoint += f"&verify_code={quote(session.verify_code, safe='')}"
                try:
                    result = await self._request_json(
                        "GET", endpoint, timeout=38
                    )
                except (httpx.TimeoutException, httpx.NetworkError):
                    await asyncio.sleep(1)
                    continue
                status = str(result.get("status") or "wait")
                if status == "wait":
                    session.status = "waiting"
                    session.message = "等待扫码。"
                elif status == "scaned":
                    session.verify_code = ""
                    session.status = "scanned"
                    session.message = "已扫码，请在手机微信确认连接。"
                elif status == "need_verifycode":
                    session.status = "needs_pair_code"
                    session.message = "请输入手机微信显示的数字配对码。"
                elif status == "verify_code_blocked":
                    session.verify_code = ""
                    session.status = "needs_pair_code"
                    session.message = "配对码多次错误，请按手机提示重新输入。"
                elif status == "scaned_but_redirect":
                    host = str(result.get("redirect_host") or "").strip()
                    session.redirect_base_url = _allowed_base_url(f"https://{host}")
                    session.status = "scanned"
                    session.message = "已扫码，正在切换微信服务节点。"
                elif status == "binded_redirect":
                    session.status = "already_connected"
                    session.message = "该微信已连接过当前服务；如需重绑，请先解绑原渠道。"
                    self._mark_channel(session.channel_id, "error", session.message)
                    return
                elif status == "expired":
                    session.status = "expired"
                    session.message = "二维码已过期，请重新生成。"
                    self._mark_channel(session.channel_id, "unbound", session.message)
                    return
                elif status == "confirmed":
                    token = str(result.get("bot_token") or "").strip()
                    account_id = str(result.get("ilink_bot_id") or "").strip()
                    user_id = str(result.get("ilink_user_id") or "").strip()
                    if not token or not account_id or not user_id:
                        raise RuntimeError("微信确认成功但缺少账号凭据")
                    base_url = _allowed_base_url(str(result.get("baseurl") or ""))
                    self._save_connected(session, token, account_id, user_id, base_url)
                    session.account_id = account_id
                    session.status = "connected"
                    session.message = "微信已连接，消息将进入你的个人智能体空间。"
                    self.start_monitor(session.channel_id)
                    return
                await asyncio.sleep(1)
            session.status = "expired"
            session.message = "扫码连接超时，请重新生成二维码。"
            self._mark_channel(session.channel_id, "unbound", session.message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("微信扫码登录失败 channel=%s: %s", session.channel_id, exc)
            session.status = "error"
            session.message = "微信连接失败，请稍后重新扫码。"
            self._mark_channel(session.channel_id, "error", str(exc)[:1000])

    def _save_connected(
        self, session: LoginSession, token: str, account_id: str, user_id: str, base_url: str
    ) -> None:
        db = SessionLocal()
        try:
            channel = db.get(Channel, session.channel_id)
            if channel is None or channel.created_by != session.owner_id:
                raise RuntimeError("扫码过程中渠道归属已变化")
            duplicate = db.query(Channel).filter(
                Channel.id != channel.id,
                Channel.type == CHANNEL_TYPE,
                Channel.account_id == account_id,
                Channel.account_id != "",
            ).first()
            if duplicate is not None:
                raise RuntimeError("该微信机器人已绑定到其他平台用户")
            channel.token = token
            channel.account_id = account_id
            channel.account_user_id = user_id
            channel.base_url = base_url
            channel.sync_buf = ""
            channel.connection_status = "connected"
            channel.last_error = ""
            channel.enabled = True
            db.commit()
        finally:
            db.close()

    def _mark_channel(self, channel_id: int, status: str, error: str = "") -> None:
        db = SessionLocal()
        try:
            channel = db.get(Channel, channel_id)
            if channel is not None:
                channel.connection_status = status
                channel.last_error = error[:2000]
                db.commit()
        finally:
            db.close()

    def start_monitor(self, channel_id: int) -> None:
        current = self._monitors.get(channel_id)
        if current and not current.done():
            return
        task = asyncio.create_task(self._monitor(channel_id))
        self._monitors[channel_id] = task
        task.add_done_callback(lambda _task, cid=channel_id: self._monitors.pop(cid, None))

    def stop_monitor(self, channel_id: int) -> None:
        task = self._monitors.pop(channel_id, None)
        if task:
            task.cancel()

    async def disconnect(self, channel_id: int, owner_id: int) -> None:
        session = self._logins.pop(channel_id, None)
        if session and session.task:
            session.task.cancel()
        self.stop_monitor(channel_id)
        db = SessionLocal()
        try:
            channel = db.get(Channel, channel_id)
            if channel is None or channel.created_by != owner_id:
                raise KeyError(channel_id)
            channel.token = ""
            channel.account_id = ""
            channel.account_user_id = ""
            channel.base_url = ""
            channel.sync_buf = ""
            channel.connection_status = "unbound"
            channel.last_error = ""
            channel.last_inbound_at = None
            channel.last_outbound_at = None
            db.commit()
        finally:
            db.close()

    async def _monitor(self, channel_id: int) -> None:
        failures = 0
        next_timeout = 38.0
        while not self._stopping:
            db = SessionLocal()
            try:
                channel = db.get(Channel, channel_id)
                if (
                    channel is None or channel.type != CHANNEL_TYPE or not channel.enabled
                    or channel.connection_status != "connected" or not channel.token
                ):
                    return
                token = channel.token
                base_url = _allowed_base_url(channel.base_url)
                sync_buf = channel.sync_buf or ""
                allowed_sender = channel.account_user_id
            finally:
                db.close()
            try:
                result = await self._request_json(
                    "POST",
                    f"{base_url}/ilink/bot/getupdates",
                    token=token,
                    body={"get_updates_buf": sync_buf, "base_info": _base_info()},
                    timeout=next_timeout,
                )
                api_error = int(result.get("ret") or 0) or int(result.get("errcode") or 0)
                if api_error:
                    raise RuntimeError(f"getUpdates error {api_error}")
                failures = 0
                suggested = float(result.get("longpolling_timeout_ms") or 35000) / 1000 + 3
                next_timeout = max(15.0, min(suggested, 65.0))
                new_buf = str(result.get("get_updates_buf") or "")
                if new_buf:
                    db = SessionLocal()
                    try:
                        current = db.get(Channel, channel_id)
                        if current is not None:
                            current.sync_buf = new_buf
                            db.commit()
                    finally:
                        db.close()
                for message in result.get("msgs") or []:
                    if not isinstance(message, dict) or int(message.get("message_type") or 0) != 1:
                        continue
                    sender = str(message.get("from_user_id") or "").strip()
                    if not sender or sender != allowed_sender:
                        logger.warning("已拒绝非绑定微信身份的消息 channel=%s", channel_id)
                        continue
                    query = _extract_text(message)
                    if not query:
                        await self._send_text(
                            channel_id, sender, "目前支持文字消息和带转写文本的语音消息。",
                            str(message.get("context_token") or ""),
                        )
                        continue
                    db = SessionLocal()
                    try:
                        current = db.get(Channel, channel_id)
                        if current is not None:
                            current.last_inbound_at = _now()
                            current.last_error = ""
                            db.commit()
                    finally:
                        db.close()
                    task = asyncio.create_task(self._handle_message(channel_id, message, query))
                    self._message_tasks.add(task)
                    task.add_done_callback(self._finish_message_task)
            except asyncio.CancelledError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError):
                continue
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self._mark_channel(channel_id, "connected", str(exc)[:1000])
                await asyncio.sleep(2 if failures < 3 else 30)

    def _finish_message_task(self, task: asyncio.Task) -> None:
        self._message_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:  # noqa: BLE001
            logger.warning("微信消息处理任务异常", exc_info=True)

    async def _handle_message(
        self, channel_id: int, message: dict[str, Any], query: str
    ) -> None:
        sender = str(message.get("from_user_id") or "").strip()
        context_token = str(message.get("context_token") or "")
        message_key = str(
            message.get("message_id") or message.get("client_id")
            or f"{message.get('create_time_ms')}:{hashlib.sha256(query.encode()).hexdigest()[:16]}"
        )
        db = SessionLocal()
        try:
            channel = db.get(Channel, channel_id)
            if channel is None or channel.account_user_id != sender:
                return
            agent = db.get(Agent, channel.agent_id)
            user = db.get(User, channel.created_by)
            if agent is None or not agent.enabled or user is None or not user.is_active:
                answer = "个人智能体暂不可用，请联系管理员。"
                job_id = ""
            else:
                from .api.chat import build_execution_snapshot, select_chat_provider, serialize_input

                inputs = TaskInput(query=query)
                selected_provider = select_chat_provider(db, user, agent, None, query)
                payload = {
                    "user_id": user.id,
                    "agent_id": agent.id,
                    "harness_version": agent.active_version,
                    "inputs": serialize_input(inputs),
                    "template_ids": [], "dataset_ids": [], "skill_ids": [],
                    "mcp_ids": [], "invoked_agent_ids": [], "provider_id": None,
                    "attachment_images": [], "attachment_docs": [],
                    "attachment_ids": [], "attachment_records": [], "attachment_context": [],
                    "session_id": _session_id(channel.id, sender, agent.id),
                    "project_id": None,
                    "source": "weixin",
                    "approval_tokens": [],
                    "execution_snapshot": build_execution_snapshot(
                        db, agent, query, provider=selected_provider
                    ),
                }
                job_id = jobs.enqueue_in_session(
                    db,
                    user.id,
                    agent.id,
                    "chat",
                    payload,
                    idempotency_key=f"weixin:{channel.id}:{message_key}"[:128],
                )
                db.commit()
                answer = ""
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("微信消息入队失败 channel=%s: %s", channel_id, exc)
            answer = "当前任务繁忙，请稍后重试。"
            job_id = ""
        finally:
            db.close()

        if job_id:
            deadline = asyncio.get_running_loop().time() + 8 * 60
            while asyncio.get_running_loop().time() < deadline:
                view = await asyncio.to_thread(jobs.view, job_id)
                if view is not None and view.status in jobs._TERMINAL:
                    if view.status == jobs.DONE:
                        answer = str((view.result or {}).get("answer") or "")
                    else:
                        answer = "抱歉，本次任务未能完成，请稍后重试。"
                    break
                await asyncio.sleep(0.5)
            if not answer:
                answer = "任务仍在处理中，请稍后在网页端查看结果。"
        await self._send_text(channel_id, sender, answer[:MAX_TEXT_CHARS], context_token, job_id)

    async def _send_text(
        self,
        channel_id: int,
        recipient: str,
        text: str,
        context_token: str,
        run_id: str = "",
    ) -> None:
        db = SessionLocal()
        try:
            channel = db.get(Channel, channel_id)
            if channel is None or not channel.token:
                return
            token = channel.token
            base_url = _allowed_base_url(channel.base_url)
        finally:
            db.close()
        client_id = f"harness-weixin-{uuid.uuid4().hex}"
        result = await self._request_json(
            "POST",
            f"{base_url}/ilink/bot/sendmessage",
            token=token,
            body={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": recipient,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                    "context_token": context_token or None,
                    "run_id": run_id or None,
                },
                "base_info": _base_info(),
            },
            timeout=20,
        )
        api_error = int(result.get("ret") or 0) or int(result.get("errcode") or 0)
        if api_error:
            message = str(result.get("errmsg") or result.get("message") or "")[:500]
            raise RuntimeError(f"sendMessage error {api_error}: {message}")
        db = SessionLocal()
        try:
            channel = db.get(Channel, channel_id)
            if channel is not None:
                channel.last_outbound_at = _now()
                channel.last_error = ""
                db.commit()
        finally:
            db.close()

    def public_runtime(self, channel: Channel) -> dict[str, str]:
        return {
            "connection_status": channel.connection_status or "unbound",
            "last_error": channel.last_error or "",
            "last_inbound_at": iso_utc(channel.last_inbound_at),
            "last_outbound_at": iso_utc(channel.last_outbound_at),
        }


manager = WeixinChannelManager()
