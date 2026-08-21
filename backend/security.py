"""认证与权限：PBKDF2 口令散列、JWT 签发校验、角色依赖项、API Key 校验。

权限模型：
- root  : 全部权限（用户管理、智能体/流程/模型/MCP/Skills/API密钥/知识库、对话）
- admin : 按模块授权，且仅可管理本人创建的资源
- user  : 默认仅对话；root 可额外授予通用设置模块
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import (
    ADMIN_MODULE_KEYS, USER_MODULE_KEYS, ApiKey, ROLE_ADMIN, ROLE_ROOT, ROLE_USER, User,
)
from .rate_limit import client_ip, enforce, reset

_PBKDF2_ITERATIONS = 240_000
_bearer = HTTPBearer(auto_error=False)


# ---------- 口令散列（stdlib，免编译依赖） ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


# ---------- JWT ----------

def create_token(user: User) -> str:
    now = int(time.time())
    payload = {
        "typ": "access",
        "jti": uuid.uuid4().hex,
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "ver": int(getattr(user, "token_version", 0) or 0),
        "iat": now,
        "exp": now + settings.JWT_EXPIRE_MINUTES * 60,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def resolve_access_token(token: str, db: Session) -> User:
    """验签并核对账号状态与令牌版本；所有长连接入口也复用此逻辑。"""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        if (
            payload.get("typ") != "access"
            or not payload.get("jti")
            or payload.get("sub") is None
        ):
            raise jwt.InvalidTokenError("access token claims missing")
        user = db.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录")
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在或已禁用")
    if int(payload.get("ver", -1)) != int(user.token_version or 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已撤销，请重新登录")
    return user


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    token = (
        credentials.credentials if credentials is not None
        else request.cookies.get(settings.AUTH_COOKIE_NAME, "")
    )
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    return resolve_access_token(token, db)


def require_admin(user: User = Depends(get_current_user)) -> User:
    """通用管理鉴权：root 与 admin 可用（不细分模块）。"""
    if user.role not in (ROLE_ROOT, ROLE_ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要 root 或 admin 权限")
    return user


def user_modules(user: User) -> Optional[set]:
    """主体可访问的设置页模块；返回 None 表示「全部」。root 始终全部。

    admin 空串 = 全部（向后兼容）；user 空串 = 无模块。
    """
    if user.role == ROLE_ROOT:
        return None
    raw = (getattr(user, "permissions", "") or "").strip()
    if raw == "":
        return None if user.role == ROLE_ADMIN else set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None if user.role == ROLE_ADMIN else set()
    if not isinstance(data, list):
        return None if user.role == ROLE_ADMIN else set()
    allowed = USER_MODULE_KEYS if user.role == ROLE_USER else ADMIN_MODULE_KEYS
    return {str(x) for x in data if str(x) in allowed}


def has_module_access(user: User, module: str) -> bool:
    """不抛异常的模块授权判定，供 HTTP 依赖和运行时能力共同复用。"""
    if user.role == ROLE_ROOT:
        return True
    if user.role == ROLE_ADMIN:
        modules = user_modules(user)
        return modules is None or module in modules
    if user.role == ROLE_USER and module in USER_MODULE_KEYS:
        return module in (user_modules(user) or set())
    return False


def require_module(module: str):
    """生成「需要某管理模块访问权限」的依赖。root 全通过；admin 按授权；其余 403。

    root 可在「用户管理」中逐个授予 admin / user 可访问的模块。
    """
    def _dep(user: User = Depends(get_current_user)) -> User:
        if has_module_access(user, module):
            return user
        if user.role not in (ROLE_ROOT, ROLE_ADMIN, ROLE_USER):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前角色无设置页访问权限")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无该设置模块的访问权限")
    return _dep


def require_root(user: User = Depends(get_current_user)) -> User:
    """用户管理：仅 root。"""
    if user.role != ROLE_ROOT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要 root 权限")
    return user


def is_root(user: User) -> bool:
    return user.role == ROLE_ROOT


def owns_resource(user: User, record) -> bool:
    """root 拥有全部；admin 仅拥有 created_by 等于自己的记录。

    created_by 为空的历史/内置记录按 root 管理处理，避免旧数据被任意 admin 接管。
    """
    return is_root(user) or getattr(record, "created_by", None) == user.id


def require_owner(user: User, record, message: str = "无权管理该资源") -> None:
    if not owns_resource(user, record):
        raise HTTPException(status.HTTP_403_FORBIDDEN, message)


def scope_owned(query, model, user: User):
    """管理列表查询作用域：root 全部，admin 本人创建 + 已开放资源。"""
    if is_root(user):
        return query
    if not hasattr(model, "is_public"):
        return query.filter(model.created_by == user.id)
    return query.filter((model.created_by == user.id) | (model.is_public.is_(True)))


def can_manage(user: User, record) -> bool:
    """是否允许编辑/删除/关闭/取消开放。"""
    return owns_resource(user, record)


def can_use(user: User, record) -> bool:
    """是否允许在其他资源中引用。公开资源可被所有 admin 使用。"""
    return owns_resource(user, record) or bool(getattr(record, "is_public", False))


def can_access_agent(user: User, agent) -> bool:
    """是否允许主体在对话/开放 API 中使用某个智能体。

    默认智能体和公开智能体对所有已认证主体可用；root 可用全部；资源创建者可用自己的
    私有智能体。必须在列表、表单、提交和 worker 执行前使用同一规则，避免仅隐藏列表
    但仍可按递增 ID 直接调用。
    """
    if user is None or agent is None or not bool(getattr(agent, "enabled", False)):
        return False
    return bool(
        is_root(user)
        or getattr(agent, "created_by", None) == user.id
        or getattr(agent, "is_public", False)
        or getattr(agent, "is_default", False)
    )


def require_use(user: User, record, message: str = "无权使用该资源") -> None:
    if not can_use(user, record):
        raise HTTPException(status.HTTP_403_FORBIDDEN, message)


# ---------- 开放 API Key ----------

def generate_api_key() -> str:
    return "sk-gca-" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def verify_api_key(
    request: Request,
    x_api_key: str = Header(default="", alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> ApiKey:
    ip_key = client_ip(request)
    enforce(
        "open-api-ip", ip_key,
        settings.OPEN_API_RATE_LIMIT,
        settings.OPEN_API_RATE_WINDOW_SECONDS,
    )
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 X-API-Key 请求头")
    record = (
        db.query(ApiKey)
        .filter(ApiKey.key_hash == hash_api_key(x_api_key), ApiKey.is_active.is_(True))
        .first()
    )
    if record is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 无效或已停用")
    reset("open-api-ip", ip_key)
    enforce(
        "open-api-key", record.key_hash,
        settings.OPEN_API_RATE_LIMIT,
        settings.OPEN_API_RATE_WINDOW_SECONDS,
    )
    return record
