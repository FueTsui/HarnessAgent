"""Validated personal defaults. Resource ACL and runtime policies stay authoritative."""
import json
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy.orm import Session

from .models import Agent, User, ROLE_ROOT
from .security import can_access_agent


ThemeColor = Literal["default", "blue", "green", "yellow", "pink", "orange", "purple", "black", "custom"]
CustomColor = Annotated[str, StringConstraints(
    strict=True, min_length=7, max_length=7, pattern=r"^#[0-9a-fA-F]{6}$", to_lower=True,
)]


class PreferenceValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["system", "light", "dark"] = "system"
    theme_color: ThemeColor = "default"
    custom_color: CustomColor = "#8b5cf6"
    approval_policy: Literal["ask", "auto", "full_access"] = "ask"
    default_agent_id: int | None = Field(default=None, gt=0)
    recent_sort: Literal["priority", "updated"] = "priority"
    revision: int = Field(default=0, ge=0)


class PreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["system", "light", "dark"] | None = None
    theme_color: ThemeColor | None = None
    custom_color: CustomColor | None = None
    approval_policy: Literal["ask", "auto", "full_access"] | None = None
    default_agent_id: int | None = Field(default=None, gt=0)
    recent_sort: Literal["priority", "updated"] | None = None
    revision: int = Field(ge=0)

    @model_validator(mode="after")
    def reject_null_settings(self):
        for key in self.model_fields_set - {"default_agent_id"}:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null")
        return self


def read_preferences(user: User, db: Session) -> dict:
    try:
        value = PreferenceValues.model_validate_json(getattr(user, "preferences", "{}") or "{}").model_dump()
    except (ValueError, TypeError):
        value = PreferenceValues().model_dump()
    # A formerly privileged account cannot retain elevated execution preferences.
    if value["approval_policy"] == "full_access" and user.role != ROLE_ROOT:
        value["approval_policy"] = "ask"
    if value["default_agent_id"] is not None:
        agent = db.get(Agent, value["default_agent_id"])
        if not agent or not agent.enabled or not can_access_agent(user, agent):
            value["default_agent_id"] = None
    return value


def save_preferences(user: User, db: Session, body: PreferenceUpdate) -> dict:
    before = user.preferences or "{}"
    current = read_preferences(user, db)
    if body.revision != current["revision"]:
        raise HTTPException(409, "设置已在其他页面更新，请重新加载后保存")
    changes = body.model_dump(exclude_unset=True, exclude={"revision"})
    if changes.get("approval_policy") == "full_access" and user.role != ROLE_ROOT:
        raise HTTPException(403, "仅 root 可设置完全访问权限")
    agent_id = changes.get("default_agent_id")
    if agent_id is not None:
        agent = db.get(Agent, agent_id)
        if not agent or not agent.enabled or not can_access_agent(user, agent):
            raise HTTPException(403, "无权使用该智能体或智能体已停用")
    value = {**current, **changes, "revision": current["revision"] + 1}
    updated = db.query(User).filter(User.id == user.id, User.preferences == before).update(
        {User.preferences: json.dumps(value, ensure_ascii=False)}, synchronize_session=False
    )
    if updated != 1:
        db.rollback()
        raise HTTPException(409, "设置已在其他页面更新，请重新加载后保存")
    db.commit()
    db.refresh(user)
    return read_preferences(user, db)
