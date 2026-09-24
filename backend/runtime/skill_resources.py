"""已授权 Skill 快照中的文本资源；绝不按模型提供的路径读取磁盘。"""
import json


def _resource_path(value) -> str | None:
    """仅规范化安全相对路径，不解析父目录、盘符、URL 或绝对路径。"""
    if not isinstance(value, str):
        return None
    path = value.strip().replace("\\", "/")
    if not path or path.startswith("/") or ":" in path or any(ord(c) < 32 for c in path):
        return None
    parts = path.split("/")
    if ".." in parts:
        return None
    normalized = "/".join(part for part in parts if part not in {"", "."})
    if normalized.casefold() in {"skill", "skill.md", "skill/skill.md"}:
        return "SKILL.md"
    return normalized or None


class SkillResourceIndex:
    """让提示词、工具描述和实际读取共享同一份受控资源目录。"""

    def __init__(self, skills: list[dict] | None):
        self._skills: dict[str, dict[str, dict]] = {}
        self._descriptions: dict[str, str] = {}
        for skill in skills or []:
            if not isinstance(skill, dict) or not isinstance(skill.get("name"), str):
                continue
            name = skill["name"]
            if not name:
                continue
            resources = self._skills.setdefault(name, {})
            self._descriptions[name] = str(skill.get("description") or "")
            for item in skill.get("resources") or []:
                if not isinstance(item, dict):
                    continue
                path = _resource_path(item.get("name"))
                if path is None:
                    continue
                resources[path] = {
                    "content": item.get("content"),
                    "binary": bool(item.get("binary")),
                }
            # instructions 是当前执行快照真正加载的技能入口，优先于同名附件。
            if isinstance(skill.get("instructions"), str):
                resources["SKILL.md"] = {"content": skill["instructions"], "binary": False}

    def __bool__(self) -> bool:
        return bool(self._skills)

    @property
    def skill_names(self) -> list[str]:
        return list(self._skills)

    @property
    def descriptors(self) -> list[dict]:
        """Discovery metadata only; no entry/reference bodies are included."""
        return [{
            "name": name, "description": self._descriptions.get(name, ""),
            "resources": self.readable_names(name), "assets": self.asset_names(name),
        } for name in self.skill_names]

    def descriptor_prompt(self) -> str:
        blocks = []
        for descriptor in self.descriptors:
            text = f"### 技能：{descriptor['name']}"
            if descriptor["description"]:
                text += f"\n适用场景：{descriptor['description']}"
            if descriptor["resources"]:
                text += "\n可按需读取文本资源：" + "、".join(descriptor["resources"])
            if "SKILL.md" in descriptor["resources"]:
                text += (
                    "\n使用此技能前先调用 read_skill_resource，skill 为技能名、file 为 SKILL.md；"
                    "技能正文按需加载，其他资源使用上列准确名称。"
                )
            if descriptor["assets"]:
                text += "\n随 Skill 安装的非文本资产（由系统能力使用）：" + "、".join(descriptor["assets"])
            blocks.append(text)
        return "\n\n".join(blocks)

    def readable_names(self, skill: str) -> list[str]:
        names = [
            name for name, item in self._skills.get(skill, {}).items()
            if not item["binary"] and isinstance(item["content"], str)
        ]
        return sorted(names, key=lambda name: (name != "SKILL.md", name))

    def asset_names(self, skill: str) -> list[str]:
        return [
            name for name, item in self._skills.get(skill, {}).items()
            if item["binary"] or not isinstance(item["content"], str)
        ]

    def read(self, skill, file) -> str:
        def failure(code: str, message: str, **details) -> str:
            return json.dumps({
                "ok": False,
                "error": {"type": "skill_resource_error", "code": code, "message": message},
                **details,
            }, ensure_ascii=False)

        if not isinstance(skill, str) or skill not in self._skills:
            return failure(
                "skill_not_available", "技能不在本次已授权的技能快照中。",
                available_skills=self.skill_names,
            )
        available = self.readable_names(skill)
        details = {
            "skill": skill,
            "available_resources": available,
            "hint": (
                "使用列出的准确资源名；技能入口为 SKILL.md。不要原样重复失败请求。"
                if "SKILL.md" in available else
                "使用列出的准确文本资源名；本技能快照未提供入口文本。不要原样重复失败请求。"
            ),
        }
        path = _resource_path(file)
        if path is None:
            return failure(
                "skill_resource_invalid_path", "资源名必须是技能目录内的安全相对路径。", **details,
            )
        item = self._skills[skill].get(path)
        if item is None:
            return failure("skill_resource_not_found", "未找到该技能文本资源。", **details)
        if item["binary"] or not isinstance(item["content"], str):
            return failure(
                "skill_resource_not_text", "该资源不是可读取的文本；请使用已授权的系统能力处理资产。",
                **details,
            )
        return json.dumps({
            "ok": True, "skill": skill, "file": path, "content": item["content"],
        }, ensure_ascii=False)
