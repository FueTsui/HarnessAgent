"""Public execution views shared by live streams and persisted replay.

This module projects append-only runtime facts into bounded, public fields.
It has no database, model, or transport dependencies.
"""

import re


_RECOVERY_PROCESS_EVENTS = {
    "task.contract.updated", "capability.activated", "invocation.reused",
    "recovery.resumed", "recovery.blocked",
    "verification.repair.started", "verification.repair.completed",
}


def _public_identifier(value, limit: int = 96) -> str:
    """Accept bounded identifiers, never stringify nested private payloads."""
    return value if isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,%d}" % limit, value) else ""


PUBLIC_PROCESS_EVENTS = {
    "runtime.started", "verification.started",
    "delegation.started", "delegation.completed", "delegation.failed",
    "delegation.awaiting_approval",
    "turn.progress", "plan.created", "plan.updated", "memory.resolved", "tools.routed",
    "plan.closeout.started", "plan.closeout.completed",
    "evidence.required", "verification.failed", "verification.completed",
    "evaluation.started", "evaluation.completed",
    "context.compacted", "guidance.claimed", "guidance.applied",
    "interaction.interrupt.received", "interaction.redirect.queued",
    "interaction.redirect.created", "interaction.redirect.applied",
    "turn.started", "turn.completed", "loop.iteration.started", "loop.stopped",
    "loop.completed", "loop.blocked", "task.queued", "task.started", "task.status",
    "task.completed", "task.completed_with_issues", "task.failed", "task.cancelled",
    "step.started",
    "step.completed", "step.failed", "step.blocked", "step.skipped", "approval.requested",
    "approval.granted", "approval.policy", "approval.auto_approved",
    "attachments.resolved", "attachments.materialized", "attachments.visual_source",
    "provider.routed", "provider.attempt", "provider.fallback",
    "model.role.selected", "model.role.fallback", "tools.selection",
    "guardrail.content_evaluated",
} | _RECOVERY_PROCESS_EVENTS


def is_public_process_event(event_type: str) -> bool:
    return event_type in PUBLIC_PROCESS_EVENTS or event_type.startswith(("turn.", "loop.", "tool."))


def runtime_event_type(item, payload: dict) -> str:
    """Tool Items name the tool; recover their lifecycle event without rewriting history."""
    if item.kind in {"tool_call", "tool_result"}:
        explicit = str(payload.get("_event_type") or "")
        if explicit.startswith("tool."):
            return explicit
        if str(item.name or "").startswith("tool."):
            return item.name
        if item.kind == "tool_call":
            return "tool.called"
        if item.status == "deferred":
            return "tool.deferred"
        return "tool.completed"
    return str(item.name or "")


def public_process_payload(event_type: str, payload: dict) -> dict:
    """One projection for live events and replay, retaining execution identity."""
    source = payload if isinstance(payload, dict) else {}
    result = _payload_fields(event_type, source)
    for key in ("execution_scope", "parent_run_id", "child_run_id", "delegation_call_id", "call_id"):
        if source.get(key):
            value = _public_identifier(source[key]) if event_type in _RECOVERY_PROCESS_EVENTS else str(source[key])[:96]
            if value:
                result[key] = value
    for key in ("parent_agent_id", "agent_id", "subagent_depth"):
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result


def _public_checkpoint(value) -> dict:
    source = value if isinstance(value, dict) else {}
    result = {}
    for key in ("loop_version", "status", "stop_reason"):
        if key in source:
            result[key] = str(source[key] or "")[:120]
    for key in ("iteration", "model_calls", "successful_tools", "revision_count",
                "memory_candidates", "memory_selected"):
        if key in source:
            try:
                result[key] = max(0, int(source[key] or 0))
            except (TypeError, ValueError, OverflowError):
                result[key] = 0
    for key in ("successful_tool_names", "required_evidence_tools", "verification_issues"):
        values = source.get(key)
        if isinstance(values, (list, tuple)):
            result[key] = [str(item)[:400] for item in values[:24]]
    return result


def _payload_fields(event_type: str, payload: dict) -> dict:
    """只回放前端实际展示的安全字段，不把工具参数或模型内部推理发给浏览器。"""
    def nonnegative_int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def public_plan_summary(value) -> dict:
        source = value if isinstance(value, dict) else {}
        result = {
            key: nonnegative_int(source.get(key))
            for key in (
                "total", "completed", "failed", "blocked", "skipped",
                "pending", "in_progress",
            )
        }
        result["terminalized"] = bool(source.get("terminalized"))
        result["all_completed"] = bool(source.get("all_completed"))
        return result

    def public_issues(value) -> list[str]:
        source = value if isinstance(value, (list, tuple)) else []
        return [
            str(item)[:400] for item in source[:20]
            if str(item).strip()
        ]

    def public_evaluation(value) -> dict:
        source = value if isinstance(value, dict) else {}
        selected = []
        for item in (source.get("selected_skills") or [])[:12]:
            if isinstance(item, dict):
                selected.append({
                    "name": str(item.get("name") or "")[:64],
                    "label": str(item.get("label") or "")[:80],
                    "version": str(item.get("version") or "")[:16],
                    "reason": str(item.get("reason") or "")[:180],
                })
            else:
                selected.append({"name": str(item)[:64]})
        tree = source.get("evidence_tree") if isinstance(source.get("evidence_tree"), dict) else {}
        children = []
        for node in (tree.get("children") or [])[:12]:
            if not isinstance(node, dict):
                continue
            checks = []
            for check in (node.get("checks") or [])[:24]:
                if not isinstance(check, dict):
                    continue
                checks.append({
                    "id": str(check.get("id") or "")[:96],
                    "label": str(check.get("label") or "")[:160],
                    "status": str(check.get("status") or "unknown")[:16],
                    "expected": str(check.get("expected") or "")[:240],
                    "observed": str(check.get("observed") or "")[:400],
                    "evidence_refs": [
                        str(ref)[:160] for ref in (check.get("evidence_refs") or [])[:12]
                    ],
                })
            children.append({
                "id": str(node.get("id") or "")[:96],
                "skill": str(node.get("skill") or "")[:64],
                "label": str(node.get("label") or "")[:80],
                "status": str(node.get("status") or "unknown")[:16],
                "confidence": float(node.get("confidence") or 0),
                "checks": checks,
            })
        summary = source.get("summary") if isinstance(source.get("summary"), dict) else {}
        return {
            "version": str(source.get("version") or "")[:16],
            "objective": str(source.get("objective") or "")[:240],
            "decision": str(source.get("decision") or "")[:24],
            "score": nonnegative_int(source.get("score")),
            "coverage": float(source.get("coverage") or 0),
            "confidence": float(source.get("confidence") or 0),
            "issue_count": nonnegative_int(source.get("issue_count")),
            "selected_skills": selected,
            "skill_gaps": [
                str(item)[:96] for item in (source.get("skill_gaps") or [])[:12]
            ],
            "summary": {
                key: nonnegative_int(summary.get(key))
                for key in ("passed", "failed", "unknown")
            },
            "evidence_tree": {
                "id": str(tree.get("id") or "evaluation-root")[:96],
                "label": str(tree.get("label") or "任务完成评测")[:80],
                "status": str(tree.get("status") or "unknown")[:16],
                "children": children,
            },
        }

    if event_type in _RECOVERY_PROCESS_EVENTS:
        def count(key) -> int:
            value = payload.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9007199254740991 else 0

        if event_type == "task.contract.updated":
            mode = payload.get("mode")
            return {"revision": count("revision"),
                    "mode": mode if isinstance(mode, str) and mode in {"guide", "redirect"} else "unknown"}
        if event_type == "capability.activated":
            kind = payload.get("kind")
            return {"kind": kind if isinstance(kind, str) and kind in {"skill", "mcp", "builtin", "agent"} else "unknown",
                    "name": _public_identifier(payload.get("name"), 128)}
        if event_type == "recovery.resumed":
            return {"checkpoint_revision": count("checkpoint_revision"), "iteration": count("iteration")}
        if event_type in {"invocation.reused", "recovery.blocked"}:
            result = {"tool": _public_identifier(payload.get("tool"), 80)}
            if event_type == "recovery.blocked":
                result["reason"] = "unknown_outcome"
            return result
        return {"attempt": count("attempt"), "issues_count": count("issues_count"),
                **({"ok": payload.get("ok") is True} if event_type.endswith("completed") else {})}

    if event_type == "runtime.started":
        return {
            "loop_version": str(payload.get("loop_version") or "")[:16],
            "agent_loop_version": str(payload.get("agent_loop_version") or "")[:16],
        }
    if event_type.startswith("delegation."):
        return {
            key: nonnegative_int(payload[key])
            for key in ("query_chars", "answer_chars") if key in payload
        }
    if event_type == "turn.progress":
        return {"text": str(payload.get("text") or "")[:256]}
    if event_type == "evaluation.started":
        return {
            "version": str(payload.get("version") or "")[:16],
            "selected_skills": [
                str(item.get("name") if isinstance(item, dict) else item)[:64]
                for item in (payload.get("selected_skills") or [])[:12]
            ],
        }
    if event_type == "evaluation.completed":
        return public_evaluation(payload)
    if event_type.startswith("interaction."):
        return {
            "mode": str(payload.get("mode") or "")[:24],
            "interrupted_turn_id": str(payload.get("interrupted_turn_id") or "")[:32],
            "successor_turn_id": str(payload.get("successor_turn_id") or "")[:32],
            "chars": nonnegative_int(payload.get("chars")),
            "cancelled_guidance": nonnegative_int(payload.get("cancelled_guidance")),
        }
    if event_type == "memory.resolved":
        return {
            "selected_count": nonnegative_int(payload.get("selected_count")),
            "candidate_count": nonnegative_int(payload.get("candidate_count")),
            "max_score": float(payload.get("max_score") or 0),
            "influence": float(payload.get("influence") or 0),
            "sources": [
                {
                    "turn_id": str(item.get("turn_id") or "")[:32],
                    "thread_id": str(item.get("thread_id") or "")[:40],
                    "thread_title": str(item.get("thread_title") or "")[:80],
                    "score": float(item.get("score") or 0),
                    "relevance": float(item.get("relevance") or 0),
                }
                for item in (payload.get("sources") or [])[:10]
                if isinstance(item, dict)
            ],
        }
    if event_type in {"plan.created", "plan.updated"}:
        return {
            "explanation": str(payload.get("explanation") or "")[:240],
            "reason": str(payload.get("reason") or "")[:240],
            "revision": nonnegative_int(payload.get("revision")),
            "steps": [
                {
                    "id": str(item.get("id") or "")[:48],
                    "step": str(item.get("step") or "")[:160],
                    "status": str(item.get("status") or "pending")[:24],
                }
                for item in (payload.get("steps") or [])[:24]
                if isinstance(item, dict)
            ],
        }
    if event_type in {"plan.closeout.started", "plan.closeout.completed"}:
        status_counts = payload.get("status_counts")
        status_counts = status_counts if isinstance(status_counts, dict) else {}
        result = {
            "reason": str(payload.get("reason") or "")[:120],
            "revision": nonnegative_int(payload.get("revision")),
            "unfinished_steps": [
                str(value)[:160] for value in (payload.get("unfinished_steps") or [])[:24]
            ],
        }
        if "status_counts" in payload:
            result["status_counts"] = {
                key: nonnegative_int(status_counts.get(key))
                for key in (
                    "completed", "failed", "blocked", "skipped",
                    "pending", "in_progress",
                )
            }
        if event_type == "plan.closeout.completed":
            for key in (
                "applied", "resolved", "terminalized", "all_completed"
            ):
                if key in payload:
                    result[key] = bool(payload.get(key))
            if "outcome" in payload:
                result["outcome"] = str(payload.get("outcome") or "")[:32]
        return result
    if event_type.startswith("step."):
        return {
            "step_id": str(payload.get("step_id") or "")[:48],
            "step": str(payload.get("step") or "")[:160],
            "status": str(payload.get("status") or "")[:24],
            "revision": nonnegative_int(payload.get("revision")),
        }
    if event_type.startswith("task."):
        result = {
            "status": str(payload.get("status") or "")[:24],
            "reason": str(payload.get("reason") or "")[:120],
            "error": str(payload.get("error") or "")[:500],
        }
        if payload.get("completion_status"):
            result["completion_status"] = str(
                payload.get("completion_status") or ""
            )[:24]
        if "completion_issues" in payload:
            result["completion_issues"] = public_issues(
                payload.get("completion_issues")
            )
        if "plan_summary" in payload:
            result["plan_summary"] = public_plan_summary(payload.get("plan_summary"))
        return result
    if event_type == "guardrail.content_evaluated":
        def enum_value(value, choices, default="unknown"):
            return value if isinstance(value, str) and value in choices else default

        decision = payload.get("decision")
        point = payload.get("point")
        result = {
            "decision": enum_value(decision, {"allow", "warn", "block"}),
            "point": enum_value(point, {"user_input", "tool_input", "tool_output", "model_output"}, ""),
            "matches": [],
        }
        values = payload.get("matches")
        for item in values[:20] if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            match = {
                "policy_name": str(item.get("policy_name") or "")[:128],
                "detector": enum_value(item.get("detector"), {"blocklist", "pii", "prompt_injection", "scan_limit"}),
                "action": enum_value(item.get("action"), {"block", "warn"}),
                "rule_index": min(10000, nonnegative_int(item.get("rule_index"))),
                "count": min(1000000, nonnegative_int(item.get("count"))),
            }
            policy_id = item.get("policy_id")
            if isinstance(policy_id, int) and not isinstance(policy_id, bool) and policy_id > 0:
                match["policy_id"] = policy_id
            result["matches"].append(match)
        provider_id = payload.get("provider_id")
        if isinstance(provider_id, int) and not isinstance(provider_id, bool) and provider_id > 0:
            result["provider_id"] = provider_id
        return result
    if event_type.startswith("approval."):
        return {
            "scope": str(payload.get("scope") or "")[:80],
            "description": str(payload.get("description") or "")[:240],
            "policy": str(payload.get("policy") or "")[:24],
            "risk": str(payload.get("risk") or "")[:24],
        }
    if event_type.startswith("turn.") or event_type.startswith("loop."):
        result = {
            key: payload.get(key)
            for key in (
                "iteration", "reason", "successful_tools",
                "budgeted_successful_tools", "max_successful_calls", "max_iterations",
                "completion_status",
            )
            if key in payload
        }
        if "checkpoint" in payload:
            result["checkpoint"] = _public_checkpoint(payload["checkpoint"])
        if "completion_issues" in payload:
            result["completion_issues"] = public_issues(
                payload.get("completion_issues")
            )
        if "plan_summary" in payload:
            result["plan_summary"] = public_plan_summary(payload.get("plan_summary"))
        return result
    if event_type.startswith("tool."):
        result = {
            "tool": str(payload.get("tool") or "")[:80],
            "targets": [str(value)[:180] for value in (payload.get("targets") or [])[:8]],
            "ok": event_type not in {"tool.rejected", "tool.failed"} and payload.get("ok") is not False,
        }
        for key in ("source", "reason", "error_type", "error_code"):
            if payload.get(key):
                result[key] = str(payload[key])[:96]
        if "iteration" in payload:
            result["iteration"] = nonnegative_int(payload["iteration"])
        return result
    if event_type == "attachments.visual_source":
        return {
            "page": min(80, nonnegative_int(payload.get("page"))),
            "source_page_count": min(80, nonnegative_int(payload.get("source_page_count"))),
            "ok": payload.get("ok") is True,
            "provider_id": nonnegative_int(payload.get("provider_id")),
            "result_chars": min(8000, nonnegative_int(payload.get("result_chars"))),
            "duration_ms": nonnegative_int(payload.get("duration_ms")),
        }
    if event_type.startswith("attachments."):
        return {
            "count": nonnegative_int(payload.get("count")),
            "inherited": bool(payload.get("inherited")),
            "continuation_of_turn_id": str(
                payload.get("continuation_of_turn_id") or ""
            )[:32],
            "names": [str(value)[:255] for value in (payload.get("names") or [])[:10]],
        }
    if event_type.startswith("provider."):
        result = {
            key: payload.get(key)
            for key in (
                "provider_id", "planned_provider_id", "from_provider_id",
                "to_provider_id", "attempt_index", "ok", "latency_ms",
            )
            if key in payload
        }
        for key in ("model", "from_model", "to_model", "route_reason", "reason"):
            if key in payload:
                result[key] = str(payload.get(key) or "")[:160]
        if isinstance(payload.get("reasoning_effort"), str) and payload["reasoning_effort"] in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "disabled", "enabled"}:
            result["reasoning_effort"] = payload["reasoning_effort"]
        if payload.get("fallback_provider_ids"):
            result["fallback_provider_ids"] = [
                nonnegative_int(value)
                for value in (payload.get("fallback_provider_ids") or [])[:8]
            ]
        if payload.get("error_class"):
            result["error_class"] = str(payload.get("error_class") or "")[:96]
        return result
    if event_type in {"model.role.selected", "model.role.fallback"}:
        role = payload.get("role")
        return {
            "role": role if isinstance(role, str) and role in {"planner", "router", "critic"} else "unknown",
            "provider_id": nonnegative_int(payload.get("provider_id")) or None,
            "inherited": bool(payload.get("inherited")),
            **({"reason": "role_unavailable"} if event_type.endswith("fallback") else {}),
        }
    if event_type == "tools.selection":
        import math
        import re

        confidence = payload.get("confidence")
        decision = payload.get("decision")
        offered = payload.get("offered")
        result = {
            "source": "model" if payload.get("source") == "model" else "deterministic",
            "decision": decision if isinstance(decision, str) and decision in {
                "accepted", "invalid_selection", "low_confidence", "model_unavailable",
            } else "invalid_selection",
            "confidence_kind": "model_estimate",
            "offered": [name for name in (offered if isinstance(offered, list) else [])[:100]
                        if isinstance(name, str) and re.fullmatch(r"[\w.:-]{1,128}", name)],
        }
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and math.isfinite(confidence) and 0 <= confidence <= 1:
            result["confidence"] = float(confidence)
        return result
    if event_type.startswith("verification."):
        result = {
            key: payload.get(key)
            for key in (
                "passed", "plan_passed", "provisional", "hard_failure",
                "repairable", "revisions", "revision", "completion_status",
            )
            if key in payload
        }
        if payload.get("issues"):
            result["issues"] = public_issues(payload.get("issues"))
        if "plan_summary" in payload:
            result["plan_summary"] = public_plan_summary(payload.get("plan_summary"))
        return result
    return {
        key: payload.get(key)
        for key in (
            "selected_count", "offered", "passed", "provisional", "guidance_id", "mode"
        )
        if key in payload
    }
