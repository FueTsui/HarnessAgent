"""认证接口：登录、查询当前用户。"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..rate_limit import client_ip, enforce, reset
from ..schemas import ChangePasswordRequest, LoginRequest, TokenResponse, UserOut
from ..security import create_token, get_current_user, hash_password, verify_password
from ..config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    rate_key = f"{ip}:{body.username.strip().lower()}"
    enforce(
        "login-ip", ip,
        settings.LOGIN_RATE_LIMIT * 5,
        settings.LOGIN_RATE_WINDOW_SECONDS,
    )
    enforce("login", rate_key, settings.LOGIN_RATE_LIMIT, settings.LOGIN_RATE_WINDOW_SECONDS)
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用")
    # 登录请求自身无 Bearer 令牌，向审计中间件登记身份，否则操作日志中登录记录缺用户名/角色。
    request.state.audit_identity = (user.id, user.username, user.role)
    reset("login", rate_key)
    token = create_token(user)
    response.set_cookie(
        settings.AUTH_COOKIE_NAME,
        token,
        max_age=settings.JWT_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE or request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    from .users import user_out  # 局部导入避免环
    access = user_out(user)
    return TokenResponse(
        access_token=token,
        username=user.username,
        role=user.role,
        all_modules=access.all_modules,
        modules=access.modules,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    from .users import user_out  # 局部导入避免环
    return user_out(user)


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """所有登录用户均可修改自己的密码（需验证旧密码）。"""
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "旧密码错误")
    if body.old_password == body.new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与旧密码相同")
    user.password_hash = hash_password(body.new_password)
    user.token_version = int(user.token_version or 0) + 1
    db.commit()
    response.delete_cookie(settings.AUTH_COOKIE_NAME, path="/", samesite="strict")
    return {"message": "密码修改成功，请使用新密码重新登录"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 服务端递增令牌版本，使本次签发的 Cookie/Bearer 令牌立即失效；仅删除
    # 浏览器 Cookie 无法撤销已复制或被窃取的同一令牌。
    user.token_version = int(user.token_version or 0) + 1
    db.commit()
    response.delete_cookie(settings.AUTH_COOKIE_NAME, path="/", samesite="strict")
