"""Private invocation journal shared by ordinary tools and evidence preflight."""
from dataclasses import asdict
import asyncio

from ..approvals import ApprovalRequired, bind_invocation
from .control import Observation, analyze_observation
from .durability import UnknownToolOutcome


class InvocationPersistenceError(RuntimeError):
    """Never turn a journal failure into a normal model-visible tool result."""


def _persist(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except UnknownToolOutcome:
        raise
    except Exception as exc:
        raise InvocationPersistenceError("工具调用账本写入或校验失败，执行已停止") from exc


def context_snapshot(context):
    return {
        "artifacts": list(context.artifacts),
        "artifact_failures": dict(context.artifact_failures),
        "artifact_metadata": dict(context.artifact_metadata),
        "active_skill_names": sorted(context.active_skill_names),
    }


def restore_context(context, state):
    for name in state.get("artifacts", []):
        if name not in context.artifacts:
            context.artifacts.append(name)
    context.artifact_failures.update(state.get("artifact_failures", {}))
    context.artifact_metadata.update(state.get("artifact_metadata", {}))
    context.active_skill_names.update(state.get("active_skill_names", []))


async def invoke_journaled(store, contract, call_id, arguments, action, *, context,
                           max_output_chars, emit):
    """Commit an outcome before exposing it; uncertain effects never auto replay."""
    invocation = None
    # Control operations only mutate checkpoint state and must be re-applied if a
    # crash occurred before the containing batch checkpoint committed.
    if store is not None and contract.kind != "control":
        invocation = _persist(store.prepare,
            call_id, contract.name, arguments, contract.capability_revision,
            effect=contract.effects,
        )
        try:
            dispatch = _persist(store.begin, invocation)
        except UnknownToolOutcome as exc:
            await emit("recovery.blocked", {
                "tool": exc.tool, "call_id": exc.call_id, "reason": "unknown_outcome",
            })
            raise
        if not dispatch:
            restore_context(context, invocation.result.get("context", {}))
            await emit("invocation.reused", {"tool": contract.name, "call_id": call_id})
            return Observation(**invocation.result["observation"])
    try:
        with bind_invocation(invocation.binding if invocation else None):
            result = await action()
        observation = analyze_observation(result, max_output_chars)
    except ApprovalRequired:
        if invocation is not None:
            _persist(store.awaiting_approval, invocation)
        raise
    except BaseException as exc:
        # Includes timeout/cancellation: a remote service may already have
        # committed its effect even though this process received no response.
        if invocation is not None and not contract.read_only:
            _persist(store.mark_unknown, invocation)
            if not isinstance(exc, asyncio.CancelledError):
                await emit("recovery.blocked", {
                    "tool": contract.name, "call_id": call_id, "reason": "unknown_outcome",
                })
                raise UnknownToolOutcome(invocation) from exc
        raise
    if invocation is not None:
        _persist(store.complete, invocation, {
            "observation": asdict(observation), "context": context_snapshot(context),
        })
    return observation
