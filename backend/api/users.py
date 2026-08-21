"""用户管理接口：仅 root 权限。

root 可管理所有用户的角色，并为 admin / user 授予可访问的设置页模块。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..artifacts import delete_for_owner, purge_unreferenced_files
from ..models import (
    ADMIN_MODULE_KEYS,
    ADMIN_MODULES,
    ROLE_ADMIN,
    ROLE_ROOT,
    ROLE_USER,
    USER_MODULE_KEYS,
    Agent,
    ApiKey,
    Attachment,
    Channel,
    HarnessVersion,
    ImprovementProposal,
    Job,
    McpServer,
    ModelProvider,
    Project,
    ScheduledTask,
    Skill,
    Template,
    Thread,
    ToolApproval,
    TokenUsage,
    Turn,
    User,
    UserTokenLimit,
)
from ..schemas import UserCreate, UserOut, UserUpdate
from ..security import hash_password, require_root

router = APIRouter(prefix="/api/v1/users", tags=["用户管理（root）"])


def user_out(u: User) -> UserOut:
    """构造 UserOut；admin 空授权代表全部，user 空授权代表无模块。"""
    raw = (getattr(u, "permissions", "") or "").strip()
    if u.role == ROLE_ROOT or (u.role == ROLE_ADMIN and raw == ""):
        return UserOut(id=u.id, username=u.username, role=u.role, is_active=u.is_active,
                       all_modules=True, modules=[])
    if u.role == ROLE_USER and raw == "":
        return UserOut(id=u.id, username=u.username, role=u.role, is_active=u.is_active,
                       all_modules=False, modules=[])
    try:
        data = json.loads(raw)
        allowed = USER_MODULE_KEYS if u.role == ROLE_USER else ADMIN_MODULE_KEYS
        modules = [str(x) for x in data if str(x) in allowed] if isinstance(data, list) else []
    except json.JSONDecodeError:
        modules = []
    allowed = USER_MODULE_KEYS if u.role == ROLE_USER else ADMIN_MODULE_KEYS
    return UserOut(id=u.id, username=u.username, role=u.role, is_active=u.is_active,
                   all_modules=set(modules) == allowed, modules=modules)


def _permissions_value(role: str, all_modules, modules) -> str:
    """由角色与请求字段计算权限存储值，避免普通用户空值继承管理员语义。"""
    if role == ROLE_ROOT:
        return ""
    allowed = USER_MODULE_KEYS if role == ROLE_USER else ADMIN_MODULE_KEYS
    if all_modules is True:
        return "" if role == ROLE_ADMIN else json.dumps(sorted(allowed), ensure_ascii=False)
    if modules is None:
        return "" if role == ROLE_ADMIN else "[]"
    keys = [k for k in modules if k in allowed]
    return json.dumps(keys, ensure_ascii=False)


@router.get("/modules")
def list_modules(_: User = Depends(require_root)):
    """可授权的设置页模块目录及其适用角色。"""
    return [
        {
            "key": key,
            "label": label,
            "roles": [ROLE_ADMIN, ROLE_USER] if key in USER_MODULE_KEYS else [ROLE_ADMIN],
        }
        for key, label in ADMIN_MODULES
    ]


@router.get("", response_model=list[UserOut])
def list_users(_: User = Depends(require_root), db: Session = Depends(get_db)):
    return [user_out(u) for u in db.query(User).order_by(User.id).all()]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, _: User = Depends(require_root), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    permissions = _permissions_value(body.role, body.all_modules, body.modules)
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        permissions=permissions,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_out(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int, body: UserUpdate, current: User = Depends(require_root), db: Session = Depends(get_db)
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if user.id == current.id and (body.is_active is False or (body.role and body.role != ROLE_ROOT)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能禁用或降级自己的 root 账号")
    before_security = (
        user.password_hash, user.role, user.is_active, user.permissions
    )
    if body.password:
        user.password_hash = hash_password(body.password)
    if body.role:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    # root 始终全部；admin / user 按各自可授予目录保存。角色变化时重新归一化，
    # 避免 admin 的治理权限在降为普通用户后残留。
    role_changed = body.role is not None and body.role != before_security[1]
    if user.role == ROLE_ROOT:
        user.permissions = ""
    elif body.all_modules is True:
        user.permissions = _permissions_value(user.role, True, body.modules)
    elif body.modules is not None:
        user.permissions = _permissions_value(user.role, False, body.modules)
    elif role_changed:
        user.permissions = _permissions_value(user.role, False, [])
    if before_security != (
        user.password_hash, user.role, user.is_active, user.permissions
    ):
        user.token_version = int(user.token_version or 0) + 1
    db.commit()
    db.refresh(user)
    return user_out(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, current: User = Depends(require_root), db: Session = Depends(get_db)):
    if user_id == current.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除自己的账号")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    dependencies = [
        ("模型提供商", db.query(ModelProvider).filter(ModelProvider.created_by == user_id).count()),
        ("智能体", db.query(Agent).filter(Agent.created_by == user_id).count()),
        ("Harness 版本", db.query(HarnessVersion).filter(HarnessVersion.created_by == user_id).count()),
        ("改进提案", db.query(ImprovementProposal).filter(ImprovementProposal.created_by == user_id).count()),
        ("MCP", db.query(McpServer).filter(McpServer.created_by == user_id).count()),
        ("技能", db.query(Skill).filter(Skill.created_by == user_id).count()),
        ("模板", db.query(Template).filter(Template.created_by == user_id).count()),
        ("API Key", db.query(ApiKey).filter(ApiKey.created_by == user_id).count()),
        ("渠道", db.query(Channel).filter(Channel.created_by == user_id).count()),
        ("项目", db.query(Project).filter(Project.user_id == user_id).count()),
        ("Thread", db.query(Thread).filter(Thread.owner_id == user_id).count()),
        ("Turn", db.query(Turn).filter(Turn.owner_id == user_id).count()),
        ("任务", db.query(Job).filter(Job.owner_id == user_id).count()),
        ("定时任务", db.query(ScheduledTask).filter(ScheduledTask.owner_id == user_id).count()),
        ("工具审批", db.query(ToolApproval).filter(ToolApproval.user_id == user_id).count()),
        ("附件", db.query(Attachment).filter(Attachment.owner_id == user_id).count()),
    ]
    bound = [f"{label} {count}" for label, count in dependencies if count]
    if bound:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该用户仍有关联数据，请先转移或删除：" + "、".join(bound),
        )
    # Token usage 是不可变审计账本：删除账号前保留用户名快照并解除用户外键，
    # 不把历史用量当作账号删除的阻塞项。
    db.query(TokenUsage).filter(TokenUsage.user_id == user_id).update(
        {TokenUsage.username: user.username, TokenUsage.user_id: None},
        synchronize_session=False,
    )
    db.query(UserTokenLimit).filter(UserTokenLimit.user_id == user_id).delete(
        synchronize_session=False
    )
    # 运行产物是有保留期的账号私有派生文件，不应像业务对象一样永久阻塞账号
    # 删除，也不应静默转移给其他用户。先在同一事务删除元数据，提交成功后再
    # 清理无人引用的文件；若后续外键检查失败，元数据会随事务一起回滚。
    artifact_filenames = delete_for_owner(db, user_id)

    # 0011 已把旧 conversations 回填至 Thread/Turn；存量数据库仍保留该退出
    # 运行时的表及其 users.id NO ACTION 外键。删除账号时只清理这份旧副本，
    # 避免隐藏的历史行把提交变成 500。
    connection = db.connection()
    legacy = inspect(connection)
    if legacy.has_table("conversations"):
        columns = {column["name"] for column in legacy.get_columns("conversations")}
        if "user_id" in columns:
            connection.execute(
                text("DELETE FROM conversations WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
    db.delete(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该用户仍有关联数据，请先转移或删除后重试",
        ) from exc
    purge_unreferenced_files(db, artifact_filenames)
