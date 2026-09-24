/* Public execution facts only. Shared by the browser and Node behavior tests. */
(function (root, factory) {
  const workspace = factory();
  if (typeof module === "object" && module.exports) module.exports = workspace;
  else root.RunWorkspace = workspace;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PHASES = {
    prepare: "准备", decide: "决策", act: "行动", observe: "观察",
    verify: "验证", approval: "需要确认", completed: "已完成",
    completed_with_issues: "受限完成", failed: "执行失败", cancelled: "已停止",
  };
  const TERMINAL = new Set(["completed", "completed_with_issues", "failed", "cancelled"]);

  function eventCategory(type = "") {
    if (type === "guardrail.content_evaluated") return "verification";
    if (type === "tools.selection") return "tools";
    if (/^(tool\.|delegation\.)/.test(type) || ["capability.activated", "invocation.reused", "recovery.blocked"].includes(type)) return "tools";
    if (/^approval\./.test(type)) return "approval";
    if (/^(verification\.|evaluation\.|plan\.closeout\.)/.test(type)) return "verification";
    return "activity";
  }

  function phaseForEvent(type, payload = {}) {
    // A nested agent's loop is evidence for its parent, not the parent's lifecycle.
    if (payload.execution_scope === "inline_subagent") return null;
    if (type === "recovery.resumed" || type === "capability.activated") return "prepare";
    if (type === "task.contract.updated") return "decide";
    if (type === "invocation.reused" || type === "recovery.blocked") return "observe";
    if (type === "verification.repair.started") return "act";
    if (type === "verification.repair.completed") return "verify";
    if (type === "guardrail.content_evaluated") return "verify";
    if (["model.role.selected", "model.role.fallback"].includes(type)) {
      return payload.role === "critic" ? "verify"
        : ["planner", "router"].includes(payload.role) ? "decide" : "prepare";
    }
    if (type === "tools.selection") return "decide";
    const status = payload.completion_status || payload.status || "";
    if (/^(task\.|turn\.|loop\.)/.test(type)) {
      if (TERMINAL.has(status)) return status;
      if (status === "done") return "completed";
      if (["waiting_approval", "awaiting_approval"].includes(status)) return "approval";
      if (status === "finalizing") return "verify";
      if (status === "planning") return "decide";
      if (status === "executing") return "act";
    }
    if (["task.completed_with_issues"].includes(type)) return "completed_with_issues";
    if (["task.completed", "turn.completed", "loop.completed"].includes(type)) {
      return payload.checkpoint?.status === "completed_with_issues"
        ? "completed_with_issues" : "completed";
    }
    if (type === "task.failed") return "failed";
    if (type === "task.cancelled") return "cancelled";
    if (type === "approval.requested") return "approval";
    if (type === "approval.granted") return "prepare";
    if (/^(verification\.|evaluation\.|plan\.closeout\.)/.test(type)) return "verify";
    if (["tool.completed", "tool.failed", "delegation.completed", "delegation.failed",
      "loop.iteration.completed"].includes(type)) return "observe";
    if (["tool.called", "step.started", "delegation.started"].includes(type)) return "act";
    if (["loop.iteration.started", "plan.created", "plan.updated", "tools.routed"].includes(type)) return "decide";
    if (["task.queued", "task.started", "turn.started", "memory.resolved",
      "attachments.resolved", "attachments.materialized", "attachments.visual_source", "provider.routed"].includes(type)) return "prepare";
    return null;
  }

  function createProjection() {
    return {phase: "", detail: "", visited: [], seen: new Set(), limited: false};
  }

  function contentGuardrailPresentation(payload = {}) {
    const points = {user_input: "用户输入", tool_input: "工具输入", tool_output: "工具输出", model_output: "模型输出"};
    const point = typeof payload.point === "string" && Object.prototype.hasOwnProperty.call(points, payload.point) ? points[payload.point] : "内容";
    const decisions = {
      allow: {label: "本次未拦截", kind: "done"},
      warn: {label: "命中提醒规则", kind: "warning"},
      block: {label: "已阻止内容传递", kind: "warning"},
    };
    const decision = typeof payload.decision === "string" && Object.prototype.hasOwnProperty.call(decisions, payload.decision)
      ? decisions[payload.decision] : {label: "判定未知", kind: "warning"};
    // Detector metadata can explain the boundary; original content never enters
    // this label, and a block event alone does not declare the whole task failed.
    return {text: `${point}护栏：${decision.label}`, kind: decision.kind};
  }

  function recoveryPresentation(type, payload = {}) {
    // These events expose progress and identities only, never raw inputs or results.
    const count = key => Number.isSafeInteger(payload[key]) && payload[key] >= 0 ? payload[key] : 0;
    const identifier = (key, limit) => typeof payload[key] === "string"
      && payload[key].length <= limit && /^[\p{L}\p{N}_.:-]+$/u.test(payload[key]) ? payload[key] : "";
    if (type === "task.contract.updated") {
      const action = payload.mode === "redirect" ? "已更新任务目标"
        : payload.mode === "guide" ? "已吸收任务补充要求" : "已更新任务要求";
      return {text: `${action}（第 ${count("revision")} 版）`, kind: "done"};
    }
    if (type === "capability.activated") {
      const kinds = {skill: "技能", mcp: "MCP 工具", builtin: "内置工具", agent: "专家"};
      const kind = Object.prototype.hasOwnProperty.call(kinds, payload.kind) ? kinds[payload.kind] : "能力";
      const name = identifier("name", 128);
      return {text: `已启用${kind}${name ? `：${name}` : ""}`, kind: "done"};
    }
    if (type === "recovery.resumed") {
      return {text: `已恢复保存的进度（第 ${count("checkpoint_revision")} 版，第 ${count("iteration")} 轮）`, kind: "done"};
    }
    if (type === "invocation.reused" || type === "recovery.blocked") {
      const tool = identifier("tool", 80);
      const label = tool ? `工具 ${tool}` : "工具";
      return type === "invocation.reused"
        ? {text: `已复用${label}的已有结果`, kind: "done"}
        : {text: `${label}结果未知，恢复已暂停，需先核实外部状态`, kind: "warning"};
    }
    if (type === "verification.repair.started") {
      return {text: `开始调用工具修复验证问题（第 ${count("attempt")} 次，${count("issues_count")} 项）`, kind: "stage-running"};
    }
    if (type === "verification.repair.completed") {
      return payload.ok === true
        ? {text: `第 ${count("attempt")} 次工具修复完成，正在复核`, kind: "done"}
        : {text: `第 ${count("attempt")} 次工具修复仍有未解决问题`, kind: "warning"};
    }
    return null;
  }

  function observe(projection, event = {}, detail = "") {
    const type = event.event_type || event.type || "";
    const payload = event.payload || {};
    const key = String(event.event_id || "");
    if (key && projection.seen.has(key)) return false;
    if (key) projection.seen.add(key);
    let phase = phaseForEvent(type, payload);
    if (!phase) return false;
    if (phase === "completed_with_issues") projection.limited = true;
    if (phase === "completed" && projection.limited) phase = "completed_with_issues";
    if (TERMINAL.has(projection.phase) && !TERMINAL.has(phase)) return false;
    projection.phase = phase;
    const presentation = type === "guardrail.content_evaluated"
      ? contentGuardrailPresentation(payload) : recoveryPresentation(type, payload);
    projection.detail = presentation?.text || String(detail || PHASES[phase]);
    if (!projection.visited.includes(phase)) projection.visited.push(phase);
    return true;
  }

  function approvalFromSnapshot(snapshot = {}) {
    if (snapshot.status !== "awaiting_approval") return null;
    return {
      scope: String(snapshot.approval_scope || ""),
      description: String(snapshot.approval_description || snapshot.approval_scope || "当前操作"),
    };
  }

  function createApprovalRequest(request = {}) {
    return {...request, pending: false, resolved: false, error: ""};
  }

  async function decideApproval(request, action, execute) {
    if (request.pending || request.resolved) return false;
    if (!["approve", "cancel"].includes(action)) throw new Error("无效审批动作");
    request.pending = true;
    request.error = "";
    try {
      await execute(action);
      request.resolved = true;
      return true;
    } catch (error) {
      request.error = String(error?.message || "操作未成功，请重试");
      return false;
    } finally {
      request.pending = false;
    }
  }

  function nearLatest({scrollHeight, scrollTop, clientHeight}, threshold = 72) {
    return scrollHeight - scrollTop - clientHeight <= threshold;
  }

  function matchesFilter(category, filter) {
    return filter === "all" || category === filter;
  }

  return {PHASES, createProjection, observe, phaseForEvent, eventCategory, contentGuardrailPresentation, recoveryPresentation,
    approvalFromSnapshot, createApprovalRequest, decideApproval, nearLatest, matchesFilter};
});
