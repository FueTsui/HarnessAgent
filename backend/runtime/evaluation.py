"""Evidence-backed evaluation for a completed Agent Turn.

The execution Harness already records plans, tools, controls and verification
facts.  This module turns those facts into a small, deterministic Evidence Tree
instead of asking another model for an opaque score.  It deliberately stores
references and summaries only; raw tool output and model reasoning never enter
the public report.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Iterable, Mapping, Sequence


EVALUATION_VERSION = "1.0"


_SKILL_LABELS = {
    "output_contract": "输出契约",
    "verification_policy": "完成条件",
    "plan_completion": "计划完成度",
    "evidence_grounding": "工具与证据",
    "interaction_control": "全双工交互",
    "artifact_delivery": "产物交付",
}


def _skill(name: str, reason: str) -> dict:
    return {
        "name": name,
        "label": _SKILL_LABELS[name],
        "version": EVALUATION_VERSION,
        "reason": reason,
    }


def select_evaluation_skills(
    *,
    plan_steps: Sequence[Mapping] = (),
    required_evidence_tools: Sequence[str] = (),
    require_successful_tool: bool = False,
    successful_tool_names: Iterable[str] = (),
    interactions: Sequence[Mapping] = (),
    artifact_kinds: Iterable[str] = (),
) -> list[dict]:
    """Select only evaluation capabilities that have a real task signal."""
    selected = [
        _skill("output_contract", "每个 Turn 都必须返回可展示结果"),
        _skill("verification_policy", "复核本轮固化的 Harness 完成条件"),
    ]
    if plan_steps:
        selected.append(_skill("plan_completion", "本轮创建了用户可见任务计划"))
    if required_evidence_tools or require_successful_tool or set(successful_tool_names):
        selected.append(_skill("evidence_grounding", "本轮要求或实际使用了工具证据"))
    if interactions:
        selected.append(_skill("interaction_control", "本轮接收了运行中引导或重定向"))
    if set(artifact_kinds):
        selected.append(_skill("artifact_delivery", "任务包含可下载产物交付要求"))
    return selected


def _check(
    check_id: str,
    label: str,
    status: str,
    *,
    expected: str,
    observed: str,
    evidence_refs: Sequence[str] = (),
) -> dict:
    return {
        "id": check_id[:96],
        "label": label[:160],
        "status": status if status in {"passed", "failed", "unknown"} else "unknown",
        "expected": expected[:240],
        "observed": observed[:400],
        "evidence_refs": [str(value)[:160] for value in evidence_refs[:12]],
    }


def _skill_node(skill: Mapping, checks: list[dict]) -> dict:
    statuses = [item["status"] for item in checks]
    if "failed" in statuses:
        status = "failed"
    elif "unknown" in statuses or not statuses:
        status = "unknown"
    else:
        status = "passed"
    known = sum(value in {"passed", "failed"} for value in statuses)
    return {
        "id": f"skill:{skill['name']}",
        "skill": skill["name"],
        "label": skill["label"],
        "status": status,
        "confidence": round(known / len(statuses), 3) if statuses else 0.0,
        "checks": checks,
    }


def evaluate_execution(
    *,
    objective: str,
    answer: str,
    issues: Sequence[str] = (),
    hard_failure: bool = False,
    verification_required: bool = True,
    plan_steps: Sequence[Mapping] = (),
    required_evidence_tools: Sequence[str] = (),
    require_successful_tool: bool = False,
    successful_tool_names: Iterable[str] = (),
    interactions: Sequence[Mapping] = (),
    artifact_kinds: Iterable[str] = (),
    artifact_evidence: Mapping[str, Sequence[str]] | None = None,
    deferred_artifact_kinds: Iterable[str] = (),
) -> dict:
    """Build a versioned Evidence Tree from deterministic runtime facts."""
    tool_names = {str(value) for value in successful_tool_names if str(value)}
    kinds = {str(value) for value in artifact_kinds if str(value)}
    deferred = {str(value) for value in deferred_artifact_kinds if str(value)}
    artifacts = artifact_evidence if isinstance(artifact_evidence, Mapping) else {}
    selected = select_evaluation_skills(
        plan_steps=plan_steps,
        required_evidence_tools=required_evidence_tools,
        require_successful_tool=require_successful_tool,
        successful_tool_names=tool_names,
        interactions=interactions,
        artifact_kinds=kinds,
    )
    nodes: list[dict] = []
    skill_gaps: list[str] = []

    for skill in selected:
        name = skill["name"]
        checks: list[dict] = []
        if name == "output_contract":
            present = bool((answer or "").strip())
            checks.append(_check(
                "output.answer_present",
                "存在可展示的最终答复",
                "passed" if present else "failed",
                expected="非空、已清洗的最终答复",
                observed=f"{len((answer or '').strip())} 个字符",
                evidence_refs=("turn.final_output",),
            ))
        elif name == "verification_policy":
            if not verification_required:
                checks.append(_check(
                    "verification.policy_enabled",
                    "完成条件验证已启用",
                    "unknown",
                    expected="运行完成前执行确定性验证",
                    observed="当前 Harness 关闭了完成条件验证",
                    evidence_refs=("harness.verification_policy",),
                ))
                skill_gaps.append("completion_policy_disabled")
            else:
                checks.append(_check(
                    "verification.no_remaining_issues",
                    "完成条件没有未解决问题",
                    "passed" if not issues else "failed",
                    expected="0 个未解决问题",
                    observed=(
                        "0 个未解决问题" if not issues else
                        f"{len(issues)} 个：" + "；".join(str(value) for value in issues[:6])
                    ),
                    evidence_refs=("verification.completed",),
                ))
        elif name == "plan_completion":
            for index, step in enumerate(plan_steps):
                step_id = str(step.get("id") or f"step_{index + 1}")
                step_status = str(step.get("status") or "pending")
                checks.append(_check(
                    f"plan.{step_id}",
                    str(step.get("step") or f"任务步骤 {index + 1}"),
                    "passed" if step_status == "completed" else "failed",
                    expected="completed",
                    observed=step_status,
                    evidence_refs=(f"plan:{step_id}",),
                ))
        elif name == "evidence_grounding":
            for capability in required_evidence_tools:
                capability = str(capability)
                checks.append(_check(
                    f"evidence.{capability}",
                    f"取得 {capability} 证据",
                    "passed" if capability in tool_names else "failed",
                    expected="至少一次成功且可审计的调用",
                    observed="已取得" if capability in tool_names else "未取得",
                    evidence_refs=(f"tool:{capability}",),
                ))
            if require_successful_tool and not required_evidence_tools:
                checks.append(_check(
                    "evidence.any_successful_tool",
                    "至少一个工具成功返回证据",
                    "passed" if tool_names else "failed",
                    expected="成功工具数大于 0",
                    observed="、".join(sorted(tool_names)) if tool_names else "没有成功工具",
                    evidence_refs=tuple(f"tool:{value}" for value in sorted(tool_names)),
                ))
            if not checks and tool_names:
                checks.append(_check(
                    "evidence.observed_tools",
                    "工具执行结果已进入审计链",
                    "passed",
                    expected="成功工具具名记录",
                    observed="、".join(sorted(tool_names)),
                    evidence_refs=tuple(f"tool:{value}" for value in sorted(tool_names)),
                ))
        elif name == "interaction_control":
            for index, interaction in enumerate(interactions):
                interaction_id = str(interaction.get("id") or f"interaction_{index + 1}")
                mode = str(interaction.get("mode") or "guide")
                applied = interaction.get("applied") is not False
                checks.append(_check(
                    f"interaction.{interaction_id}",
                    "重定向当前目标" if mode == "redirect" else "吸收运行中补充引导",
                    "passed" if applied else "failed",
                    expected="控制事件被当前或后继 Turn 明确接收",
                    observed="已应用" if applied else "未应用",
                    evidence_refs=(f"interaction:{interaction_id}",),
                ))
        elif name == "artifact_delivery":
            for kind in sorted(kinds):
                refs = [str(value) for value in (artifacts.get(kind) or ())]
                if kind in deferred:
                    status = "unknown"
                    observed = "等待后置渲染与质量校验"
                    skill_gaps.append(f"{kind}_post_render_pending")
                else:
                    status = "passed" if refs else "failed"
                    observed = "、".join(refs) if refs else "没有可验证产物"
                checks.append(_check(
                    f"artifact.{kind}",
                    f"交付 {kind} 产物",
                    status,
                    expected="存在已注册且通过结构检查的产物",
                    observed=observed,
                    evidence_refs=tuple(f"artifact:{value}" for value in refs),
                ))
        nodes.append(_skill_node(skill, checks))

    checks = [check for node in nodes for check in node["checks"]]
    known = [check for check in checks if check["status"] in {"passed", "failed"}]
    passed = sum(check["status"] == "passed" for check in known)
    failed = sum(check["status"] == "failed" for check in known)
    unknown = sum(check["status"] == "unknown" for check in checks)
    score = round(100 * passed / len(known)) if known else 0
    coverage = round(len(known) / len(checks), 3) if checks else 0.0
    if hard_failure:
        decision = "failed"
    elif failed or unknown or issues:
        decision = "needs_attention"
    else:
        decision = "passed"
    root_status = "failed" if decision == "failed" else (
        "passed" if decision == "passed" else "unknown"
    )
    return {
        "version": EVALUATION_VERSION,
        "objective": " ".join((objective or "").split())[:240],
        "decision": decision,
        "score": score,
        "coverage": coverage,
        "confidence": coverage,
        "selected_skills": selected,
        "issue_count": len(issues),
        "skill_gaps": list(dict.fromkeys(skill_gaps))[:12],
        "summary": {"passed": passed, "failed": failed, "unknown": unknown},
        "evidence_tree": {
            "id": "evaluation-root",
            "label": "任务完成评测",
            "status": root_status,
            "children": nodes,
        },
    }


def resolve_artifact_evaluation(
    report: Mapping,
    *,
    kind: str,
    passed: bool,
    artifacts: Sequence[str] = (),
    issues: Sequence[str] = (),
) -> dict:
    """Resolve a post-render artifact check without discarding the first audit report."""
    value = deepcopy(dict(report or {}))
    tree = value.get("evidence_tree") if isinstance(value.get("evidence_tree"), dict) else {}
    for node in tree.get("children") or []:
        if not isinstance(node, dict) or node.get("skill") != "artifact_delivery":
            continue
        for check in node.get("checks") or []:
            if isinstance(check, dict) and check.get("id") == f"artifact.{kind}":
                check["status"] = "passed" if passed else "failed"
                check["observed"] = (
                    "、".join(str(item) for item in artifacts[:12]) if passed else
                    "；".join(str(item) for item in issues[:6]) or "产物校验失败"
                )[:400]
                check["evidence_refs"] = [
                    f"artifact:{str(item)[:140]}" for item in artifacts[:12]
                ]
        statuses = [item.get("status") for item in node.get("checks") or [] if isinstance(item, dict)]
        node["status"] = "failed" if "failed" in statuses else (
            "unknown" if "unknown" in statuses else "passed"
        )
        node["confidence"] = round(
            sum(item in {"passed", "failed"} for item in statuses) / len(statuses), 3
        ) if statuses else 0.0

    gaps = [
        str(item) for item in (value.get("skill_gaps") or [])
        if str(item) != f"{kind}_post_render_pending"
    ]
    value["skill_gaps"] = gaps
    all_checks = [
        check
        for node in tree.get("children") or [] if isinstance(node, dict)
        for check in (node.get("checks") or []) if isinstance(check, dict)
    ]
    known = [item for item in all_checks if item.get("status") in {"passed", "failed"}]
    passed_count = sum(item.get("status") == "passed" for item in known)
    failed_count = sum(item.get("status") == "failed" for item in known)
    unknown_count = sum(item.get("status") == "unknown" for item in all_checks)
    value["score"] = round(100 * passed_count / len(known)) if known else 0
    value["coverage"] = round(len(known) / len(all_checks), 3) if all_checks else 0.0
    value["confidence"] = value["coverage"]
    value["summary"] = {
        "passed": passed_count, "failed": failed_count, "unknown": unknown_count,
    }
    if not passed:
        value["decision"] = "failed"
        value["issue_count"] = max(int(value.get("issue_count") or 0), len(issues) or 1)
    elif failed_count or unknown_count or int(value.get("issue_count") or 0):
        value["decision"] = "needs_attention"
    else:
        value["decision"] = "passed"
    tree["status"] = {
        "failed": "failed", "needs_attention": "unknown", "passed": "passed",
    }[value["decision"]]
    value["evidence_tree"] = tree
    return value


def resolve_plan_evaluation(report: Mapping, plan_steps: Sequence[Mapping]) -> dict:
    """Refresh plan checks after deterministic artifact/template post-processing."""
    value = deepcopy(dict(report or {}))
    tree = value.get("evidence_tree") if isinstance(value.get("evidence_tree"), dict) else {}
    nodes = tree.get("children") if isinstance(tree.get("children"), list) else []
    plan_node = next(
        (item for item in nodes if isinstance(item, dict) and item.get("skill") == "plan_completion"),
        None,
    )
    if plan_node is not None:
        checks = []
        for index, step in enumerate(plan_steps):
            step_id = str(step.get("id") or f"step_{index + 1}")
            step_status = str(step.get("status") or "pending")
            checks.append(_check(
                f"plan.{step_id}",
                str(step.get("step") or f"任务步骤 {index + 1}"),
                "passed" if step_status == "completed" else "failed",
                expected="completed",
                observed=step_status,
                evidence_refs=(f"plan:{step_id}",),
            ))
        replacement = _skill_node({
            "name": "plan_completion", "label": _SKILL_LABELS["plan_completion"],
        }, checks)
        plan_node.clear()
        plan_node.update(replacement)

    all_checks = [
        check
        for node in nodes if isinstance(node, dict)
        for check in (node.get("checks") or []) if isinstance(check, dict)
    ]
    known = [item for item in all_checks if item.get("status") in {"passed", "failed"}]
    passed_count = sum(item.get("status") == "passed" for item in known)
    failed_count = sum(item.get("status") == "failed" for item in known)
    unknown_count = sum(item.get("status") == "unknown" for item in all_checks)
    value["score"] = round(100 * passed_count / len(known)) if known else 0
    value["coverage"] = round(len(known) / len(all_checks), 3) if all_checks else 0.0
    value["confidence"] = value["coverage"]
    value["summary"] = {
        "passed": passed_count, "failed": failed_count, "unknown": unknown_count,
    }
    if value.get("decision") != "failed":
        value["decision"] = (
            "needs_attention" if failed_count or unknown_count or int(value.get("issue_count") or 0)
            else "passed"
        )
    tree["status"] = {
        "failed": "failed", "needs_attention": "unknown", "passed": "passed",
    }.get(str(value.get("decision") or "needs_attention"), "unknown")
    value["evidence_tree"] = tree
    return value


def evaluation_summary(report: Mapping | None) -> dict:
    """Small result-safe projection; the complete tree remains in append-only Items."""
    source = report if isinstance(report, Mapping) else {}
    return {
        "version": str(source.get("version") or EVALUATION_VERSION)[:16],
        "decision": str(source.get("decision") or "")[:24],
        "score": int(source.get("score") or 0),
        "coverage": float(source.get("coverage") or 0),
        "confidence": float(source.get("confidence") or 0),
        "issue_count": max(0, int(source.get("issue_count") or 0)),
        "selected_skills": [
            str(item.get("name") or "")[:64]
            for item in (source.get("selected_skills") or [])[:12]
            if isinstance(item, Mapping) and item.get("name")
        ],
        "skill_gaps": [str(item)[:96] for item in (source.get("skill_gaps") or [])[:12]],
    }
