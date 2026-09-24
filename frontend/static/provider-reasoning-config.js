/* Shared configuration and capability preview for managed and personal models. */
(function (root) {
  "use strict";
  const effortValues = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
  const toggleValues = ["disabled", "enabled"];
  const labels = {none: "关闭", minimal: "极轻", low: "轻度", medium: "适中", high: "深度", xhigh: "极深", max: "最高", disabled: "关闭", enabled: "开启"};
  const params = ["auto", "reasoning_effort", "reasoning.effort", "output_config.effort"];
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
  const label = (value, capability) => capability?.reasoning_effort_labels?.[value] || labels[value] || value || "接口默认";

  function normalizeConfig(value = {}, context = {}) {
    const mode = value.mode || "auto";
    if (!["auto", "custom", "off"].includes(mode)) throw new Error("请选择有效的推理配置方式");
    if (mode === "auto" || (mode === "off" && !value.control)) return {mode};
    const control = value.control || "effort";
    if (!["effort", "thinking_toggle"].includes(control)) throw new Error("请选择有效的推理控制方式");
    const allowed = control === "thinking_toggle" ? toggleValues : effortValues;
    const selected = value.supported_efforts || [];
    if (!Array.isArray(selected) || (mode === "custom" && !selected.length) || selected.some(item => !allowed.includes(item))) throw new Error("请至少选择一个有效档位");
    const effort_param = control === "thinking_toggle" ? "auto" : value.effort_param || "auto";
    if (!params.includes(effort_param)) throw new Error("请选择有效的推理参数字段");
    const result = {mode, control, effort_param, supported_efforts: allowed.filter(item => selected.includes(item))};
    if (control === "thinking_toggle" && context.wire_api === "messages") {
      const budget = value.budget_tokens === "" || value.budget_tokens == null ? 2048 : Number(value.budget_tokens);
      if (!Number.isInteger(budget) || budget < 1024 || (mode !== "off" && context.max_tokens && budget >= Number(context.max_tokens))) throw new Error("思考预算需为至少 1024 且小于最大输出 Tokens 的整数");
      result.budget_tokens = budget;
    }
    return result;
  }

  function buildPayload(config, effort, context = {}, capability = null) {
    const reasoning_config = normalizeConfig(config, context);
    const reasoning_effort = !context.model_reasoning || reasoning_config.mode === "off" ? "" : effort || "";
    const supported = reasoning_config.mode === "custom" ? reasoning_config.supported_efforts : capability?.reasoning_efforts;
    if (reasoning_effort && (!Array.isArray(supported) || !supported.includes(reasoning_effort))) throw new Error("当前默认档位不受支持，请重新选择默认档位或使用接口默认");
    return {reasoning_config, reasoning_effort};
  }

  function mount(container, {getContext, initial = {}}) {
    let config = {...(initial.reasoning_config || {})};
    let currentDefault = initial.reasoning_effort || "";
    let capability = Array.isArray(initial.reasoning_efforts) ? initial : null;
    let request = 0, timer = null, error = "", notice = "", disabledNotice = false;
    let wasEnabled = Boolean(getContext().model_reasoning);
    const prefix = container.id;
    container.classList.add("reasoning-config");
    container.innerHTML = `<div class="reasoning-config-grid">
      <label>推理配置<select data-reasoning="mode"><option value="auto">自动识别</option><option value="custom">自定义</option><option value="off">接口默认</option></select></label>
      <label>默认档位<select data-reasoning="default"></select></label>
      <div class="reasoning-config-custom reasoning-config-wide" data-reasoning="custom" hidden>
        <label>推理控制方式<select data-reasoning="control"><option value="effort">推理强度</option><option value="thinking_toggle">思考开关</option></select></label>
        <label data-reasoning="param-wrap">参数字段<select data-reasoning="param"><option value="auto">按接口协议自动</option><option value="reasoning_effort">reasoning_effort</option><option value="reasoning.effort">reasoning.effort</option><option value="output_config.effort">output_config.effort</option></select></label>
        <fieldset class="reasoning-config-wide"><legend>可用档位</legend><div class="reasoning-config-choices" data-reasoning="efforts"></div></fieldset>
        <label data-reasoning="budget-wrap" hidden>思考预算 Tokens<input data-reasoning="budget" type="number" min="1024" step="1" value="2048" /></label>
      </div>
    </div><p class="reasoning-config-help" id="${prefix}-help">自动识别会显示服务端确认的能力；自定义请按服务商文档填写。接口默认不发送推理覆盖参数。</p>
    <p class="reasoning-config-status" data-reasoning="status" role="status" aria-live="polite"></p>`;
    const find = name => container.querySelector(`[data-reasoning="${name}"]`);
    for (const name of ["mode", "default", "control", "param", "budget"]) find(name).setAttribute("aria-describedby", `${prefix}-help`);
    find("mode").value = config.mode || "auto";
    find("control").value = config.control || "effort";
    find("param").value = config.effort_param || "auto";
    find("budget").value = config.budget_tokens ?? 2048;

    function rawConfig() {
      return {mode: find("mode").value, control: find("control").value, effort_param: find("param").value,
        supported_efforts: [...container.querySelectorAll('[data-reasoning-effort]:checked')].map(input => input.value), budget_tokens: find("budget").value};
    }
    function renderChoices(selected = []) {
      const values = find("control").value === "thinking_toggle" ? toggleValues : effortValues;
      find("efforts").innerHTML = values.map(value => `<label><input type="checkbox" data-reasoning-effort value="${value}" ${selected.includes(value) ? "checked" : ""} /><span>${label(value)} <small>${value}</small></span></label>`).join("");
    }
    function renderState() {
      const context = getContext(), enabled = Boolean(context.model_reasoning), mode = find("mode").value;
      const custom = mode === "custom", toggle = find("control").value === "thinking_toggle";
      find("mode").disabled = !enabled;
      find("custom").hidden = !custom;
      find("param-wrap").hidden = toggle;
      find("budget-wrap").hidden = !toggle || context.wire_api !== "messages";
      find("custom").querySelectorAll("input, select").forEach(input => { input.disabled = !enabled; });
      const supported = !enabled || mode === "off" ? [] : custom ? rawConfig().supported_efforts : capability?.reasoning_efforts || [];
      if (!enabled || mode === "off") currentDefault = "";
      const invalid = currentDefault && !supported.includes(currentDefault);
      find("default").innerHTML = '<option value="">接口默认</option>' + supported.map(value => `<option value="${esc(value)}">${esc(label(value, capability))} · ${esc(value)}</option>`).join("")
        + (invalid ? `<option value="${esc(currentDefault)}">${esc(currentDefault)}（当前不可用，请重新选择）</option>` : "");
      find("default").value = currentDefault;
      find("default").disabled = !enabled || mode === "off" || (!supported.length && !invalid);
      const status = find("status");
      status.classList.toggle("is-error", Boolean(error || invalid));
      status.textContent = error || (!enabled ? disabledNotice ? "已关闭推理控制；原自定义档位已保留，可重新启用后切回自定义。" : "未启用推理能力，将使用接口默认。" : mode === "off" ? "沿用接口默认，不提供对话中的推理覆盖。"
        : invalid ? "已保留原默认值；当前能力不支持它，请确认后更改。"
        : notice || (capability ? capability.reasoning_supported && capability.reasoning_efforts?.length
          ? `实际可用：${capability.reasoning_efforts.map(value => label(value, capability)).join("、")}`
          : capability.reasoning_unavailable_reason || "当前接口未提供可调推理档位。" : "填写模型 ID 后确认可用档位。"));
    }
    async function refresh() {
      clearTimeout(timer);
      const serial = ++request, context = getContext();
      error = "";
      let reasoning_config;
      try { reasoning_config = normalizeConfig(rawConfig(), context); }
      catch (issue) { error = issue.message; capability = null; renderState(); return; }
      if (!context.model_reasoning || reasoning_config.mode === "off" || !String(context.model_id || "").trim()) {
        capability = null; notice = ""; renderState(); return;
      }
      notice = "正在确认可用档位…"; renderState();
      try {
        const response = await root.api("/api/v1/reasoning-capabilities", {method: "POST", json: {
          model_id: context.model_id, provider_type: context.provider_type, wire_api: context.wire_api,
          model_reasoning: Boolean(context.model_reasoning), max_tokens: context.max_tokens,
          reasoning_config, reasoning_effort: "",
        }});
        if (serial !== request) return;
        if (!response || !Array.isArray(response.reasoning_efforts)) throw new Error("推理能力返回异常，请重试");
        capability = response; notice = "";
      } catch (issue) {
        if (serial !== request) return;
        capability = null; error = `无法确认推理能力：${issue.message}`; notice = "";
      }
      renderState();
    }
    function update() {
      request++;
      const enabled = Boolean(getContext().model_reasoning);
      if (wasEnabled && !enabled) {
        find("mode").value = "off";
        currentDefault = "";
        disabledNotice = true;
      }
      wasEnabled = enabled;
      capability = null; error = ""; notice = "正在确认可用档位…";
      clearTimeout(timer); renderState();
      timer = setTimeout(refresh, 180);
    }
    find("mode").onchange = () => {
      if (find("mode").value === "custom" && capability?.reasoning_efforts?.length) {
        find("control").value = capability.reasoning_control === "thinking_toggle" ? "thinking_toggle" : "effort";
        renderChoices(capability.reasoning_efforts);
      }
      update();
    };
    find("control").onchange = () => { renderChoices([]); currentDefault = ""; update(); };
    find("efforts").onchange = () => {
      if (currentDefault && !rawConfig().supported_efforts.includes(currentDefault)) {
        currentDefault = ""; root.showToast?.("默认档位已恢复为接口默认");
      }
      update();
    };
    find("param").onchange = update;
    find("budget").oninput = update;
    find("default").onchange = () => { currentDefault = find("default").value; renderState(); };
    renderChoices(config.supported_efforts || []);
    renderState();
    void refresh();
    return {container, update, refresh,
      read() {
        if (error) throw new Error(error);
        return buildPayload(rawConfig(), currentDefault, getContext(), capability);
      },
      destroy() { request++; clearTimeout(timer); },
    };
  }

  const exported = Object.freeze({effortValues, toggleValues, label, normalizeConfig, buildPayload, mount});
  root.ProviderReasoningConfig = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(globalThis);
