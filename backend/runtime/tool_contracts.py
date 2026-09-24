"""工具定义、执行绑定与策略选择的单一目录。

模型路由只选择候选工具；本目录固定本轮授权边界，执行器必须从同一目录取绑定。
"""
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal

from .policies import RuntimePolicies


ToolKind = Literal["control", "builtin", "resource", "skill", "agent", "mcp"]
ToolEffect = Literal["read", "write", "external", "unknown"]

# Explicit allowlists: names/descriptions supplied by an external provider must
# never make an unknown operation parallel or safe to replay.
_READ_ONLY_BUILTINS = frozenset({
    "ls", "glob", "grep", "read", "read_many", "git_status", "git_diff",
    "document_inspect", "web_search", "web_fetch", "CronList", "list_agents",
})
_WRITE_BUILTINS = frozenset({
    "write", "edit", "multi_edit", "apply_patch", "git_commit",
    "html_generate", "document_create", "document_format", "image_render",
    "CronCreate", "CronDelete", "CronSetEnabled",
})
_EXTERNAL_BUILTINS = frozenset({
    "image_generate", "browser_open", "browser_click", "browser_type",
    "browser_screenshot", "browser_close", "spawn_agent", "resume_agent",
    "interrupt_agent", "close_agent",
})


@dataclass(frozen=True)
class McpToolBinding:
    connection: Any
    remote_name: str
    risk: dict
    input_schema: dict
    server_id: int
    server_name: str
    output_schema: dict | None = None


@dataclass(frozen=True)
class ToolContract:
    name: str
    kind: ToolKind
    definition: dict
    binding: Any = None

    @property
    def input_schema(self) -> dict:
        return (self.definition.get("function") or {}).get("parameters") or {}

    @property
    def effects(self) -> ToolEffect:
        if self.kind == "resource":
            return "read" if self.name == "read_skill_resource" else "unknown"
        if self.kind == "builtin":
            if self.name in _READ_ONLY_BUILTINS:
                return "read"
            if self.name in _WRITE_BUILTINS:
                return "write"
            if self.name in _EXTERNAL_BUILTINS:
                return "external"
        if self.kind in {"control", "skill"}:
            return "write"
        if self.kind == "agent":
            return "external"
        if self.kind == "mcp" and isinstance(self.binding, McpToolBinding):
            risk = self.binding.risk or {}
            if (risk.get("classification_source") == "annotation"
                    and risk.get("mutating") is False
                    and risk.get("destructive") is False):
                return "read"
            return "external" if risk.get("mutating") is True else "unknown"
        return "unknown"

    @property
    def read_only(self) -> bool:
        return self.effects == "read"

    @property
    def capability_revision(self) -> str:
        """Fingerprint the dispatch contract, excluding ephemeral connections.

        Reordering JSON keys does not invalidate a grant; changes to schemas,
        permissions/effects, or an MCP target do. The runtime version belongs in
        the hash so a future incompatible implementation can invalidate grants.
        """
        identity: dict = {}
        if isinstance(self.binding, McpToolBinding):
            identity = {
                "server_id": self.binding.server_id,
                "server_name": self.binding.server_name,
                "remote_name": self.binding.remote_name,
                "risk": self.binding.risk,
                "input_schema": self.binding.input_schema,
                "output_schema": self.binding.output_schema,
            }
        elif isinstance(self.binding, dict):
            identity = {key: self.binding[key] for key in (
                "id", "name", "version", "harness_id", "harness_version_id",
                "tool_policy", "memory_policy", "verification_policy", "output_policy",
            ) if key in self.binding}
        payload = {"contract_version": 1, "name": self.name, "kind": self.kind,
                   "definition": self.definition, "effects": self.effects,
                   "binding": identity}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolCatalog:
    """按注册顺序保存契约；策略选择产生仅包含已授权绑定的新目录。"""

    def __init__(self) -> None:
        self._entries: dict[str, ToolContract] = {}

    def register(self, definition: dict, kind: ToolKind, binding: Any = None) -> None:
        name = str((definition.get("function") or {}).get("name") or "")
        if not name or name in self._entries:
            raise ValueError(f"工具名称缺失或重复：{name}")
        self._entries[name] = ToolContract(name, kind, definition, binding)

    @property
    def names(self) -> set[str]:
        return set(self._entries)

    @property
    def definitions(self) -> list[dict]:
        return [entry.definition for entry in self._entries.values()]

    def get(self, name: str) -> ToolContract | None:
        return self._entries.get(name)

    def require(self, name: str) -> ToolContract:
        entry = self.get(name)
        if entry is None:
            raise ValueError(f"工具未绑定或已被策略禁用：{name}")
        return entry

    def is_kind(self, name: str, kind: ToolKind) -> bool:
        entry = self.get(name)
        return entry is not None and entry.kind == kind

    def select(self, policies: RuntimePolicies) -> "ToolCatalog":
        selected = ToolCatalog()
        allowed, denied = set(policies.allowed_tools), set(policies.denied_tools)
        for entry in self._entries.values():
            # 保留既有语义：全禁用模式仍提供计划控制；其余模式允许显式禁用计划。
            if policies.tool_mode == "disabled":
                permitted = entry.name == "update_plan"
            else:
                permitted = entry.name not in denied and (
                    entry.name == "update_plan"
                    or policies.tool_mode != "allowlist"
                    or entry.name in allowed
                )
            if permitted:
                selected._entries[entry.name] = entry
        return selected


def unavailable_tool_result(name: str) -> dict:
    return {
        "ok": False,
        "error": {
            "type": "tool_unavailable",
            "code": "not_available",
            "message": f"工具未绑定或已被策略禁用：{name}",
        },
    }
