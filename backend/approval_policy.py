"""Per-Turn approval policy decisions.

The policy only controls whether an already-authorized tool call needs an
interactive approval.  It never expands the Agent's tool bindings, resource
ownership, workspace roots, shell allowlist, network guard, or platform role.
"""
from __future__ import annotations

from typing import Any


ASK = "ask"
AUTO = "auto"
FULL_ACCESS = "full_access"
DEFAULT = ASK
VALUES = (ASK, AUTO, FULL_ACCESS)

# These built-ins can create durable/external state, execute commands, or act
# on an interactive page.  AUTO keeps them interactive.  Other mutating
# built-ins are confined to the per-user/per-Agent/per-run workspace or create
# isolated artifacts, so AUTO can approve them while retaining an append-only
# decision event. Delegation stays interactive because it creates durable,
# separately billed work that is not yet idempotent.
HIGH_RISK_BUILTINS = frozenset({
    "shell",
    "git_commit",
    "browser_click",
    "browser_type",
    "CronCreate",
    "CronDelete",
    "CronSetEnabled",
    "spawn_agent",
    "resume_agent",
    "interrupt_agent",
    "close_agent",
})


def normalize(value: Any, *, default: str = DEFAULT) -> str:
    """Return a supported policy; reject unknown non-empty client values."""
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        return default
    aliases = {
        "on_request": ASK,
        "request": ASK,
        "manual": ASK,
        "help_me_approve": AUTO,
        "full": FULL_ACCESS,
        "never": FULL_ACCESS,
    }
    text = aliases.get(text, text)
    if text not in VALUES:
        raise ValueError("批准策略必须是 ask、auto 或 full_access")
    return text


def builtin_risk(tool_name: str, arguments: dict | None = None) -> str:
    """Classify an already-authorized mutating built-in for AUTO mode."""
    name = str(tool_name or "")
    args = arguments if isinstance(arguments, dict) else {}
    if name in HIGH_RISK_BUILTINS:
        return "high"
    # Creating a new file is bounded and recoverable; explicitly overwriting an
    # existing file deserves the same prompt as other high-risk mutations.
    if name == "write" and bool(args.get("overwrite")):
        return "high"
    return "write"


def requires_builtin_approval(
    policy: Any,
    *,
    tool_name: str,
    mutating: bool,
    arguments: dict | None = None,
) -> bool:
    """Whether a built-in call must surface an interactive approval."""
    if not mutating:
        return False
    selected = normalize(policy)
    if selected == FULL_ACCESS:
        return False
    if selected == AUTO:
        return builtin_risk(tool_name, arguments) == "high"
    return True


def requires_external_approval(
    policy: Any,
    *,
    mutating: bool,
    destructive: bool = False,
) -> bool:
    """Whether an MCP/external side effect must surface an approval.

    AUTO deliberately keeps every external write interactive: a nominally
    non-destructive call can still send a message, publish data, charge an
    account, or mutate a remote record.  FULL_ACCESS is the explicit opt-out.
    """
    del destructive  # retained in the signature for explicit call-site audit data
    if not mutating:
        return False
    return normalize(policy) != FULL_ACCESS
