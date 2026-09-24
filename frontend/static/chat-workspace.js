/* Shared, DOM-independent rules for the conversation workspace. */
(function (root) {
  "use strict";

  function normalizePreferences(value, role) {
    if (!value || typeof value !== "object"
        || !["system", "light", "dark"].includes(value.theme)
        || !["ask", "auto", "full_access"].includes(value.approval_policy)
        || !["priority", "updated"].includes(value.recent_sort)
        || (value.default_agent_id !== null
          && (!Number.isInteger(value.default_agent_id) || value.default_agent_id < 1))) {
      throw new Error("个人设置返回异常，请刷新页面或在后台管理中检查设置");
    }
    if (value.approval_policy === "full_access" && role !== "root") {
      throw new Error("当前账户无权使用完全访问策略，请在后台管理中修改批准策略");
    }
    return {
      theme: value.theme,
      approval_policy: value.approval_policy,
      default_agent_id: value.default_agent_id,
      recent_sort: value.recent_sort,
    };
  }

  function modelRoutingPresentation(eventType, payload = {}) {
    if (["model.role.selected", "model.role.fallback"].includes(eventType)) {
      const roles = {executor: "执行", planner: "规划", router: "路由", critic: "审查"};
      const label = Object.prototype.hasOwnProperty.call(roles, payload.role)
        ? roles[payload.role] : "当前环节";
      if (eventType === "model.role.fallback") {
        return {text: `${label}模型暂不可用，已回退到主模型`, kind: "warning"};
      }
      return {text: `${label}：${payload.inherited === true ? "沿用主模型" : "已选择专用模型"}`, kind: "done"};
    }
    if (eventType !== "tools.selection") return null;
    const count = new Set(Array.isArray(payload.offered)
      ? payload.offered.filter(name => typeof name === "string" && name.trim()) : []).size;
    const confidence = payload.confidence_kind === "model_estimate"
      && typeof payload.confidence === "number" && Number.isFinite(payload.confidence)
      && payload.confidence >= 0 && payload.confidence <= 1
      ? `（模型自评 ${Math.round(payload.confidence * 100)}%）` : "";
    if (payload.decision === "accepted") {
      return {text: `工具路由：已选择 ${count} 项能力${confidence}`, kind: "done"};
    }
    const reasons = {
      invalid_selection: "工具选择未通过校验",
      low_confidence: "模型自评分未达阈值",
      model_unavailable: "路由模型不可用",
    };
    if (Object.prototype.hasOwnProperty.call(reasons, payload.decision)) {
      return {text: `${reasons[payload.decision]}，沿用规则匹配的 ${count} 项能力${confidence}`, kind: "warning"};
    }
    return {text: "工具路由状态已更新", kind: "progress"};
  }

  function newComposerSelection() {
    return {approval_policy: null, provider_id: null, reasoning_effort: ""};
  }

  function effectiveApproval(preferences, selection, role) {
    const policy = selection?.approval_policy || preferences?.approval_policy || "ask";
    if (!["ask", "auto", "full_access"].includes(policy)) throw new Error("批准策略无效，请重新选择");
    if (policy === "full_access" && role !== "root") throw new Error("完全访问仅限 root，请重新选择批准策略");
    return policy;
  }

  function selectedComposerModel(models, selection) {
    return (models || []).find(item => item.provider_id === (selection?.provider_id ?? null)) || null;
  }

  function modelAvailable(model) {
    return Boolean(model && model.available !== false && model.model !== "");
  }

  function reasoningLabel(value, model = null) {
    return ({low: "轻度", medium: "中", high: "高", xhigh: "极高", max: "最高", ultra: "Ultra"})[value]
      || model?.reasoning_effort_labels?.[value] || ({none: "关闭", disabled: "关闭", enabled: "开启", minimal: "极轻"})[value] || value || "默认";
  }

  function reasoningSlider(model, selection = {}) {
    const efforts = modelAvailable(model) && model.reasoning_supported && Array.isArray(model.reasoning_efforts)
      ? [...new Set(model.reasoning_efforts.filter(value => typeof value === "string" && value.length))] : [];
    const override = efforts.includes(selection.reasoning_effort) ? selection.reasoning_effort : "";
    const value = override || (efforts.includes(model?.reasoning_effort) ? model.reasoning_effort : "");
    return {efforts, value, index: Math.max(0, efforts.indexOf(value)), label: reasoningLabel(value, model), inherited: !override};
  }

  // Visual grades only. This does not add model capabilities or change wire values.
  function reasoningVisual(value) {
    const grades = {
      minimal: ["subtle", 4, .12, 14, 4, .1, 0],
      low: ["subtle", 10, .22, 12, 8, .18, 0],
      medium: ["steady", 16, .38, 7, 22, .28, 2],
      high: ["strong", 24, .56, 4.2, 42, .42, 4],
      xhigh: ["intense", 32, .72, 2.5, 68, .56, 7],
      max: ["maximum", 42, .88, 1, 96, .7, 10],
      ultra: ["ultra", 52, 1, .7, 128, .84, 14],
      enabled: ["steady", 16, .38, 7, 22, .28, 2],
    };
    const [tier, count, power, duration, drift, glow, trail] = Object.prototype.hasOwnProperty.call(grades, value)
      ? grades[value] : ["off", 0, 0, 14, 0, 0, 0];
    return {tier, count, power, duration, drift, glow, trail};
  }

  function normalizeModelCatalog(result) {
    const current = result?.default;
    if (!current || typeof current !== "object"
        || (current.provider_id !== null && (!Number.isInteger(current.provider_id) || current.provider_id < 1))
        || !Array.isArray(result.items)) {
      throw new Error("模型目录不可用，请重新加载");
    }
    // Registered accounts inherit Agent routing. Visitors receive a concrete safe
    // default; retain its ID so the displayed model is frozen into the new turn.
    return [{...current, provider_id: null, automatic_provider_id: current.provider_id},
      ...result.items.filter(item => item && Number.isInteger(item.provider_id) && item.provider_id > 0)];
  }

  function reconcileComposerSelection(selection, models) {
    const next = {...newComposerSelection(), ...selection};
    let reason = "";
    if (!selectedComposerModel(models, next)) {
      next.provider_id = null;
      next.reasoning_effort = "";
      reason = "所选模型已不可用，已切回智能体自动选择，请确认后重新发送";
    }
    const model = selectedComposerModel(models, next);
    if (next.reasoning_effort && (!model?.reasoning_supported
        || !Array.isArray(model.reasoning_efforts)
        || !model.reasoning_efforts.includes(next.reasoning_effort))) {
      next.reasoning_effort = "";
      reason = "所选推理强度已不受支持，已恢复默认，请确认后重新发送";
    }
    return {selection: next, reason};
  }

  function sameTurnOptions(active, options) {
    const fields = ["agent_id", "provider_id", "reasoning_effort", "approval_policy"];
    return Boolean(active && options && fields.every(key =>
      Object.prototype.hasOwnProperty.call(active, key)
      && Object.prototype.hasOwnProperty.call(options, key)
      && active[key] !== undefined && active[key] === options[key]));
  }

  const workspace = Object.freeze({normalizePreferences, modelRoutingPresentation,
    newComposerSelection, effectiveApproval, selectedComposerModel, modelAvailable, reasoningLabel, reasoningSlider, reasoningVisual,
    normalizeModelCatalog, reconcileComposerSelection, sameTurnOptions});
  root.ChatWorkspace = workspace;
  if (typeof module !== "undefined" && module.exports) module.exports = workspace;
})(globalThis);
