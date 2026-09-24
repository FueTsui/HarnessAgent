/* Root-only guardrail administration. The server remains the policy authority. */
(function () {
  "use strict";

  const endpoint = "/api/v1/guardrails";
  const workspace = {
    active: false, generation: 0, snapshot: null, draft: null,
    readController: null, previewController: null, previewVersion: 0,
    previewPending: false, pendingSave: null, notice: null,
  };
  const node = id => document.getElementById(id);
  const root = () => node("gr-global-content");
  const safe = value => escapeHtml(value);
  const copy = value => JSON.parse(JSON.stringify(value));
  const isCurrent = generation => workspace.active && workspace.generation === generation && root();
  const booleanFields = ["enabled", "block_mutating_tools", "require_high_risk_approval", "require_external_approval"];

  function timestamp(value) {
    if (!value) return "使用默认策略";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "服务端已保存" : `更新于 ${date.toLocaleString("zh-CN", { hour12: false })}`;
  }

  function message(error) {
    if (error?.status === 403) return "当前账户没有护栏管理权限，请使用 root 账户。";
    if (error?.status === 409) return "策略已被其他管理员更新。请重新读取最新策略后再修改。";
    if (error?.status === 422) return "配置校验未通过，请检查工具名称、参数上限和测试参数。";
    return String(error?.message || "请求失败，请检查连接后重试。");
  }

  function noticeMarkup(kind, text) {
    return `<div class="gr-notice is-${safe(kind)}">${icon(kind === "error" ? "close" : "check", 16)}<span>${safe(text)}</span></div>`;
  }

  function setNotice(kind, text) {
    workspace.notice = text ? {kind, text} : null;
    const target = node("gr-feedback");
    if (target) target.innerHTML = text ? noticeMarkup(kind, text) : "";
  }

  function switchMarkup(name, label, checked) {
    return `<label class="gr-switch"><input type="checkbox" id="gr-${safe(name)}" name="${safe(name)}" data-gr-config aria-label="${safe(label)}" ${checked ? "checked" : ""}><span class="gr-switch-track" aria-hidden="true"></span></label>`;
  }

  function ruleMarkup(name, title, description) {
    return `<div class="gr-rule"><div class="gr-rule-copy"><h4>${safe(title)}</h4><p>${safe(description)}</p></div>${switchMarkup(name, title, workspace.draft[name])}</div>`;
  }

  function savedStatus() {
    const snapshot = workspace.snapshot;
    if (!snapshot) return "尚未读取";
    return snapshot.config.enabled ? "护栏已启用" : "附加护栏已停用";
  }

  function isDirty() {
    if (!workspace.snapshot || !workspace.draft) return false;
    return JSON.stringify(workspace.snapshot.config) !== JSON.stringify(workspace.draft);
  }

  function syncControls() {
    if (!workspace.active || !workspace.snapshot) return;
    const dirty = isDirty();
    const saving = !!workspace.pendingSave;
    root()?.querySelectorAll("[data-gr-config]").forEach(input => { input.disabled = saving; });
    const saveButton = node("gr-save");
    if (saveButton) {
      saveButton.disabled = saving || !dirty;
      saveButton.innerHTML = icon(saving ? "history" : "check", 15) + (saving ? "保存中…" : "保存策略");
    }
    const discardButton = node("gr-discard");
    if (discardButton) discardButton.disabled = saving || !dirty;
    const refreshButton = node("gr-refresh");
    if (refreshButton) refreshButton.disabled = saving;
    const title = node("gr-save-state");
    if (title) title.textContent = saving ? "正在提交策略" : dirty ? "有未保存的更改" : "当前显示服务端策略";
    const caption = node("gr-save-caption");
    if (caption) caption.textContent = saving ? "保存完成后，后续工具调用使用新策略。" : dirty ? "保存后生效；离开模块再返回会重新读取服务端策略。" : `版本 ${workspace.snapshot.revision} · 保存后对后续工具调用生效`;
    const draftLabel = node("gr-draft-label");
    if (draftLabel) {
      draftLabel.textContent = dirty ? "未保存" : "已同步";
      draftLabel.className = `gr-badge ${dirty ? "is-draft" : "is-purple"}`;
    }
    const hint = node("gr-preview-policy");
    if (hint) hint.textContent = dirty ? "测试服务端已保存策略，不包含当前更改" : "使用服务端当前已保存策略";
    const testButton = node("gr-preview-submit");
    if (testButton) {
      const builtinEmpty = node("gr-preview-kind")?.value === "builtin" && !(workspace.snapshot.builtin_tools || []).length;
      testButton.disabled = saving || workspace.previewPending || builtinEmpty;
      testButton.innerHTML = icon(workspace.previewPending ? "history" : "flask", 15) + (workspace.previewPending ? "正在测试…" : "测试策略");
    }
    const enabledHint = node("gr-disabled-hint");
    if (enabledHint) enabledHint.hidden = workspace.draft.enabled;
  }

  function render() {
    const container = root();
    if (!container || !workspace.snapshot) return;
    const snapshot = workspace.snapshot;
    const stats = snapshot.stats || {};
    const builtinTools = snapshot.builtin_tools || [];
    const baseline = snapshot.baseline || [];
    container.innerHTML = `
      <div class="gr-hero">
        <div class="gr-hero-intro"><span class="gr-hero-icon">${icon("shield", 26)}</span><div><h3>为每次工具调用设定清晰边界</h3><p>全局策略统一作用于内置工具与 MCP，保存后在后续调用前检查。</p></div></div>
        <div class="gr-hero-meta"><span class="gr-badge ${snapshot.config.enabled ? "is-active" : ""}">${savedStatus()}</span><span class="gr-timestamp">${safe(timestamp(snapshot.updated_at))}</span></div>
      </div>
      <div class="gr-layout">
        <div class="gr-primary">
          <form id="gr-config-form" novalidate>
            <section class="gr-card" aria-labelledby="gr-policy-title">
              <div class="gr-card-heading"><div><h3 id="gr-policy-title">${icon("shield", 18)}执行策略</h3><p>${safe(stats.active_rules ?? 0)} 项附加规则已生效 · ${safe(stats.blocked_tools ?? 0)} 个工具在阻止名单中</p></div><span class="gr-badge is-purple" id="gr-draft-label">已同步</span></div>
              <div class="gr-card-body">
                ${ruleMarkup("enabled", "启用全局护栏", "统一检查工具调用；停用后仍保留平台原有权限与审批。")}
                <div id="gr-disabled-hint" class="gr-notice is-warning" ${workspace.draft.enabled ? "hidden" : ""}>${icon("shield", 16)}<span>保存此状态后，下方附加规则不再执行。平台原有安全边界继续生效。</span></div>
                ${ruleMarkup("block_mutating_tools", "阻止写入与变更", "拦截被识别为修改数据、发送内容或改变外部状态的工具。")}
                ${ruleMarkup("require_high_risk_approval", "高风险操作需要审批", "对被识别为高风险的操作增加人工审批要求。")}
                ${ruleMarkup("require_external_approval", "MCP 写操作需要审批", "外部写操作增加单次审批，包括任务选择完全访问策略时。")}
                <hr class="gr-divider">
                <div class="gr-policy-fields">
                  <div class="gr-field"><label for="gr-blocked_tools">阻止指定工具</label><textarea id="gr-blocked_tools" name="blocked_tools" data-gr-config rows="3" placeholder="每行一个工具名称，例如 shell" spellcheck="false" aria-describedby="gr-blocked-help">${safe(workspace.draft.blocked_tools.join("\n"))}</textarea><small id="gr-blocked-help">精确匹配工具名称，每行一个，最多 100 个。指定 MCP 连接可填写 mcp:服务ID:工具名。</small></div>
                  <div class="gr-field"><label for="gr-max_argument_chars">工具参数长度上限</label><input type="number" id="gr-max_argument_chars" name="max_argument_chars" data-gr-config min="0" max="1000000" step="1" value="${safe(workspace.draft.max_argument_chars)}" aria-describedby="gr-argument-help"><small id="gr-argument-help">按序列化后的参数字符数检查。0 表示不额外限制，最大 1,000,000。</small></div>
                </div>
              </div>
            </section>
          </form>
          <div class="gr-feedback" id="gr-feedback" role="status" aria-live="polite">${workspace.notice ? noticeMarkup(workspace.notice.kind, workspace.notice.text) : ""}</div>
          <div class="gr-savebar"><div class="gr-save-copy"><strong id="gr-save-state">当前显示服务端策略</strong><small id="gr-save-caption">保存后对后续工具调用生效</small></div><div class="gr-save-actions"><button type="button" class="gr-btn" id="gr-discard" disabled>撤销更改</button><button type="submit" form="gr-config-form" class="gr-btn gr-btn-primary" id="gr-save" disabled>${icon("check", 15)}保存策略</button></div></div>
        </div>
        <aside class="gr-aside" aria-label="策略测试与安全边界">
          <section class="gr-card" aria-labelledby="gr-test-title">
            <div class="gr-card-heading"><div><h3 id="gr-test-title">${icon("flask", 18)}策略测试</h3><p>模拟一次调用，检查已保存策略的判定。</p></div><span class="gr-badge">预览</span></div>
            <div class="gr-card-body">
              <form id="gr-preview-form" novalidate>
                <div class="gr-field-grid"><div class="gr-field"><label for="gr-preview-kind">工具来源</label><select id="gr-preview-kind"><option value="builtin">内置工具</option><option value="mcp">MCP 工具</option></select></div><div class="gr-field"><label for="gr-preview-approval">会话审批模式</label><select id="gr-preview-approval"><option value="ask">请求批准</option><option value="auto">帮我批准</option><option value="full_access">完全访问 · root</option></select></div></div>
                <div class="gr-field" id="gr-builtin-field"><label for="gr-preview-builtin">内置工具</label><select id="gr-preview-builtin">${builtinTools.length ? builtinTools.map(tool => `<option value="${safe(tool.name)}">${safe(tool.name)}${tool.description ? ` · ${safe(String(tool.description).slice(0, 100))}` : ""}</option>`).join("") : '<option value="">暂无可用内置工具</option>'}</select><small>工具风险使用服务端目录中的定义。</small></div>
                <div id="gr-mcp-fields" hidden>
                  <div class="gr-field"><label for="gr-preview-mcp">MCP 工具名称</label><input id="gr-preview-mcp" placeholder="例如 search_documents" maxlength="256" spellcheck="false"></div>
                  <div class="gr-field"><label for="gr-preview-server">连接 ID（可选）</label><input id="gr-preview-server" type="number" min="1" step="1" placeholder="用于匹配指定连接的阻止规则"></div>
                  <div class="gr-rule"><div class="gr-rule-copy"><h4>模拟写入操作</h4><p>指定此测试调用的风险。</p></div>${switchMarkup("preview-mutating", "模拟写入操作", false).replace(" data-gr-config", "")}</div>
                  <div class="gr-rule"><div class="gr-rule-copy"><h4>模拟高风险操作</h4><p>测试额外审批是否触发。</p></div>${switchMarkup("preview-destructive", "模拟高风险操作", false).replace(" data-gr-config", "")}</div>
                </div>
                <div class="gr-field"><label for="gr-preview-arguments">调用参数 · JSON 对象</label><textarea id="gr-preview-arguments" rows="3" spellcheck="false" placeholder='{"query": "示例内容"}'>{}</textarea></div>
                <div class="gr-test-caption"><span id="gr-preview-policy">使用已保存策略</span></div>
                <button type="submit" class="gr-btn gr-btn-primary gr-btn-wide" id="gr-preview-submit">${icon("flask", 15)}测试策略</button>
              </form>
              <div id="gr-preview-result" role="status" aria-live="polite"><div class="gr-test-empty">${icon("shield", 15)}<span>预览不会执行工具。实际调用仍需通过角色、资源绑定和平台其他检查。</span></div></div>
            </div>
          </section>
          <section class="gr-card" aria-labelledby="gr-boundary-title">
            <div class="gr-card-heading"><div><h3 id="gr-boundary-title">${icon("key", 18)}平台既有安全边界</h3><p>由系统执行，不受护栏总开关影响。</p></div></div>
            <div class="gr-card-body"><ul class="gr-boundary-list">${baseline.length ? baseline.map(item => `<li>${icon(item.status === "enforced" ? "check" : "shield", 15)}<div><strong>${safe(item.name)}</strong><p>${safe(item.description)}</p></div></li>`).join("") : '<li><div><p>服务端暂未返回安全边界说明。</p></div></li>'}</ul><hr class="gr-divider"><div class="gr-test-caption"><span>策略由 root 管理 · 版本 ${safe(snapshot.revision)}</span><button type="button" id="gr-refresh" class="gr-btn gr-btn-link">${icon("restore", 13)}重新读取</button></div></div>
          </section>
        </aside>
      </div>`;
    bindEvents();
    syncControls();
  }

  function readDraft() {
    const form = node("gr-config-form");
    if (!form) return;
    for (const name of booleanFields) workspace.draft[name] = form.elements[name].checked;
    workspace.draft.blocked_tools = [...new Set(form.elements.blocked_tools.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean))];
    const rawMaximum = form.elements.max_argument_chars.value.trim();
    workspace.draft.max_argument_chars = rawMaximum === "" ? null : Number(rawMaximum);
  }

  function validateConfig() {
    readDraft();
    const config = workspace.draft;
    const toolsInput = node("gr-blocked_tools");
    const limitInput = node("gr-max_argument_chars");
    toolsInput.removeAttribute("aria-invalid");
    limitInput.removeAttribute("aria-invalid");
    if (config.blocked_tools.length > 100 || config.blocked_tools.some(name => name.length > 256 || /\s/.test(name))) {
      toolsInput.setAttribute("aria-invalid", "true");
      toolsInput.focus();
      throw new Error("阻止名单最多 100 个工具，每个名称最多 256 字符且不能含空白。请每行填写一个名称。");
    }
    if (!Number.isInteger(config.max_argument_chars) || config.max_argument_chars < 0 || config.max_argument_chars > 1000000) {
      limitInput.setAttribute("aria-invalid", "true");
      limitInput.focus();
      throw new Error("参数长度上限必须是 0 到 1,000,000 之间的整数。");
    }
    return copy(config);
  }

  function cancelPreview(clearResult) {
    workspace.previewVersion += 1;
    workspace.previewController?.abort();
    workspace.previewController = null;
    workspace.previewPending = false;
    if (clearResult && node("gr-preview-result")) node("gr-preview-result").innerHTML = `<div class="gr-test-empty">${icon("flask", 15)}<span>调整参数后，点击“测试策略”获取新的判定。</span></div>`;
    syncControls();
  }

  function bindEvents() {
    node("gr-config-form").addEventListener("submit", event => { event.preventDefault(); save(); });
    node("gr-config-form").addEventListener("input", () => { readDraft(); setNotice(null, ""); syncControls(); });
    node("gr-config-form").addEventListener("change", () => { readDraft(); setNotice(null, ""); syncControls(); });
    node("gr-discard").addEventListener("click", () => {
      if (workspace.pendingSave) return;
      cancelPreview(false);
      workspace.draft = copy(workspace.snapshot.config);
      workspace.notice = {kind: "success", text: "已撤销本页更改，恢复到当前读取的服务端策略。"};
      render();
    });
    node("gr-refresh").addEventListener("click", () => load());
    node("gr-preview-form").addEventListener("submit", event => { event.preventDefault(); preview(); });
    const changePreview = event => {
      if (event.target.id === "gr-preview-destructive" && event.target.checked) node("gr-preview-mutating").checked = true;
      if (event.target.id === "gr-preview-mutating" && !event.target.checked) node("gr-preview-destructive").checked = false;
      cancelPreview(true);
    };
    node("gr-preview-form").addEventListener("input", changePreview);
    node("gr-preview-form").addEventListener("change", changePreview);
    node("gr-preview-kind").addEventListener("change", event => {
      const mcp = event.target.value === "mcp";
      node("gr-builtin-field").hidden = mcp;
      node("gr-mcp-fields").hidden = !mcp;
      syncControls();
    });
  }

  async function save() {
    if (!workspace.active || workspace.pendingSave || !isDirty()) return;
    let config;
    try { config = validateConfig(); }
    catch (error) { setNotice("error", message(error)); return; }
    cancelPreview(true);
    setNotice(null, "");
    const generation = workspace.generation;
    const request = api(endpoint, {method: "PUT", json: {config, revision: workspace.snapshot.revision}});
    workspace.pendingSave = request;
    syncControls();
    try {
      const snapshot = await request;
      if (!isCurrent(generation)) return;
      workspace.snapshot = snapshot;
      workspace.draft = copy(snapshot.config);
      workspace.notice = {kind: "success", text: `策略已保存（版本 ${snapshot.revision}），后续工具调用开始使用新策略；正在执行的调用不会回滚。`};
      render();
    } catch (error) {
      if (!isCurrent(generation)) return;
      const uncertain = !error.status ? " 请重新读取，确认服务端是否已保存。" : "";
      setNotice("error", message(error) + uncertain);
    } finally {
      if (workspace.pendingSave === request) workspace.pendingSave = null;
      if (isCurrent(generation)) syncControls();
    }
  }

  function previewPayload() {
    const kind = node("gr-preview-kind").value;
    const toolName = kind === "builtin" ? node("gr-preview-builtin").value : node("gr-preview-mcp").value.trim();
    if (!toolName || toolName.length > 256 || /\s/.test(toolName)) throw new Error("请填写有效的工具名称，最多 256 字符且不能含空白。");
    let args;
    try { args = JSON.parse(node("gr-preview-arguments").value); }
    catch (_) { throw new Error("调用参数必须是有效 JSON，例如 {\"query\": \"示例\"}。"); }
    if (!args || typeof args !== "object" || Array.isArray(args)) throw new Error("调用参数必须是 JSON 对象，不能是数组、文本或 null。");
    const rawServer = kind === "mcp" ? node("gr-preview-server").value.trim() : "";
    const serverId = rawServer ? Number(rawServer) : null;
    if (serverId !== null && (!Number.isSafeInteger(serverId) || serverId < 1)) throw new Error("MCP 连接 ID 必须是正整数，或留空。");
    return {
      kind, tool_name: toolName, arguments: args,
      approval_policy: node("gr-preview-approval").value,
      mutating: kind === "mcp" && node("gr-preview-mutating").checked,
      destructive: kind === "mcp" && node("gr-preview-destructive").checked,
      server_id: serverId,
    };
  }

  async function preview() {
    if (!workspace.active || workspace.previewPending || workspace.pendingSave) return;
    let payload;
    try { payload = previewPayload(); }
    catch (error) { node("gr-preview-result").innerHTML = noticeMarkup("error", message(error)); return; }
    const generation = workspace.generation;
    const version = ++workspace.previewVersion;
    const controller = new AbortController();
    workspace.previewController?.abort();
    workspace.previewController = controller;
    workspace.previewPending = true;
    node("gr-preview-result").innerHTML = "";
    syncControls();
    try {
      const result = await api(`${endpoint}/preview`, {method: "POST", json: payload, signal: controller.signal});
      if (!isCurrent(generation) || version !== workspace.previewVersion) return;
      const decisions = {
        allow: {className: "is-allowed", title: "本护栏未拦截", icon: "check"},
        block: {className: "is-blocked", title: "调用将被护栏阻止", icon: "shield"},
        require_approval: {className: "is-approval", title: "调用需要人工审批", icon: "key"},
      };
      const decision = decisions[result.decision];
      if (!decision) throw new Error("服务端返回了无法识别的判定，请重新测试。");
      const rules = result.matched_rules || [];
      const revisionNote = Number.isInteger(result.revision) ? `测试策略版本 ${result.revision}${result.revision !== workspace.snapshot.revision ? " · 服务端策略已更新，请重新读取最新配置。" : ""}` : "";
      node("gr-preview-result").innerHTML = `<div class="gr-result ${decision.className}"><div class="gr-result-title">${icon(decision.icon, 17)}${decision.title}</div><p>${safe(result.reason || "服务端未返回详细原因。")}</p>${rules.length ? `<ul>${rules.map(rule => `<li>${icon("shield", 13)}<span>${safe(rule.name)}</span></li>`).join("")}</ul>` : ""}${revisionNote ? `<p>${safe(revisionNote)}</p>` : ""}<p>本次仅进行策略预览，没有执行工具，也未校验调用者的资源访问权限。</p></div>`;
    } catch (error) {
      if (!isCurrent(generation) || version !== workspace.previewVersion || error.name === "AbortError") return;
      node("gr-preview-result").innerHTML = noticeMarkup("error", message(error));
    } finally {
      if (version === workspace.previewVersion) {
        workspace.previewPending = false;
        workspace.previewController = null;
        if (isCurrent(generation)) syncControls();
      }
    }
  }

  async function load() {
    workspace.active = true;
    const generation = ++workspace.generation;
    workspace.readController?.abort();
    cancelPreview(false);
    const container = root();
    if (!container) return;
    if (Auth.role() !== "root") {
      container.innerHTML = `<div class="gr-error">${icon("shield", 28)}<strong>需要 root 管理权限</strong><p>护栏是全局执行策略，仅 root 可以读取和修改。</p></div>`;
      return;
    }
    container.innerHTML = `<div class="gr-loading" role="status" aria-live="polite">${icon("shield", 28)}<strong>正在读取护栏策略</strong><p>${workspace.pendingSave ? "等待正在提交的更改完成，再读取最新状态。" : "正在获取服务端配置、工具目录与安全边界。"}</p></div>`;
    // Mutations are never aborted on tab changes. Wait before reading to avoid
    // displaying a snapshot fetched before an in-flight save has committed.
    if (workspace.pendingSave) await workspace.pendingSave.catch(() => null);
    if (!isCurrent(generation)) return;
    const controller = new AbortController();
    workspace.readController = controller;
    try {
      const snapshot = await api(endpoint, {signal: controller.signal});
      if (!isCurrent(generation)) return;
      if (!snapshot?.config || !Number.isInteger(snapshot.revision) || !Array.isArray(snapshot.config.blocked_tools)) throw new Error("服务端策略数据不完整，请重新读取。");
      workspace.snapshot = snapshot;
      workspace.draft = copy(snapshot.config);
      workspace.notice = null;
      render();
    } catch (error) {
      if (!isCurrent(generation) || error.name === "AbortError") return;
      container.innerHTML = `<div class="gr-error" role="alert">${icon("shield", 28)}<strong>暂时无法读取护栏</strong><p>${safe(message(error))}</p><button type="button" class="gr-btn" id="gr-retry">${icon("restore", 15)}重新加载</button></div>`;
      node("gr-retry")?.addEventListener("click", () => load());
    } finally {
      if (workspace.readController === controller) workspace.readController = null;
    }
  }

  function leave() {
    workspace.active = false;
    workspace.generation += 1;
    workspace.readController?.abort();
    workspace.readController = null;
    cancelPreview(false);
  }

  window.GuardrailsGlobal = Object.freeze({load, leave});
})();

/* Named policy workspace. All mutations are validated and persisted by the API. */
(function () {
  "use strict";
  const base = "/api/v1/guardrails";
  const state = {active:false, generation:0, tab:"policies", query:"", catalog:null, policies:[], blocklists:[], wizard:null, editor:null, busy:false};
  const el = id => document.getElementById(id);
  const root = () => el("guardrails-content");
  const h = value => escapeHtml(String(value ?? ""));
  const clone = value => JSON.parse(JSON.stringify(value));
  const current = generation => state.active && state.generation === generation && root();
  const points = {user_input:"用户输入",tool_input:"工具输入",tool_output:"工具输出",model_output:"模型输出"};
  const labels = {blocklist:"阻止列表",pii:"个人信息",prompt_injection:"明显提示词注入"};
  const errorText = error => error?.status === 409 ? "此资源已更新或仍被引用，请刷新检查后重试。" : error?.status === 403 ? "当前账户没有此项权限。" : String(error?.message || "请求失败，请重试。");
  const date = value => value ? new Date(value).toLocaleString("zh-CN",{hour12:false}) : "—";
  const policyBody = row => Object.fromEntries(["name","description","enabled","rules","agent_ids","provider_ids","all_targets","revision"].filter(key=>row[key]!==undefined).map(key=>[key,clone(row[key])]));
  function feedback(message, bad=false) { const box=el("gp-feedback"); if(box) {box.textContent=message;box.className=`gp-feedback${bad?" is-error":""}`;} }
  function totals() { return `<div class="gp-counts"><span><b>${state.policies.length}</b> 个护栏</span><span><b>${state.policies.filter(p=>p.enabled).length}</b> 个已启用</span><span><b>${state.blocklists.length}</b> 份阻止列表</span></div>`; }
  function tabs() { return `<div class="gp-tabs" role="tablist">${[["policies","护栏"],["blocklists","阻止列表"],["integrations","集成"]].map(([id,label])=>`<button type="button" role="tab" aria-selected="${state.tab===id}" class="${state.tab===id?"active":""}" data-gp-tab="${id}">${label}</button>`).join("")}<span></span>${state.catalog.can_manage_global?`<button type="button" data-gp-tab="global" class="gp-global-link ${state.tab==="global"?"active":""}">${icon("settings",15)} 全局工具规则</button>`:""}</div>`; }
  function targetText(policy) { if(policy.all_targets) return Auth.role()==="root"?"所有任务":"本人所有任务"; const names=[]; for(const [key,options] of [["agent_ids",state.catalog.agents],["provider_ids",state.catalog.providers]]) for(const id of policy[key]||[]) names.push(options.find(row=>row.id===id)?.name || `#${id}`); return names.join("、") || "未绑定"; }
  function listMarkup() {
    const policies=state.policies.filter(row=>(row.name+row.description).toLowerCase().includes(state.query.toLowerCase()));
    return `<div class="gp-toolbar"><label class="gp-search">${icon("search",16)}<input id="gp-search" type="search" aria-label="搜索护栏" placeholder="按名称搜索护栏" value="${h(state.query)}"></label><button type="button" class="gp-primary" data-gp-action="create-policy">${icon("plus",16)} 创建护栏</button></div>${policies.length?`<div class="gp-table-wrap"><table class="gp-table"><thead><tr><th>名称</th><th>控件</th><th>绑定智能体 / 模型</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${policies.map(row=>`<tr><td><button type="button" class="gp-name" data-gp-action="edit-policy" data-id="${row.id}">${icon("shield",18)}${h(row.name)}</button><p>${h(row.description||"未填写说明")}</p></td><td><div class="gp-tags">${row.rules.map(rule=>`<span>${h(labels[rule.detector]||rule.detector)}</span>`).join("")}</div></td><td class="gp-target-cell">${h(targetText(row))}</td><td><span class="gp-status ${row.enabled?"on":""}">${row.enabled?"已启用":"已停用"}</span></td><td>${h(date(row.updated_at))}</td><td><div class="gp-row-actions"><button type="button" data-gp-action="toggle-policy" data-id="${row.id}">${row.enabled?"停用":"启用"}</button><button type="button" data-gp-action="edit-policy" data-id="${row.id}">编辑</button><button type="button" class="danger" data-gp-action="delete-policy" data-id="${row.id}">删除</button></div></td></tr>`).join("")}</tbody></table></div>`:empty("还没有匹配的护栏","创建命名护栏，选择检测控件和干预点，再绑定智能体或模型。", "create-policy","创建护栏")}`;
  }
  function empty(title, description, action, label) { return `<div class="gp-empty">${icon("shield",38)}<h3>${h(title)}</h3><p>${h(description)}</p>${action?`<button type="button" class="gp-primary" data-gp-action="${action}">${h(label)}</button>`:""}</div>`; }
  function blocklistMarkup() {
    const rows=state.blocklists.filter(row=>(row.name+row.description).toLowerCase().includes(state.query.toLowerCase()));
    return `<div class="gp-toolbar"><label class="gp-search">${icon("search",16)}<input id="gp-search" type="search" aria-label="搜索阻止列表" placeholder="按名称搜索阻止列表" value="${h(state.query)}"></label><div class="gp-row-actions"><button type="button" data-gp-action="import-list">导入 CSV</button><button type="button" class="gp-primary" data-gp-action="create-list">${icon("plus",16)} 创建阻止列表</button></div></div><p class="gp-hint">完整术语匹配支持大小写选项；安全正则限定为固定宽度表达式，避免不受控回溯。列表须绑定到护栏规则后才生效。</p>${rows.length?`<div class="gp-table-wrap"><table class="gp-table"><thead><tr><th>名称</th><th>条目数</th><th>匹配方式</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${rows.map(row=>`<tr><td><button type="button" class="gp-name" data-gp-action="edit-list" data-id="${row.id}">${h(row.name)}</button><p>${h(row.description)}</p></td><td>${row.entries.length}</td><td>${[...new Set(row.entries.map(item=>item.mode))].map(mode=>mode==="exact"?"完整术语":"安全正则").join("、")}</td><td>${h(date(row.updated_at))}</td><td><div class="gp-row-actions"><button type="button" data-gp-action="edit-list" data-id="${row.id}">编辑</button><button type="button" class="danger" data-gp-action="delete-list" data-id="${row.id}">删除</button></div></td></tr>`).join("")}</tbody></table></div>`:empty("建立可复用的阻止列表","逐条添加术语或导入 UTF-8 CSV，并在创建护栏时引用。","create-list","创建阻止列表")}`;
  }
  function integrationsMarkup() {
    return `<div class="gp-section-title"><div><h3>集成与生效范围</h3><p>${h(state.catalog.scope_note)}</p></div></div><div class="gp-integration-grid">${[["agents","智能体","agent_ids","bot"],["providers","模型","provider_ids","brain"]].map(([key,label,binding,ico])=>`<section class="gp-integration-card"><h3>${icon(ico,20)}${label}</h3>${state.catalog[key].length?state.catalog[key].map(target=>{const policies=state.policies.filter(policy=>policy.enabled&&(policy.all_targets||(policy[binding]||[]).includes(target.id)));return `<div class="gp-integration-row"><strong>${h(target.name)}</strong><div class="gp-tags">${policies.length?policies.map(policy=>`<button type="button" data-gp-action="edit-policy" data-id="${policy.id}">${h(policy.name)}</button>`).join(""):"<span class='muted'>未绑定启用的护栏</span>"}</div></div>`;}).join(""):"<p class='gp-hint'>当前账户没有可绑定资源。</p>"}</section>`).join("")}</div><div class="gp-boundary"><strong>执行边界</strong><p>用户输入在模型调用前检查；工具参数在执行前检查；工具返回在交给模型前检查。模型输出规则启用时，完整响应通过检查后才发送给用户。工具输出拦截不能撤销工具已经发生的写入。</p><p>同一护栏的智能体与模型绑定按任一命中生效。模型调用开始时读取规则快照，后续新调用读取最新规则。</p></div>`;
  }
  function render() {
    if(!state.active||!root())return;
    root().innerHTML=`<div class="gp-workspace">${tabs()}${totals()}<div id="gp-feedback" class="gp-feedback" role="status"></div><div id="gp-body">${state.tab==="policies"?listMarkup():state.tab==="blocklists"?blocklistMarkup():state.tab==="integrations"?integrationsMarkup():`<div id="gr-global-content"></div>`}</div><div id="gp-dialog-host"></div></div>`;
    root().onclick = click;
    el("gp-search")?.addEventListener("input",event=>{state.query=event.target.value;const pos=event.target.selectionStart;render();el("gp-search")?.focus();el("gp-search")?.setSelectionRange?.(pos,pos);});
    if(state.tab==="global")window.GuardrailsGlobal?.load();
    renderDialog();
  }
  function newPolicy(row) {
    state.wizard={id:row?.id,step:0,preview:null,draft:row?policyBody(row):{name:"",description:"",enabled:true,rules:[],agent_ids:[],provider_ids:[],all_targets:false}};
    state.editor=null;renderDialog();
  }
  function captureWizard() {
    const w=state.wizard;if(!w)return;
    for(const key of ["name","description"])if(el(`gp-policy-${key}`))w.draft[key]=el(`gp-policy-${key}`).value;
    root().querySelectorAll("[data-rule-index]").forEach(card=>{
      const rule=w.draft.rules[Number(card.dataset.ruleIndex)];
      rule.points=[...card.querySelectorAll("[data-rule-point]:checked")].map(input=>input.value);
      rule.action=card.querySelector("[data-rule-action]")?.value||rule.action;
      if(rule.detector==="blocklist")rule.blocklist_ids=[...card.querySelectorAll("[data-rule-list]:checked")].map(input=>Number(input.value));
      if(rule.detector==="pii")rule.pii_types=[...card.querySelectorAll("[data-pii]:checked")].map(input=>input.value);
    });
    if(el("gp-all-targets"))w.draft.all_targets=el("gp-all-targets").checked;
    if(el("gp-targets"))for(const key of ["agent_ids","provider_ids"])w.draft[key]=[...el("gp-targets").querySelectorAll(`[data-binding='${key}']:checked`)].map(input=>Number(input.value));
    if(el("gp-policy-enabled"))w.draft.enabled=el("gp-policy-enabled").checked;
  }
  function rulesMarkup(draft) {
    return `<div class="gp-form-grid"><label>护栏名称<input id="gp-policy-name" maxlength="128" value="${h(draft.name)}" placeholder="例如：客户服务数据保护" required></label><label>说明<input id="gp-policy-description" maxlength="2000" value="${h(draft.description)}" placeholder="记录用途与适用场景"></label></div><h3>选择检测控件</h3><div class="gp-detectors">${state.catalog.detectors.map(detector=>`<button type="button" class="gp-detector ${draft.rules.some(rule=>rule.detector===detector.id)?"selected":""}" data-gp-action="add-rule" data-detector="${h(detector.id)}" ${!detector.available?"disabled":""}><strong>${h(detector.name)}<span>${detector.available?draft.rules.some(rule=>rule.detector===detector.id)?"已添加":"添加控件":"未配置"}</span></strong><small>${h(detector.description)}</small></button>`).join("")}</div>${draft.rules.map((rule,index)=>`<section class="gp-rule-card" data-rule-index="${index}"><div class="gp-rule-heading"><h3>${h(labels[rule.detector])}</h3><button type="button" class="gp-text-button" data-gp-action="remove-rule" data-index="${index}">移除</button></div><div class="gp-rule-grid"><fieldset><legend>干预点</legend>${Object.entries(points).map(([id,name])=>`<label class="gp-check"><input type="checkbox" data-rule-point value="${id}" ${rule.points.includes(id)?"checked":""}>${name}</label>`).join("")}</fieldset><label>处理动作<select data-rule-action><option value="block" ${rule.action==="block"?"selected":""}>阻止：停止当前内容继续传递</option><option value="warn" ${rule.action==="warn"?"selected":""}>告警：记录命中并继续执行</option></select></label></div>${rule.detector==="blocklist"?`<fieldset><legend>绑定阻止列表</legend>${state.blocklists.length?state.blocklists.map(list=>`<label class="gp-check"><input type="checkbox" data-rule-list value="${list.id}" ${rule.blocklist_ids.includes(list.id)?"checked":""}>${h(list.name)} · ${list.entries.length} 条</label>`).join(""):"<p class='gp-hint'>尚无阻止列表。请先关闭向导，在阻止列表页创建或导入。</p>"}</fieldset>`:rule.detector==="pii"?`<fieldset><legend>识别格式</legend>${[["email","邮箱"],["phone","中国大陆手机号"],["china_id","18位身份证格式"]].map(([id,name])=>`<label class="gp-check"><input type="checkbox" data-pii value="${id}" ${rule.pii_types.includes(id)?"checked":""}>${name}</label>`).join("")}</fieldset>`:"<p class='gp-hint'>仅固定模式检查，不能识别全部语义改写或间接注入。</p>"}</section>`).join("")}`;
  }
  function targetsMarkup(draft) {
    return `<h3>选择智能体和模型</h3><p class="gp-hint">${h(state.catalog.scope_note)}</p><label class="gp-check gp-all"><input type="checkbox" id="gp-all-targets" ${draft.all_targets?"checked":""}>${Auth.role()==="root"?"作用于所有任务":"作用于本人所有任务"}</label><div class="gp-target-columns" id="gp-targets">${[["agents","智能体","agent_ids"],["providers","模型","provider_ids"]].map(([key,label,binding])=>`<fieldset><legend>${label} · ${state.catalog[key].length}</legend>${state.catalog[key].length?state.catalog[key].map(item=>`<label class="gp-target-option"><input type="checkbox" data-binding="${binding}" value="${item.id}" ${(draft[binding]||[]).includes(item.id)?"checked":""}><span>${h(item.name)}</span></label>`).join(""):"<p class='gp-hint'>当前账户没有可绑定资源。</p>"}</fieldset>`).join("")}</div>`;
  }
  function reviewMarkup(w) {
    const draft=w.draft;
    return `<div class="gp-review-title">${icon("shield",30)}<div><h3>${h(draft.name)}</h3><p>${h(draft.description||"未填写说明")}</p></div></div><dl class="gp-review"><dt>绑定范围</dt><dd>${h(targetText(draft))}</dd><dt>检测控件</dt><dd>${draft.rules.map(rule=>`<div><strong>${h(labels[rule.detector])}</strong> · ${rule.action==="block"?"阻止":"告警"}<p>${rule.points.map(point=>points[point]).join(" / ")}</p></div>`).join("")}</dd></dl><label class="gp-check gp-all"><input type="checkbox" id="gp-policy-enabled" ${draft.enabled?"checked":""}>保存后启用护栏</label><section class="gp-preview"><div class="gp-rule-heading"><h3>测试当前草稿</h3><select id="gp-preview-point" aria-label="选择测试干预点">${Object.entries(points).map(([id,label])=>`<option value="${id}">${label}</option>`).join("")}</select></div><textarea id="gp-preview-text" maxlength="100000" placeholder="输入测试文本；不会调用模型，也不会保存测试内容。"></textarea><button type="button" data-gp-action="preview-policy">运行检测</button><div id="gp-preview-result" role="status"></div></section><p class="gp-hint">确定性检测有误报和漏报；未配置的语义风险检测项不会生效。模型输出检查会缓冲响应，完整检查通过后才发送。</p>`;
  }
  function renderDialog() {
    const host=el("gp-dialog-host");if(!host)return;
    if(!state.wizard&&!state.editor){host.innerHTML="";return;}
    const w=state.wizard;
    const title=w?(w.id?"编辑护栏":"创建护栏"):(state.editor.import?"导入阻止列表 CSV":state.editor.id?"编辑阻止列表":"创建阻止列表");
    host.innerHTML=`<div class="gp-overlay"><section class="gp-dialog" role="dialog" aria-modal="true" aria-labelledby="gp-dialog-title"><header><div><small>GUARDRAILS</small><h2 id="gp-dialog-title">${title}</h2></div><button type="button" class="gp-close" aria-label="关闭" data-gp-action="close-dialog">×</button></header>${w?`<ol class="gp-steps">${["添加控件","选择智能体和模型","审查"].map((label,index)=>`<li class="${w.step===index?"active":w.step>index?"done":""}"><span>${index+1}</span>${label}</li>`).join("")}</ol>`:""}<div class="gp-dialog-body">${w?(w.step===0?rulesMarkup(w.draft):w.step===1?targetsMarkup(w.draft):reviewMarkup(w)):editorMarkup()}<div id="gp-dialog-error" class="gp-dialog-error" role="alert"></div></div><footer><button type="button" data-gp-action="${w&&w.step>0?"previous":"close-dialog"}">${w&&w.step>0?"上一步":"取消"}</button><button type="button" class="gp-primary" data-gp-action="${w?(w.step<2?"next":"save-policy"):state.editor.import?"save-import":"save-list"}">${w&&w.step<2?"下一步":state.editor?.import?"导入":"保存"}</button></footer></section></div>`;
    host.querySelector("input,button")?.focus();
    host.querySelectorAll("input,textarea,select").forEach(input=>input.addEventListener("input",()=>{if(el("gp-preview-result"))el("gp-preview-result").textContent="";}));
  }
  function editorMarkup() {
    const e=state.editor;
    return `<div class="gp-form-grid"><label>列表名称<input id="gp-list-name" maxlength="128" value="${h(e.name)}" placeholder="例如：敏感业务术语"></label><label>说明<input id="gp-list-description" maxlength="2000" value="${h(e.description)}"></label></div>${e.import?`<div class="gp-upload"><input type="file" id="gp-csv-file" accept=".csv,text/csv"><p>UTF-8 CSV，上限256KB、1000条。表头：<code>value,mode,case_sensitive</code></p><pre>value,mode,case_sensitive\n内部项目代号,exact,false\n1[3-9]\\d{9},regex,false</pre></div>`:`<div class="gp-rule-heading"><h3>匹配条目</h3><button type="button" data-gp-action="add-entry">添加条目</button></div><div class="gp-entry-list">${e.entries.map((entry,index)=>`<div class="gp-entry" data-entry="${index}"><input data-entry-value value="${h(entry.value)}" maxlength="256" aria-label="条目${index+1}" placeholder="完整术语或正则表达式"><select data-entry-mode aria-label="匹配方式"><option value="exact" ${entry.mode==="exact"?"selected":""}>完整术语</option><option value="regex" ${entry.mode==="regex"?"selected":""}>安全正则</option></select><label class="gp-check"><input type="checkbox" data-entry-case ${entry.case_sensitive?"checked":""}>区分大小写</label><button type="button" aria-label="删除条目${index+1}" data-gp-action="remove-entry" data-index="${index}">×</button></div>`).join("")}</div><p class="gp-hint">英文和数字术语按完整词边界匹配，中文按完整字串匹配。安全正则只接受字符类、转义、锚点与固定 {n} 重复；不支持分组、分支和可变重复。最多1000条。</p>`}`;
  }
  function captureEditor() { const e=state.editor;if(!e)return; e.name=el("gp-list-name")?.value||""; e.description=el("gp-list-description")?.value||""; if(!e.import)e.entries=[...root().querySelectorAll("[data-entry]")].map(row=>({value:row.querySelector("[data-entry-value]").value,mode:row.querySelector("[data-entry-mode]").value,case_sensitive:row.querySelector("[data-entry-case]").checked})); }
  function validateStep() {
    const w=state.wizard;captureWizard();
    if(!w.draft.name.trim())throw new Error("请填写护栏名称。");
    if(!w.draft.rules.length)throw new Error("请添加至少一个已配置的检测控件。");
    for(const rule of w.draft.rules){if(!rule.points.length)throw new Error("每个控件至少选择一个干预点。");if(rule.detector==="blocklist"&&!rule.blocklist_ids.length)throw new Error("请为阻止列表控件选择一份列表。");if(rule.detector==="pii"&&!rule.pii_types.length)throw new Error("请至少选择一种个人信息格式。");}
    if(w.step>=1&&!w.draft.all_targets&&!w.draft.agent_ids.length&&!w.draft.provider_ids.length)throw new Error("请选择智能体或模型，或选择全部可作用任务。");
  }
  async function mutate(path,options) { if(state.busy)return false;state.busy=true; const generation=state.generation;root()?.querySelectorAll("button").forEach(button=>button.disabled=true);try{await api(base+path,options);if(current(generation)){state.wizard=null;state.editor=null;await refresh();feedback("已保存，后续新调用将使用最新配置。");}return true;}finally{state.busy=false;if(current(generation))root()?.querySelectorAll("button").forEach(button=>{if(!button.classList.contains("gp-detector")||state.catalog.detectors.find(row=>row.id===button.dataset.detector)?.available)button.disabled=false;});} }
  async function click(event) {
    const target=event.target.closest("[data-gp-action],[data-gp-tab]");if(!target||!root()?.contains(target)||state.busy)return;
    try {
      if(target.dataset.gpTab){window.GuardrailsGlobal?.leave();state.tab=target.dataset.gpTab;state.query="";state.wizard=null;state.editor=null;render();return;}
      const action=target.dataset.gpAction,id=Number(target.dataset.id),index=Number(target.dataset.index);
      const policy=state.policies.find(row=>row.id===id),list=state.blocklists.find(row=>row.id===id);
      if(action==="close-dialog"){state.wizard=null;state.editor=null;renderDialog();}
      if(action==="create-policy"||action==="edit-policy")newPolicy(policy);
      if(action==="add-rule"){captureWizard();const detector=target.dataset.detector;if(!state.wizard.draft.rules.some(rule=>rule.detector===detector))state.wizard.draft.rules.push({detector,points:["user_input","model_output"],action:"block",blocklist_ids:[],pii_types:["email","phone","china_id"],enabled:true});renderDialog();}
      if(action==="remove-rule"){captureWizard();state.wizard.draft.rules.splice(index,1);renderDialog();}
      if(action==="next"){validateStep();state.wizard.step++;renderDialog();}
      if(action==="previous"){captureWizard();state.wizard.step--;renderDialog();}
      if(action==="save-policy"){validateStep();await mutate(state.wizard.id?`/policies/${state.wizard.id}`:"/policies",{method:state.wizard.id?"PUT":"POST",json:state.wizard.draft});}
      if(action==="toggle-policy")await mutate(`/policies/${id}`,{method:"PUT",json:{...policyBody(policy),enabled:!policy.enabled}});
      if(action==="delete-policy"&&confirm(`删除护栏“${policy.name}”？`))await mutate(`/policies/${id}`,{method:"DELETE"});
      if(action==="preview-policy"){
        captureWizard();const w=state.wizard,generation=state.generation,text=el("gp-preview-text").value,point=el("gp-preview-point").value;target.disabled=true;
        try{const result=await api(base+"/policies/preview",{method:"POST",json:{policy:w.draft,point,text}});if(current(generation)&&state.wizard===w&&el("gp-preview-text")?.value===text&&el("gp-preview-point")?.value===point){el("gp-preview-result").innerHTML=`<div class="gp-test-result ${h(result.decision)}"><strong>${{allow:"未命中当前规则",warn:"命中告警规则",block:"已拦截"}[result.decision]||h(result.decision)}</strong>${result.matches.map(match=>`<p>${h(labels[match.detector]||match.detector)} · ${match.action==="block"?"阻止":"告警"} · ${match.count} 类模式命中</p>`).join("")}<small>仅测试当前草稿，不代表语义内容已被全面检查。</small></div>`;}}finally{if(current(generation))target.disabled=false;}
      }
      if(["create-list","edit-list","import-list"].includes(action)){state.editor=action==="edit-list"?clone(list):{name:"",description:"",entries:[{value:"",mode:"exact",case_sensitive:false}],import:action==="import-list"};state.wizard=null;renderDialog();}
      if(action==="add-entry"){captureEditor();if(state.editor.entries.length>=1000)throw new Error("最多1000条。");state.editor.entries.push({value:"",mode:"exact",case_sensitive:false});renderDialog();}
      if(action==="remove-entry"){captureEditor();state.editor.entries.splice(index,1);renderDialog();}
      if(action==="save-list"){captureEditor();const e=state.editor;if(!e.name.trim()||!e.entries.length||e.entries.some(entry=>!entry.value.trim()))throw new Error("请填写名称和至少一条非空匹配项。");await mutate(e.id?`/blocklists/${e.id}`:"/blocklists",{method:e.id?"PUT":"POST",json:{name:e.name,description:e.description,entries:e.entries,...(e.id?{revision:e.revision}:{})}});}
      if(action==="save-import"){captureEditor();const e=state.editor,file=el("gp-csv-file")?.files?.[0];if(!e.name.trim()||!file)throw new Error("请填写列表名称并选择CSV文件。");if(file.size>262144)throw new Error("CSV文件不能超过256KB。");const body=new FormData();body.append("file",file);body.append("name",e.name);body.append("description",e.description);await mutate("/blocklists/import",{method:"POST",body});}
      if(action==="delete-list"&&confirm(`删除阻止列表“${list.name}”？仍被护栏引用时无法删除。`))await mutate(`/blocklists/${id}`,{method:"DELETE"});
    }catch(error){if(!state.active)return;const box=el("gp-dialog-error");if(box)box.textContent=errorText(error);else feedback(errorText(error),true);}
  }
  async function refresh() {
    const generation=state.generation;
    const [catalog,policies,blocklists]=await Promise.all([api(base+"/catalog"),api(base+"/policies"),api(base+"/blocklists")]);
    if(!current(generation))return;
    state.catalog=catalog;state.policies=policies.items;state.blocklists=blocklists.items;render();
  }
  async function load() {
    state.active=true;const generation=++state.generation;state.wizard=null;state.editor=null;
    if(!root())return;
    root().innerHTML=`<div class="gp-empty" role="status">${icon("shield",36)}<h3>正在读取护栏工作区</h3><p>获取命名策略、阻止列表和可绑定资源。</p></div>`;
    try{await refresh();}catch(error){if(current(generation)){root().innerHTML=`<div class="gp-empty" role="alert"><h3>无法加载护栏</h3><p>${h(errorText(error))}</p><button type="button" id="gp-retry">重试</button></div>`;el("gp-retry").onclick = load;}}
  }
  function leave(){state.active=false;state.generation++;state.wizard=null;state.editor=null;window.GuardrailsGlobal?.leave();}
  window.GuardrailsAdmin=Object.freeze({load,leave});
})();
