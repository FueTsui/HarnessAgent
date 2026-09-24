/* Versioned model-role configuration, shared by the editor and contract checks. */
(function (root) {
  "use strict";
  const roleNames = ["router", "planner", "critic"];
  const roles = {
    router: ["路由", "从已授权的工具中判断任务意图"],
    planner: ["规划", "拆分任务、安排执行步骤"],
    critic: ["验证", "答复验收失败时提出修订方案"],
  };
  const efforts = ["", "none", "minimal", "low", "medium", "high", "xhigh", "max", "disabled", "enabled"];
  const clone = value => JSON.parse(JSON.stringify(value || {}));
  function executionProviderId(agent) {
    const routing = agent?.routing || {};
    return ["rules", "policy"].includes(routing.mode)
      ? routing.default_provider_id || agent?.provider_id || null
      : agent?.provider_id || null;
  }
  function roleCapabilities(catalog, selectedId, execution = {}) {
    let ids = selectedId ? [Number(selectedId)] : [execution.providerId, ...(execution.fallbackIds || [])];
    if (!selectedId && ["rules", "policy"].includes(execution.mode)) {
      for (const rule of execution.rules || []) ids.push(rule.provider_id, ...(rule.fallback_provider_ids || []));
    }
    ids = [...new Set(ids.filter(Boolean).map(Number))];
    const connections = ids.map(id => catalog.find(item => item.id === id && item.enabled !== false));
    const first = connections[0];
    const supported = (selectedId || execution.providerId) && connections.length && connections.every(item => item?.reasoning_supported && Array.isArray(item.reasoning_efforts))
      ? first.reasoning_efforts.filter(effort => connections.every(item => item.reasoning_efforts.includes(effort))) : [];
    return {reasoning_efforts: supported, reasoning_effort_labels: first?.reasoning_effort_labels || {},
      reasoning_unavailable_reason: first?.reasoning_unavailable_reason || "当前执行模型及备用连接没有共同可用档位，请继承连接设置。"};
  }
  const fail = (message, field) => { const error = new Error(message); error.field = field; throw error; };
  const number = (value, min, max, label, field, integer = false) => {
    if (value === "" || value == null) fail(`${label}不能为空`, field);
    const result = Number(value);
    if (!Number.isFinite(result) || result < min || result > max || (integer && !Number.isInteger(result))) fail(`${label}需为 ${min}–${max} 之间的${integer ? "整数" : "数值"}`, field);
    return result;
  };
  function buildRouting(original, form) {
    const routing = clone(original);
    routing.version = 2;
    if (!["fixed", "rules", "policy"].includes(form.mode)) fail("请选择有效的执行模型路由模式", "agent-route-mode");
    routing.mode = form.mode;
    // A fixed execution model does not activate an older policy default. Keep
    // that inactive setting so merely editing the name cannot change routing.
    routing.default_provider_id = form.mode === "fixed" && Object.hasOwn(routing, "default_provider_id")
      ? routing.default_provider_id : form.providerId || null;
    routing.strategy = form.strategy === "lowest_cost" ? "lowest_cost" : "ordered";
    routing.fallback_provider_ids = [...new Set(form.fallbackIds || [])].filter(id => id !== routing.default_provider_id);
    let rules;
    try { rules = JSON.parse(form.rules || "[]"); } catch (_) { fail("高级匹配规则必须是有效 JSON", "agent-route-rules"); }
    if (!Array.isArray(rules) || rules.some(rule => !rule || typeof rule !== "object" || Array.isArray(rule))) fail("高级匹配规则必须是对象数组", "agent-route-rules");
    routing.rules = rules;
    routing.health = {
      ...(routing.health || {}), enabled: !!form.health.enabled,
      lookback_minutes: number(form.health.lookback_minutes, 1, 10080, "统计窗口", "agent-health-lookback", true),
      min_samples: number(form.health.min_samples, 1, 1000, "最少样本", "agent-health-min-samples", true),
      max_error_rate: number(form.health.max_error_rate, 0.05, 1, "最大错误率", "agent-health-error-rate"),
      consecutive_failures: number(form.health.consecutive_failures, 1, 100, "连续失败阈值", "agent-health-consecutive", true),
    };
    routing.roles = {...(routing.roles || {})};
    for (const name of roleNames) {
      const input = form.roles[name];
      if (!efforts.includes(input.reasoning_effort || "")) fail(`${roles[name][0]}模型推理强度无效`, `agent-${name}-effort`);
      routing.roles[name] = {
        provider_id: input.provider_id === "" || input.provider_id == null ? null : number(input.provider_id, 1, Number.MAX_SAFE_INTEGER, `${roles[name][0]}模型`, `agent-${name}-provider`, true),
        reasoning_effort: input.reasoning_effort || "",
        max_tokens: input.max_tokens === "" || input.max_tokens == null ? null : number(input.max_tokens, 1, 1000000, `${roles[name][0]}输出上限`, `agent-${name}-tokens`, true),
      };
    }
    if (!["auto", "always", "off"].includes(form.planning)) fail("规划触发方式无效", "agent-planning-mode");
    if (!["deterministic", "model"].includes(form.toolRouting)) fail("工具决策方式无效", "agent-tool-routing");
    if (!["on_failure", "off"].includes(form.review)) fail("验证触发方式无效", "agent-review-mode");
    routing.planning = {...(routing.planning || {}), mode: form.planning};
    routing.tool_routing = {...(routing.tool_routing || {}), mode: form.toolRouting, confidence_threshold: number(form.confidence, 0, 1, "模型置信度门槛", "agent-router-confidence")};
    routing.review = {...(routing.review || {}), mode: form.review};
    return routing;
  }
  function moveFallback(ids, id, offset) {
    const result = [...ids];
    const index = result.indexOf(id), target = index + offset;
    if (index >= 0 && target >= 0 && target < result.length) [result[index], result[target]] = [result[target], result[index]];
    return result;
  }
  let providers = [], fallbackIds = [];
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
  function providerLabel(item) {
    const modelName = String(item.model_name || "").trim();
    const legacyName = String(item.name || "").trim();
    return modelName || (!legacyName.startsWith("__personal_model_") ? legacyName : "") || item.model_id || `模型 #${item.id}`;
  }
  function providerOptions(selected, label) {
    const available = providers.filter(item => item.enabled !== false || item.id === selected);
    let html = `<option value="">${label}</option>` + available.map(item => `<option value="${item.id}" ${item.id === selected ? "selected" : ""} ${item.enabled === false ? "disabled" : ""}>${esc(providerLabel(item))}${item.model_id && item.model_id !== providerLabel(item) ? ` · ${esc(item.model_id)}` : ""}${item.enabled === false ? "（已停用，请更换）" : ""}</option>`).join("");
    if (selected && !available.some(item => item.id === selected)) html += `<option value="${selected}" selected disabled>不可用模型 #${selected}（请选择其他模型）</option>`;
    return html;
  }
  function renderFallbacks() {
    const sorted = [...fallbackIds.map(id => providers.find(item => item.id === id) || {id, name: `不可用模型 #${id}`, enabled: false}), ...providers.filter(item => item.enabled !== false && !fallbackIds.includes(item.id))];
    $("agent-provider-fallbacks").innerHTML = sorted.map(item => {
      const index = fallbackIds.indexOf(item.id);
      const label = esc(providerLabel(item));
      return `<div class="fallback-item"><label><input type="checkbox" name="agent_provider_fallback" value="${item.id}" ${index >= 0 ? "checked" : ""} ${item.enabled === false && index < 0 ? "disabled" : ""} /><span>${label}${item.enabled === false ? "（不可用，请移除）" : ""}</span></label><span class="fallback-position">${index >= 0 ? `第 ${index + 1} 顺位` : "未选用"}</span><button type="button" class="btn ghost small" data-fallback-move="${item.id}" data-offset="-1" aria-label="上移 ${label}" ${index <= 0 ? "disabled" : ""}>上移</button><button type="button" class="btn ghost small" data-fallback-move="${item.id}" data-offset="1" aria-label="下移 ${label}" ${index < 0 || index === fallbackIds.length - 1 ? "disabled" : ""}>下移</button></div>`;
    }).join("") || '<p class="field-help">暂无可用模型连接，请先在“模型”页面添加。</p>';
    $("agent-provider-fallbacks").querySelectorAll("input").forEach(input => input.onchange = () => {
      const id = Number(input.value);
      fallbackIds = input.checked ? [...fallbackIds, id] : fallbackIds.filter(value => value !== id);
      renderFallbacks();
      renderRoleEfforts();
    });
    $("agent-provider-fallbacks").querySelectorAll("[data-fallback-move]").forEach(button => button.onclick = () => {
      const id = Number(button.dataset.fallbackMove), offset = Number(button.dataset.offset);
      fallbackIds = moveFallback(fallbackIds, id, offset);
      renderFallbacks();
      $("agent-provider-fallbacks").querySelector(`[data-fallback-move="${id}"][data-offset="${offset}"]`)?.focus();
    });
  }
  function selectTab(name, focus = false) {
    document.querySelectorAll("[data-agent-tab]").forEach(button => {
      const active = button.dataset.agentTab === name;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      $(`agent-section-${button.dataset.agentTab}`).hidden = !active;
      if (active && focus) button.focus();
    });
  }
  function syncMode() {
    $("agent-router-confidence").disabled = $("agent-tool-routing").value !== "model";
    $("agent-route-strategy").disabled = $("agent-route-mode").value !== "policy";
    renderRoleEfforts();
  }
  function currentRoleCapabilities(name) {
    let rules;
    try { rules = JSON.parse($("agent-route-rules").value || "[]"); } catch (_) { rules = []; }
    return roleCapabilities(providers, $(`agent-${name}-provider`).value, {
      providerId: Number($("agent-provider").value) || null, fallbackIds,
      mode: $("agent-route-mode").value, rules: Array.isArray(rules) ? rules.filter(rule => rule && typeof rule === "object") : [],
    });
  }
  function renderRoleEfforts(initial = null) {
    for (const name of roleNames) {
      const select = $(`agent-${name}-effort`);
      if (!select) continue;
      const value = initial ? initial[name]?.reasoning_effort || "" : select.value;
      const capability = currentRoleCapabilities(name), supported = capability.reasoning_efforts;
      const invalid = value && !supported.includes(value);
      select.innerHTML = '<option value="">继承模型连接设置</option>' + supported.map(effort =>
        `<option value="${esc(effort)}">${esc(root?.ProviderReasoningConfig?.label(effort, capability) || effort)} · ${esc(effort)}</option>`).join("")
        + (invalid ? `<option value="${esc(value)}">${esc(value)}（当前不可用，请重新选择）</option>` : "");
      select.value = value;
      $(`agent-${name}-effort-help`).textContent = invalid ? "已保留原设置；所选连接不支持此档位，请更改或继承。"
        : supported.length ? "仅显示所选连接支持的档位；继承执行模型时采用候选连接的共同档位。" : capability.reasoning_unavailable_reason;
    }
  }
  function load(agent, catalog) {
    providers = catalog;
    const routing = agent?.routing || {};
    fallbackIds = [...new Set(routing.fallback_provider_ids || [])];
    $("agent-provider").innerHTML = providerOptions(executionProviderId(agent), "默认执行模型");
    $("agent-model-roles").innerHTML = roleNames.map(name => `<div class="model-role-row" role="row"><label for="agent-${name}-provider" role="cell">${roles[name][0]}</label><div role="cell"><select id="agent-${name}-provider">${providerOptions(routing.roles?.[name]?.provider_id, "继承执行模型")}</select></div><p role="cell">${roles[name][1]}</p></div>`).join("");
    $("agent-role-parameters").innerHTML = roleNames.map(name => `<fieldset><legend>${roles[name][0]}模型参数</legend><label for="agent-${name}-effort">推理档位</label><select id="agent-${name}-effort" aria-describedby="agent-${name}-effort-help"></select><small class="field-help" id="agent-${name}-effort-help"></small><label for="agent-${name}-tokens">最大输出 Token</label><input id="agent-${name}-tokens" type="number" min="1" max="1000000" value="${esc(routing.roles?.[name]?.max_tokens ?? "")}" placeholder="留空继承模型连接设置" /></fieldset>`).join("");
    $("agent-route-mode").value = routing.mode || "fixed";
    $("agent-route-strategy").value = routing.strategy || "ordered";
    $("agent-route-rules").value = JSON.stringify(routing.rules || [], null, 2);
    $("agent-health-enabled").checked = routing.health?.enabled ?? true;
    $("agent-health-lookback").value = routing.health?.lookback_minutes ?? 60;
    $("agent-health-min-samples").value = routing.health?.min_samples ?? 3;
    $("agent-health-error-rate").value = routing.health?.max_error_rate ?? 0.6;
    $("agent-health-consecutive").value = routing.health?.consecutive_failures ?? 3;
    $("agent-tool-routing").value = routing.tool_routing?.mode || "deterministic";
    $("agent-router-confidence").value = routing.tool_routing?.confidence_threshold ?? 0.7;
    $("agent-planning-mode").value = routing.planning?.mode || "auto";
    $("agent-review-mode").value = routing.review?.mode || "on_failure";
    $("agent-tool-routing").onchange = syncMode;
    $("agent-route-mode").onchange = syncMode;
    $("agent-provider").onchange = () => renderRoleEfforts();
    $("agent-route-rules").oninput = () => renderRoleEfforts();
    for (const name of roleNames) $(`agent-${name}-provider`).onchange = () => renderRoleEfforts();
    $("agent-dialog").querySelectorAll("details").forEach(element => element.open = false);
    renderFallbacks();
    renderRoleEfforts(routing.roles || {});
    syncMode();
    document.querySelectorAll("[data-agent-tab]").forEach((button, index, tabs) => {
      button.onclick = () => selectTab(button.dataset.agentTab);
      button.onkeydown = event => {
        let next;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        if (next == null) return;
        event.preventDefault(); selectTab(tabs[next].dataset.agentTab, true);
      };
    });
    selectTab(agent ? "models" : "overview");
  }
  function read(original, providerId) {
    const roleValues = Object.fromEntries(roleNames.map(name => [name, {provider_id: $(`agent-${name}-provider`).value, reasoning_effort: $(`agent-${name}-effort`).value, max_tokens: $(`agent-${name}-tokens`).value}]));
    for (const [name, value] of Object.entries(roleValues)) {
      if (value.provider_id && !providers.some(item => item.id === Number(value.provider_id) && item.enabled !== false)) fail(`${roles[name][0]}模型不可用，请更换或选择继承执行模型`, `agent-${name}-provider`);
      if (value.reasoning_effort && !currentRoleCapabilities(name).reasoning_efforts.includes(value.reasoning_effort)) fail(`${roles[name][0]}模型不支持所选推理档位，请更改或继承连接设置`, `agent-${name}-effort`);
    }
    if (providerId && !providers.some(item => item.id === providerId && item.enabled !== false)) fail("执行模型不可用，请更换模型", "agent-provider");
    if (fallbackIds.some(id => !providers.some(item => item.id === id && item.enabled !== false))) fail("备用列表中有不可用模型，请移除后保存", "agent-provider-fallbacks");
    return buildRouting(original, {
      providerId, roles: roleValues, fallbackIds, mode: $("agent-route-mode").value, strategy: $("agent-route-strategy").value,
      rules: $("agent-route-rules").value, planning: $("agent-planning-mode").value, toolRouting: $("agent-tool-routing").value,
      confidence: $("agent-router-confidence").value, review: $("agent-review-mode").value,
      health: {enabled: $("agent-health-enabled").checked, lookback_minutes: $("agent-health-lookback").value, min_samples: $("agent-health-min-samples").value, max_error_rate: $("agent-health-error-rate").value, consecutive_failures: $("agent-health-consecutive").value},
    });
  }
  function focusError(error) {
    const target = error.field && $(error.field);
    if (!target) return;
    const panel = target.closest('[role="tabpanel"]');
    if (panel) selectTab(panel.id.replace("agent-section-", ""));
    const details = target.closest("details");
    if (details) details.open = true;
    target.focus();
  }
  const api = {buildRouting, executionProviderId, roleCapabilities, moveFallback, load, read, selectTab, focusError};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.AgentModelConfig = api;
})(typeof window !== "undefined" ? window : null);
