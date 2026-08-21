"""消息渠道：个人微信扫码绑定，并兼容历史 Webhook / 公众号接口。

三条管线：
- 管理（root/admin，JWT）：`/api/v1/channels` 增删改查、查看接口地址与 Token。
- 通用接口（generic，Bearer token 鉴权）：`POST /open/channel/<path_key>`
    传 {"query": ...} 调用绑定智能体；同步返回 {answer,...} 或（超时/async）返回 job_id 轮询。
    适配任意第三方系统、自有前端、对话平台 webhook。
- 微信公众号回调（wechat_mp，免登录、签名校验）：`/open/wechat/<path_key>`
    GET  —— URL 接入验签，回显 echostr。
    POST —— 收文本/关注事件，调用绑定智能体，按「同步回复」或「客服消息异步推送」答复。

公众号回复策略（公众号要求 5s 内响应，失败重试至多 3 次）：
- 未配置 AppID/AppSecret：同步等待 WECHAT_SYNC_WAIT_SECONDS 取结果即回复；超时回 success，
  借公众号对同一 MsgId 的重试再答（去重命中同一任务）。适合「快回答」的对话智能体。
- 配置了 AppID/AppSecret：首条消息立即回执「正在生成…」，结果在后台用「客服消息」推送。
  适合耗时较长的流程智能体（不受 5s 窗口限制）。
"""
import asyncio
import datetime
import hashlib
import logging
import secrets
import time
import xml.etree.ElementTree as ET
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import jobs
from ..config import settings
from ..database import get_db
from ..models import Agent, Channel, ChannelDispatch, User
from ..schemas import ChannelCreate, ChannelOut, ChannelUpdate, WeixinPairCodeRequest
from ..security import can_manage, require_module, require_owner, require_use, scope_owned
from ..weixin_channel import CHANNEL_TYPE as WEIXIN_CHANNEL_TYPE, manager as weixin_manager
from .chat import resolve_agent, serialize_input
from .settings import get_public_base_url
from ..runtime import TaskInput
from ..rate_limit import client_ip, enforce, reset

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels", tags=["消息渠道"])
webhook_router = APIRouter(prefix="/open/wechat", tags=["微信公众号回调（签名校验）"])
generic_router = APIRouter(prefix="/open/channel", tags=["第三方通用接口（Bearer Token）"])

CHANNEL_TYPES = {WEIXIN_CHANNEL_TYPE, "generic", "wechat_mp"}
WELCOME_FALLBACK = "您好，我已上线，请直接发送您的问题。"
SLOW_ACK = "已收到，正在为您生成，请稍候…"
SLOW_NO_PUSH = "正在处理，请稍后再发送一次「{q}」查看结果。"


def _endpoint_path(channel: Channel) -> str:
    """渠道对外可调用的地址（按类型）。"""
    if channel.type == "wechat_mp":
        return f"/open/wechat/{channel.path_key}"
    return f"/open/channel/{channel.path_key}"


# ---------- 管理（JWT，按模块授权 channels） ----------

def _channel_out(c: Channel, agent_name: str, user: User, base: str = "") -> ChannelOut:
    path = _endpoint_path(c)
    return ChannelOut(
        id=c.id, name=c.name, type=c.type, agent_id=c.agent_id, agent_name=agent_name,
        path_key=c.path_key,
        # iLink bot token 是长期登录凭据，任何情况下都不返回浏览器。
        token="" if c.type == WEIXIN_CHANNEL_TYPE else c.token,
        webhook_path="" if c.type == WEIXIN_CHANNEL_TYPE else path,
        webhook_url=(
            "" if c.type == WEIXIN_CHANNEL_TYPE
            else (base.rstrip("/") + path) if base else ""
        ),
        app_id=c.app_id, has_app_secret=bool(c.app_secret), enabled=c.enabled,
        can_manage=can_manage(user, c),
        account_id=c.account_id or "",
        workspace_key=f"user_{c.created_by}/agent_{c.agent_id}",
        **weixin_manager.public_runtime(c),
    )


def _agent_name(db: Session, agent_id: Optional[int]) -> str:
    if not agent_id:
        return ""
    a = db.get(Agent, agent_id)
    return a.name if a else "（已删除）"


def _unique_path_key(db: Session) -> str:
    for _ in range(8):
        key = secrets.token_urlsafe(12).replace("_", "").replace("-", "")[:16]
        if key and db.query(Channel).filter(Channel.path_key == key).first() is None:
            return key
    return secrets.token_hex(12)


def _bind_agent(db: Session, user: User, agent_id: int) -> Agent:
    """校验绑定的智能体存在、启用且当前管理员可用。"""
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "绑定的智能体不存在或已停用")
    require_use(user, agent, "无权绑定该智能体")
    return agent


@router.get("", response_model=list[ChannelOut])
def list_channels(admin: User = Depends(require_module("channels")), db: Session = Depends(get_db)):
    rows = scope_owned(db.query(Channel), Channel, admin).order_by(Channel.id).all()
    base = get_public_base_url(db)
    return [_channel_out(c, _agent_name(db, c.agent_id), admin, base) for c in rows]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ChannelOut)
def create_channel(
    body: ChannelCreate,
    admin: User = Depends(require_module("channels")),
    db: Session = Depends(get_db),
):
    if body.type not in CHANNEL_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不支持的渠道类型")
    if body.type == WEIXIN_CHANNEL_TYPE:
        existing = db.query(Channel).filter(
            Channel.created_by == admin.id,
            Channel.type == WEIXIN_CHANNEL_TYPE,
        ).first()
        if existing is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "每个用户只能绑定一个个人微信消息渠道")
    agent = _bind_agent(db, admin, body.agent_id)
    channel = Channel(
        name=body.name, type=body.type, agent_id=agent.id,
        path_key=_unique_path_key(db),
        # 个人微信 Token 只能由扫码确认后的腾讯 iLink 服务签发。预填随机 Webhook
        # 密钥会污染 get_bot_qrcode 的 local_token_list，导致返回中没有二维码。
        token="" if body.type == WEIXIN_CHANNEL_TYPE else secrets.token_urlsafe(16),
        app_id=body.app_id.strip(), app_secret=body.app_secret.strip(),
        connection_status="unbound" if body.type == WEIXIN_CHANNEL_TYPE else "connected",
        enabled=True, created_by=admin.id,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return _channel_out(channel, agent.name, admin, get_public_base_url(db))


@router.patch("/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    admin: User = Depends(require_module("channels")),
    db: Session = Depends(get_db),
):
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "渠道不存在")
    require_owner(admin, channel)
    if body.name is not None:
        channel.name = body.name
    if body.agent_id is not None:
        _bind_agent(db, admin, body.agent_id)
        channel.agent_id = body.agent_id
    if body.enabled is not None:
        channel.enabled = body.enabled
    if body.app_id is not None:
        channel.app_id = body.app_id.strip()
    if body.app_secret is not None:
        channel.app_secret = body.app_secret.strip()
    if body.regenerate_token:
        if channel.type == WEIXIN_CHANNEL_TYPE:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "个人微信登录凭据只能通过扫码更新",
            )
        channel.token = secrets.token_urlsafe(16)
    db.commit()
    db.refresh(channel)
    if channel.type == WEIXIN_CHANNEL_TYPE:
        if not channel.enabled:
            weixin_manager.stop_monitor(channel.id)
        elif channel.connection_status == "connected":
            weixin_manager.start_monitor(channel.id)
    return _channel_out(channel, _agent_name(db, channel.agent_id), admin, get_public_base_url(db))


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: int,
    admin: User = Depends(require_module("channels")),
    db: Session = Depends(get_db),
):
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "渠道不存在")
    require_owner(admin, channel)
    if channel.type == WEIXIN_CHANNEL_TYPE:
        await weixin_manager.disconnect(channel.id, channel.created_by)
        channel = db.get(Channel, channel_id)
    db.delete(channel)
    db.commit()


def _personal_weixin_channel(db: Session, channel_id: int, user: User) -> Channel:
    channel = db.get(Channel, channel_id)
    if channel is None or channel.type != WEIXIN_CHANNEL_TYPE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "个人微信消息渠道不存在")
    require_owner(user, channel)
    return channel


@router.post("/{channel_id}/login")
async def start_weixin_login(
    channel_id: int,
    user: User = Depends(require_module("channels")),
    db: Session = Depends(get_db),
):
    """生成只属于当前平台用户的微信二维码，并在后台等待手机确认。"""
    channel = _personal_weixin_channel(db, channel_id, user)
    if channel.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能由渠道所属用户扫码绑定")
    try:
        return await weixin_manager.start_login(channel.id, user.id)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        logger.warning("生成微信二维码失败 channel=%s: %s", channel.id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "暂时无法生成微信二维码，请稍后重试")


@router.get("/{channel_id}/login")
def get_weixin_login_status(
    channel_id: int,
    user: User = Depends(require_module("channels")),
    db: Session = Depends(get_db),
):
    channel = _personal_weixin_channel(db, channel_id, user)
    try:
        return weixin_manager.login_status(channel.id, channel.created_by)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "扫码会话不存在")


@router.post("/{channel_id}/pair-code")
def submit_weixin_pair_code(
    channel_id: int,
    body: WeixinPairCodeRequest,
    user: User = Depends(require_module("channels")),
    db: Session = Depends(get_db),
):
    channel = _personal_weixin_channel(db, channel_id, user)
    if channel.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能由渠道所属用户提交配对码")
    try:
        return weixin_manager.submit_pair_code(channel.id, user.id, body.code)
    except KeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "当前没有等待配对码的扫码会话")


@router.post("/{channel_id}/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_weixin(
    channel_id: int,
    user: User = Depends(require_module("channels")),
    db: Session = Depends(get_db),
):
    channel = _personal_weixin_channel(db, channel_id, user)
    await weixin_manager.disconnect(channel.id, channel.created_by)


# ---------- 公众号签名 / XML ----------

def wechat_signature(token: str, timestamp: str, nonce: str) -> str:
    """公众号消息签名：sha1(sort(token, timestamp, nonce) 拼接)。"""
    return hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode()).hexdigest()


def _signature_ok(channel: Channel, signature: str, timestamp: str, nonce: str) -> bool:
    if not (channel.token and signature and timestamp and nonce):
        return False
    return secrets.compare_digest(wechat_signature(channel.token, timestamp, nonce), signature)


def _parse_message(body: bytes) -> dict:
    """解析公众号推送的 XML 消息为扁平 dict；防御超大体与解析异常。"""
    if len(body) > 64 * 1024:
        raise ValueError("消息体过大")
    root = ET.fromstring(body)  # 公众号消息无 DTD/外部实体，ET 默认不解析外部实体
    return {child.tag: (child.text or "") for child in root}


def _xml_text_reply(to_user: str, from_user: str, content: str) -> str:
    """构造公众号被动回复的文本 XML（to/from 相对收到的消息已对调）。"""
    safe = (content or "").replace("]]>", "]]]]><![CDATA[>")
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{safe}]]></Content>"
        "</xml>"
    )


def _clip(text: str) -> str:
    limit = settings.WECHAT_REPLY_MAX_CHARS
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…\n（回复较长已截断，完整内容请在平台查看）"


# ---------- 去重派发：同一消息（含公众号重试）映射到同一任务 ----------

_DISPATCH_TTL_SECONDS = 600


def _dispatch_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _enqueue_chat(
    channel: Channel,
    agent: Agent,
    query: str,
    *,
    idempotency_key: str | None = None,
) -> str:
    inputs = TaskInput(query=query)
    payload = {
        "user_id": channel.created_by,
        "agent_id": agent.id,
        "harness_version": agent.active_version,
        "inputs": serialize_input(inputs),
        "template_ids": [],
        "source": "wechat",
    }
    return jobs.enqueue(
        channel.created_by,
        agent.id,
        "chat",
        payload,
        idempotency_key=idempotency_key,
    )


def _dispatch_message(
    db: Session,
    dedupe_key: str,
    channel: Channel,
    agent: Agent,
    query: str,
    *,
    claim_push: bool,
) -> tuple[str, bool]:
    """跨进程幂等派发；返回 (job_id, 是否取得客服推送权)。"""
    token = _dispatch_hash(dedupe_key)
    now = datetime.datetime.now(datetime.timezone.utc)
    db.query(ChannelDispatch).filter(
        ChannelDispatch.expires_at <= now
    ).delete(synchronize_session=False)
    db.commit()
    row = db.get(ChannelDispatch, token)
    if row is None:
        # 结束只读事务后再用任务队列的独立短会话写入，避免 SQLite 锁升级冲突。
        db.rollback()
        job_id = _enqueue_chat(
            channel,
            agent,
            query,
            idempotency_key=f"wechat:{token}",
        )
        row = ChannelDispatch(
            dedupe_key=token,
            channel_id=channel.id,
            job_id=job_id,
            expires_at=now + datetime.timedelta(seconds=_DISPATCH_TTL_SECONDS),
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(ChannelDispatch, token)
            if row is None:
                raise
    push_claimed = False
    if claim_push:
        push_claimed = bool(
            db.query(ChannelDispatch)
            .filter(
                ChannelDispatch.dedupe_key == token,
                ChannelDispatch.pushing.is_(False),
            )
            .update(
                {ChannelDispatch.pushing: True},
                synchronize_session=False,
            )
        )
        db.commit()
        row = db.get(ChannelDispatch, token)
    return row.job_id, push_claimed


def _drop_dispatch(db: Session, dedupe_key: str) -> None:
    db.query(ChannelDispatch).filter(
        ChannelDispatch.dedupe_key == _dispatch_hash(dedupe_key)
    ).delete(synchronize_session=False)
    db.commit()


async def _await_job(job_id: str, timeout: float) -> Optional[jobs.JobView]:
    """轮询任务直至终态或超时；返回终态快照，超时返回 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = await asyncio.to_thread(jobs.view, job_id)
        if view is not None and view.status in (
            jobs.DONE, jobs.FAILED, jobs.CANCELLED, jobs.DEAD_LETTER
        ):
            return view
        await asyncio.sleep(0.3)
    return None


def _answer_of(view: Optional[jobs.JobView]) -> Optional[str]:
    if view is None or view.status != jobs.DONE or not view.result:
        return None
    return view.result.get("answer") or ""


# ---------- 客服消息异步推送（配置了 AppID/AppSecret 时） ----------

_wx_token_cache: "dict[str, tuple[str, float]]" = {}  # app_id -> (access_token, expiry_monotonic)


async def _wx_access_token(client: httpx.AsyncClient, app_id: str, app_secret: str) -> str:
    cached = _wx_token_cache.get(app_id)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    resp = await client.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={"grant_type": "client_credential", "appid": app_id, "secret": app_secret},
    )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"获取 access_token 失败：{data}")
    _wx_token_cache[app_id] = (token, time.monotonic() + int(data.get("expires_in", 7200)) - 300)
    return token


async def _push_customer_message(channel: Channel, openid: str, job_id: str) -> None:
    """等任务完成后，用客服消息把答复推给用户（超 5s 窗口的长回答）。"""
    try:
        view = await _await_job(job_id, timeout=float(settings.JOB_TTL_SECONDS))
        answer = _answer_of(view)
        if not answer:
            answer = "抱歉，本次未能生成结果，请稍后重试。"
        async with httpx.AsyncClient(timeout=20) as client:
            token = await _wx_access_token(client, channel.app_id, channel.app_secret)
            resp = await client.post(
                "https://api.weixin.qq.com/cgi-bin/message/custom/send",
                params={"access_token": token},
                content=_json_customer_message(openid, _clip(answer)),
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
            if data.get("errcode"):
                logger.warning("客服消息推送失败 channel=%s errcode=%s", channel.id, data)
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响主流程
        logger.warning("客服消息推送异常 channel=%s：%s", channel.id, exc)


def _json_customer_message(openid: str, content: str) -> bytes:
    import json as _json
    # ensure_ascii=False 避免中文被转义后超字节上限
    return _json.dumps(
        {"touser": openid, "msgtype": "text", "text": {"content": content}},
        ensure_ascii=False,
    ).encode("utf-8")


# ---------- 公众号回调 ----------

def _get_channel(db: Session, path_key: str, expected_type: str) -> Channel:
    channel = db.query(Channel).filter(
        Channel.path_key == path_key, Channel.type == expected_type
    ).first()
    if channel is None or not channel.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "渠道不存在或已停用")
    return channel


@webhook_router.get("/{path_key}")
def wechat_verify(
    path_key: str,
    signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """公众号服务器配置「提交」时的接入验签：签名正确则回显 echostr。"""
    channel = _get_channel(db, path_key, "wechat_mp")
    if not _signature_ok(channel, signature, timestamp, nonce):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "签名校验失败")
    return PlainTextResponse(echostr)


@webhook_router.post("/{path_key}")
async def wechat_message(
    path_key: str,
    request: Request,
    signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """公众号消息回调：验签 → 解析 → 调用绑定智能体 → 回复（同步或客服消息）。"""
    ip = client_ip(request)
    enforce(
        "wechat-ip", ip,
        settings.OPEN_API_RATE_LIMIT * 2,
        settings.OPEN_API_RATE_WINDOW_SECONDS,
    )
    channel = _get_channel(db, path_key, "wechat_mp")
    if not _signature_ok(channel, signature, timestamp, nonce):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "签名校验失败")
    reset("wechat-ip", ip)
    try:
        msg = _parse_message(await request.body())
    except (ET.ParseError, ValueError):
        return PlainTextResponse("success")  # 解析失败：回 success 让公众号不再重试

    from_user = msg.get("FromUserName", "")
    to_user = msg.get("ToUserName", "")
    msg_type = msg.get("MsgType", "")
    reply = lambda text: PlainTextResponse(_xml_text_reply(from_user, to_user, text))

    agent = db.get(Agent, channel.agent_id)
    if agent is None or not agent.enabled:
        return reply("智能体暂不可用，请稍后再试。")

    # 关注事件：欢迎语（不触发智能体执行）
    if msg_type == "event":
        if msg.get("Event", "").lower() == "subscribe":
            return reply(agent.opening_statement or WELCOME_FALLBACK)
        return PlainTextResponse("success")

    # 取用户文本（语音消息取识别结果）
    query = msg.get("Content") if msg_type == "text" else msg.get("Recognition", "")
    query = (query or "").strip()
    if not query:
        return reply("请发送文字消息，我来帮您解答。")

    # 去重：同一消息的公众号重试映射到同一任务
    dedupe_key = f"{path_key}:{from_user}:{msg.get('MsgId') or msg.get('CreateTime') or query}"
    use_push = bool(channel.app_id and channel.app_secret)
    job_id, push_claimed = _dispatch_message(
        db, dedupe_key, channel, agent, query, claim_push=use_push
    )
    # 客服消息模式：仅取得数据库原子推送权的副本启动后台任务。
    if use_push and push_claimed:
        asyncio.create_task(_push_customer_message(channel, from_user, job_id))

    if use_push:
        return reply(SLOW_ACK)

    # 同步模式：等待窗口内出结果即回复；超时回 success（靠公众号重试再答）
    view = await _await_job(job_id, timeout=settings.WECHAT_SYNC_WAIT_SECONDS)
    answer = _answer_of(view)
    if answer:
        _drop_dispatch(db, dedupe_key)
        return reply(_clip(answer))
    if view is not None and view.status in (jobs.FAILED, jobs.CANCELLED):
        _drop_dispatch(db, dedupe_key)
        return reply("抱歉，本次未能生成结果，请稍后重试。")
    return PlainTextResponse("success")


# ---------- 通用第三方接口（generic，Bearer Token 鉴权） ----------

def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.headers.get("x-channel-token", "").strip()


def _require_channel_token(channel: Channel, request: Request) -> None:
    token = _bearer_token(request)
    if not (channel.token and token and secrets.compare_digest(token, channel.token)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "渠道令牌无效")


def _job_payload(view: Optional[jobs.JobView]) -> dict:
    """把任务快照整理成对外 JSON。"""
    if view is None:
        return {"status": jobs.RUNNING}
    out: dict = {"status": view.status}
    if view.status == jobs.DONE and view.result:
        result = view.result
        completion_status = str(
            result.get("completion_status") or "completed"
        )
        if completion_status not in {"completed", "completed_with_issues"}:
            completion_status = "completed"
        raw_issues = result.get("completion_issues")
        raw_issues = raw_issues if isinstance(raw_issues, (list, tuple)) else []
        raw_summary = result.get("plan_summary")
        raw_summary = raw_summary if isinstance(raw_summary, dict) else {}
        out.update({
            "task_status": completion_status,
            "completion_status": completion_status,
            "completion_issues": [str(value)[:400] for value in raw_issues[:20]],
            "plan_summary": dict(raw_summary),
            "answer": result.get("answer", ""),
            "export_files": result.get("export_files", []),
            "turn_id": result.get("turn_id"),
            "thread_id": result.get("thread_id"),
        })
    elif view.status == jobs.FAILED:
        out["error"] = view.error or "执行失败"
    return out


@generic_router.post("/{path_key}")
async def generic_call(path_key: str, request: Request, db: Session = Depends(get_db)):
    """通用调用：Bearer Token 鉴权 → 调用绑定智能体。

    请求体 JSON：{"query": "用户问题", "async": false}
    - async=false（默认）：同步等待最多 CHANNEL_SYNC_WAIT_SECONDS。出结果返回 {status:"done", answer,...}；
      超时返回 {status:"running", job_id}，调用方用 GET /open/channel/<path_key>/jobs/<job_id> 轮询。
    - async=true：立即返回 {status:"pending", job_id}，全程轮询。
    """
    ip = client_ip(request)
    enforce(
        "channel-ip", ip,
        settings.OPEN_API_RATE_LIMIT,
        settings.OPEN_API_RATE_WINDOW_SECONDS,
    )
    channel = _get_channel(db, path_key, "generic")
    _require_channel_token(channel, request)
    reset("channel-ip", ip)
    enforce(
        "channel", path_key,
        settings.OPEN_API_RATE_LIMIT,
        settings.OPEN_API_RATE_WINDOW_SECONDS,
    )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 非法 JSON
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请求体必须为 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请求体必须为 JSON 对象")
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少 query")

    agent = db.get(Agent, channel.agent_id)
    if agent is None or not agent.enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "绑定的智能体不可用")

    job_id = _enqueue_chat(channel, agent, query)
    if body.get("async"):
        return {"turn_id": job_id, "status": jobs.PENDING}
    view = await _await_job(job_id, timeout=settings.CHANNEL_SYNC_WAIT_SECONDS)
    return {"turn_id": job_id, **_job_payload(view)}


@generic_router.get("/{path_key}/jobs/{job_id}")
def generic_job(path_key: str, job_id: str, request: Request, db: Session = Depends(get_db)):
    """轮询通用调用任务（同 Bearer Token 鉴权）。"""
    channel = _get_channel(db, path_key, "generic")
    _require_channel_token(channel, request)
    enforce(
        "channel-poll", path_key,
        settings.OPEN_API_RATE_LIMIT * 3,
        settings.OPEN_API_RATE_WINDOW_SECONDS,
    )
    view = jobs.view(job_id)
    if view is None or view.agent_id != channel.agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return {"turn_id": job_id, **_job_payload(view)}
