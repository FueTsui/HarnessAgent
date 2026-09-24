/* Account-owned explicit memory. Each scope resolves to a concrete runtime boundary. */
(() => {
  const scopes = {
    custom: {name: "自定义记忆", icon: "bot", copy: "为指定智能体保存你的偏好、术语与工作约定，仅在该智能体的后续运行中参与召回。", target: "agents", field: "agent_id", label: "适用智能体"},
    context: {name: "上下文记忆", icon: "history", copy: "保存特定会话中的任务背景与关键事实，仅在该会话内参与召回。被排除的会话不会提供记忆候选。", target: "threads", field: "thread_id", label: "适用会话"},
    project: {name: "项目记忆", icon: "folder", copy: "沉淀项目背景、约定和决策，在当前账户的同一项目下跨会话复用。", target: "projects", field: "project_id", label: "适用项目"},
    global: {name: "全局记忆", icon: "database", copy: "保存你的通用偏好与事实，在当前账户的不同项目和智能体中复用。全局范围始终只属于你。", label: "当前账户全部项目"},
  };
  const state = {scope: "custom", query: "", filter: "all", offset: 0, limit: 50, rows: [], targets: {}, counts: {}, total: 0, request: 0, lifecycle: 0, active: false, timer: null, editing: null, saving: false};
  const $ = id => document.getElementById(id);
  const e = value => escapeHtml(value);
  const time = value => value ? new Date(value).toLocaleString("zh-CN", {year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}) : "—";
  const toast = value => typeof showToast === "function" && showToast(value);

  function shell() {
    $("memory-content").innerHTML = `<div class="memory-workspace">
      <div class="memory-scope-tabs" role="tablist" aria-label="记忆范围">${Object.entries(scopes).map(([key, scope]) => `<button type="button" role="tab" id="memory-tab-${key}" aria-controls="memory-scope-panel" data-memory-scope="${key}" aria-selected="${state.scope === key}" tabindex="${state.scope === key ? 0 : -1}">${icon(scope.icon, 17)}<span>${scope.name}</span><b data-memory-count="${key}">0</b></button>`).join("")}</div>
      <section id="memory-scope-panel" role="tabpanel" aria-labelledby="memory-tab-${state.scope}">
        <div class="memory-scope-intro"><div class="memory-scope-symbol" id="memory-scope-symbol"></div><div><h3 id="memory-scope-title"></h3><p id="memory-scope-copy"></p></div><button class="btn" id="memory-new">${icon("plus", 16)}新增记忆</button></div>
        <div class="memory-privacy-note">${icon("shield", 15)}<span>仅当前账户可见 · 相关记忆作为低权重参考，当前请求优先 · 召回遵循智能体和会话的记忆开关</span></div>
        <div class="memory-toolbar"><label class="admin-search"><span>${icon("search", 16)}</span><input id="memory-search" type="search" maxlength="200" placeholder="搜索标题或记忆内容" aria-label="搜索记忆" value="${e(state.query)}" /></label><label class="admin-filter"><span>状态</span><select id="memory-filter" aria-label="记忆状态"><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label><span id="memory-result-count" role="status" aria-live="polite"></span></div>
        <div id="memory-list" class="memory-entry-list" aria-live="polite"></div>
        <div class="memory-pagination" id="memory-pagination" hidden><span id="memory-page-description"></span><button class="btn small ghost" id="memory-prev">上一页</button><button class="btn small ghost" id="memory-next">下一页</button></div>
      </section></div>`;
    $("memory-filter").value = state.filter;
    updateScope();
    $("memory-content").onclick = handleClick;
    $("memory-content").querySelector(".memory-scope-tabs").onkeydown = event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const keys = Object.keys(scopes), index = keys.indexOf(state.scope);
      const next = event.key === "Home" ? 0 : event.key === "End" ? keys.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + keys.length) % keys.length;
      event.preventDefault(); selectScope(keys[next]); $("memory-tab-" + keys[next]).focus();
    };
    $("memory-search").oninput = event => {
      state.query = event.target.value; state.offset = 0;
      clearTimeout(state.timer); state.timer = setTimeout(() => fetchRows(), 220);
    };
    $("memory-filter").onchange = event => { state.filter = event.target.value; state.offset = 0; fetchRows(); };
  }

  function updateScope() {
    const scope = scopes[state.scope];
    $("memory-scope-title").textContent = scope.name;
    $("memory-scope-copy").textContent = scope.copy;
    $("memory-scope-symbol").innerHTML = icon(scope.icon, 23);
    $("memory-scope-panel").setAttribute("aria-labelledby", "memory-tab-" + state.scope);
    $("memory-content").querySelector(".memory-workspace").dataset.scope = state.scope;
    $("memory-content").querySelectorAll("[data-memory-scope]").forEach(tab => {
      tab.setAttribute("aria-selected", String(tab.dataset.memoryScope === state.scope));
      tab.tabIndex = tab.dataset.memoryScope === state.scope ? 0 : -1;
    });
  }

  function selectScope(scope) {
    if (!scopes[scope] || scope === state.scope) return;
    state.scope = scope; state.offset = 0; updateScope(); fetchRows();
  }

  async function fetchRows() {
    const request = ++state.request;
    const params = new URLSearchParams({scope: state.scope, q: state.query, offset: String(state.offset), limit: String(state.limit)});
    if (state.filter !== "all") params.set("enabled", String(state.filter === "enabled"));
    $("memory-list").innerHTML = '<div class="empty-state" role="status">正在加载记忆…</div>';
    $("memory-pagination").hidden = true;
    try {
      const result = await api("/api/v1/memories?" + params);
      if (!state.active || request !== state.request) return;
      state.rows = result.items; state.counts = result.counts; state.total = result.total;
      if (state.offset && state.offset >= state.total) { state.offset = Math.max(0, Math.ceil(state.total / state.limit) - 1) * state.limit; return fetchRows(); }
      renderRows();
    } catch (error) {
      if (!state.active || request !== state.request) return;
      $("memory-list").innerHTML = `<div class="empty-state"><strong>记忆加载失败</strong><span>${e(error.message)}</span><button class="btn ghost" data-memory-retry>重试</button></div>`;
      $("memory-result-count").textContent = "加载失败";
    }
  }

  function renderRows() {
    $("memory-content").querySelectorAll("[data-memory-count]").forEach(count => count.textContent = String(state.counts[count.dataset.memoryCount] || 0));
    $("memory-result-count").textContent = `${state.total} 条记忆`;
    $("memory-list").innerHTML = state.rows.map(row => `<article class="memory-entry ${row.enabled ? "" : "is-disabled"}">
      <div class="memory-entry-head"><span class="memory-entry-mark">${icon(scopes[row.scope].icon, 18)}</span><div><h4>${e(row.title)}</h4><span>${e(row.target_name)}</span></div><span class="status ${row.enabled ? "done" : "cancelled"}">${row.enabled ? "已启用" : "已停用"}</span></div>
      <p class="memory-entry-text">${e(row.content)}</p>
      <div class="memory-entry-footer"><span title="${e(row.source)}">来源：${e(row.source)}</span><time datetime="${e(row.updated_at)}">${e(time(row.updated_at))}</time><div class="memory-entry-actions"><button class="btn small ghost" data-memory-edit="${row.id}">编辑</button><button class="btn small ghost" data-memory-toggle="${row.id}">${row.enabled ? "停用" : "启用"}</button><button class="btn small danger" data-memory-delete="${row.id}">删除</button></div></div>
    </article>`).join("") || `<div class="empty-state collection-empty">${icon(state.query || state.filter !== "all" ? "search" : scopes[state.scope].icon, 28)}<strong>${state.query || state.filter !== "all" ? "没有匹配的记忆" : "还没有" + scopes[state.scope].name}</strong><span>${state.query || state.filter !== "all" ? "调整搜索词或状态筛选后再试。" : "添加经过确认的偏好与事实，在后续相关任务中复用。"}</span>${!state.query && state.filter === "all" ? '<button class="btn ghost" data-memory-add>新增第一条记忆</button>' : ""}</div>`;
    $("memory-pagination").hidden = state.total <= state.limit;
    $("memory-page-description").textContent = `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)} / ${state.total}`;
    $("memory-prev").disabled = state.offset === 0;
    $("memory-next").disabled = state.offset + state.limit >= state.total;
  }

  async function handleClick(event) {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    if (button.dataset.memoryScope) return selectScope(button.dataset.memoryScope);
    if (button.id === "memory-new" || button.hasAttribute("data-memory-add")) return openEditor();
    if (button.hasAttribute("data-memory-retry")) return fetchRows();
    if (button.id === "memory-prev" || button.id === "memory-next") { state.offset += button.id === "memory-prev" ? -state.limit : state.limit; return fetchRows(); }
    const id = Number(button.dataset.memoryEdit || button.dataset.memoryToggle || button.dataset.memoryDelete);
    const row = state.rows.find(item => item.id === id);
    if (!row) return;
    if (button.dataset.memoryEdit) return openEditor(row);
    const remove = !!button.dataset.memoryDelete;
    if (remove && !confirm(`删除记忆“${row.title}”？删除后不再参与后续召回。`)) return;
    button.disabled = true;
    try {
      await api(`/api/v1/memories/${id}`, remove ? {method: "DELETE"} : {method: "PATCH", json: {enabled: !row.enabled}});
      toast(remove ? "记忆已删除" : row.enabled ? "记忆已停用" : "记忆已启用");
      if (state.active) await fetchRows();
    } catch (error) { toast(error.message); if (button.isConnected) button.disabled = false; }
  }

  function ensureEditor() {
    if ($("memory-editor")) return;
    const dialog = document.createElement("dialog"); dialog.id = "memory-editor"; dialog.className = "memory-editor";
    dialog.innerHTML = `<form id="memory-editor-form"><h3 id="memory-editor-title">新增记忆</h3><p class="dialog-copy">保存可复用的事实与偏好。内容作为参考背景，不会覆盖当前请求。</p>
      <label for="memory-edit-scope">记忆范围</label><select id="memory-edit-scope">${Object.entries(scopes).map(([key, scope]) => `<option value="${key}">${scope.name}</option>`).join("")}</select>
      <div id="memory-target-field"><label for="memory-edit-target" id="memory-target-label"></label><select id="memory-edit-target"></select></div><p id="memory-target-help" class="field-help"></p>
      <label for="memory-edit-title">标题</label><input id="memory-edit-title" required maxlength="160" placeholder="例如：项目文档采用中文编写" />
      <label for="memory-edit-content">记忆内容</label><textarea id="memory-edit-content" required maxlength="6000" rows="6" placeholder="写下需要在后续任务中参考的事实、背景或偏好"></textarea>
      <label for="memory-edit-source">来源说明</label><input id="memory-edit-source" maxlength="300" placeholder="例如：手动录入、项目会议结论" />
      <label class="memory-enabled-field"><input id="memory-edit-enabled" type="checkbox" checked />启用后参与相关任务召回</label>
      <p id="memory-editor-error" class="error" role="alert"></p><div class="dialog-actions"><button class="btn ghost" type="button" id="memory-editor-cancel">取消</button><button class="btn" type="submit" id="memory-editor-save">保存记忆</button></div></form>`;
    document.body.appendChild(dialog);
    $("memory-edit-scope").onchange = updateTargets;
    $("memory-editor-cancel").onclick = () => dialog.close();
    dialog.addEventListener("cancel", event => { if (state.saving) event.preventDefault(); });
    $("memory-editor-form").onsubmit = saveEditor;
  }

  function updateTargets() {
    const key = $("memory-edit-scope").value, scope = scopes[key];
    const current = state.editing?.scope === key ? state.editing[scope.field] : null;
    const options = state.targets[scope.target] || [];
    $("memory-target-field").hidden = !scope.target;
    $("memory-target-label").textContent = scope.label;
    $("memory-edit-target").required = !!scope.target;
    $("memory-edit-target").innerHTML = '<option value="">请选择</option>' + options.map(item => `<option value="${e(item.id)}" ${String(item.id) === String(current) ? "selected" : ""}>${e(item.name)}${item.excluded ? "（已排除召回）" : ""}${item.memory_enabled === false ? "（记忆开关关闭）" : ""}</option>`).join("");
    // Keep an existing binding editable if its target is outside the recent-target list.
    if (current && !options.some(item => String(item.id) === String(current))) {
      $("memory-edit-target").insertAdjacentHTML("beforeend", `<option value="${e(current)}" selected>${e(state.editing.target_name)}</option>`);
    }
    $("memory-target-help").textContent = scope.target && !options.length ? `当前没有可选${scope.label.slice(2)}，可先创建目标或改用全局记忆。` : scope.copy;
  }

  function openEditor(row = null) {
    if (state.saving) return;
    ensureEditor(); state.editing = row;
    $("memory-editor-title").textContent = row ? "编辑记忆" : "新增记忆";
    $("memory-edit-scope").value = row?.scope || state.scope;
    $("memory-edit-title").value = row?.title || "";
    $("memory-edit-content").value = row?.content || "";
    $("memory-edit-source").value = row?.source || "手动录入";
    $("memory-edit-enabled").checked = row?.enabled ?? true;
    $("memory-editor-error").textContent = "";
    updateTargets(); $("memory-editor").showModal(); $("memory-edit-title").focus();
  }

  async function saveEditor(event) {
    event.preventDefault();
    if (state.saving) return;
    const scope = $("memory-edit-scope").value, spec = scopes[scope];
    const payload = {scope, title: $("memory-edit-title").value.trim(), content: $("memory-edit-content").value.trim(), source: $("memory-edit-source").value.trim(), enabled: $("memory-edit-enabled").checked};
    if (spec.field) payload[spec.field] = scope === "context" ? $("memory-edit-target").value : Number($("memory-edit-target").value);
    if (!payload.title || !payload.content || (spec.field && !payload[spec.field])) { $("memory-editor-error").textContent = "请填写标题、内容并选择适用范围。"; return; }
    const editingId = state.editing?.id;
    state.saving = true; $("memory-editor-save").disabled = true; $("memory-editor-cancel").disabled = true;
    try {
      await api(editingId ? `/api/v1/memories/${editingId}` : "/api/v1/memories", {method: editingId ? "PATCH" : "POST", json: payload});
      $("memory-editor").close(); toast("记忆已保存，后续相关运行将按当前开关使用");
      if (state.active) { state.scope = scope; state.offset = 0; updateScope(); await fetchRows(); }
    } catch (error) { $("memory-editor-error").textContent = error.message; }
    finally { state.saving = false; $("memory-editor-save").disabled = false; $("memory-editor-cancel").disabled = false; }
  }

  window.MemoryAdmin = {
    async load() {
      state.active = true; shell();
      const lifecycle = ++state.lifecycle;
      ++state.request;
      $("memory-new").disabled = true;
      $("memory-list").innerHTML = '<div class="empty-state" role="status">正在加载记忆…</div>';
      try {
        const targets = await api("/api/v1/memories/targets");
        if (!state.active || lifecycle !== state.lifecycle) return;
        state.targets = targets; $("memory-new").disabled = false;
        await fetchRows();
      } catch (error) {
        if (!state.active || lifecycle !== state.lifecycle) return;
        $("memory-list").innerHTML = `<div class="empty-state"><strong>加载记忆目标失败</strong><span>${e(error.message)}</span><button class="btn ghost" id="memory-target-retry">重试</button></div>`;
        $("memory-target-retry").onclick = () => window.MemoryAdmin.load();
      }
    },
    leave() { state.active = false; ++state.request; ++state.lifecycle; clearTimeout(state.timer); $("memory-editor")?.close(); },
  };
})();
