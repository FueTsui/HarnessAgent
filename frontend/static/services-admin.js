/* 自定义服务目录、试运行及当前账户执行记录。 */
(() => {
  const e = value => escapeHtml(String(value ?? ""));
  const $ = id => document.getElementById(id);
  const state = {active: false, generation: 0, tab: "playground", kind: "", query: "", items: [], runs: [], agents: [], canProgram: false};
  const label = kind => kind === "program" ? "编程服务" : "网络服务";
  const time = value => value ? new Date(value).toLocaleString("zh-CN", {hour12: false}) : "—";
  const empty = (title, copy, create = false) => `<div class="service-empty"><span class="service-empty-icon">${icon("server", 36)}</span><h3>${title}</h3><p>${copy}</p>${create ? '<button class="btn" data-service-create>创建服务</button>' : ""}</div>`;

  function render() {
    if (!state.active) return;
    const items = state.items.filter(item => (!state.kind || item.kind === state.kind) && `${item.name} ${item.description}`.toLowerCase().includes(state.query.toLowerCase()));
    $("services-content").innerHTML = `<div class="section-heading"><div><div class="section-kicker">SERVICES</div><h2>${state.kind ? label(state.kind) : "服务"}</h2><p>${state.kind ? "注册可供智能体调用的服务，配置输入参数与可使用的智能体。" : "连接网络接口或本地程序，测试输入输出并查看自己的执行记录。"}</p></div></div>
      ${!state.kind ? `<nav class="module-subnav" aria-label="服务视图">${[["playground", "操场"], ["custom", "自定义"], ["data", "数据"]].map(([key, text]) => `<button data-service-tab="${key}" class="${state.tab === key ? "active" : ""}" ${state.tab === key ? 'aria-current="page"' : ""}>${text}</button>`).join("")}</nav>` : ""}
      <div class="admin-list-toolbar service-toolbar"><label class="admin-search">${icon("search", 18)}<input id="service-search" type="search" aria-label="搜索服务" placeholder="搜索名称或用途" value="${e(state.query)}"></label><span class="admin-result-count">${state.tab === "data" && !state.kind ? `${state.runs.length} 条最近记录` : `${items.length} 个服务`}</span><button class="btn ghost" id="services-refresh">${icon("refresh", 15)}刷新</button>${(state.tab !== "data" || state.kind) && (state.kind !== "program" || state.canProgram) ? '<button class="btn" data-service-create>创建服务</button>' : ""}</div>
      ${state.tab === "data" && !state.kind ? renderRuns() : renderCatalog(items)}
      ${state.kind ? '<p class="service-footnote">在服务配置中绑定现有智能体，再由系统管理员为该智能体分配 service_list 和 service_call 内置工具。调用仍受全局工具开关、审批策略和护栏限制。</p>' : ""}`;
    $("service-search").oninput = event => {
      const position = event.target.selectionStart;
      state.query = event.target.value; render();
      $("service-search").focus();
      try { $("service-search").setSelectionRange(position, position); } catch (_) { /* search 类型不支持选区 */ }
    };
    $("services-refresh").onclick = refresh;
    $("services-content").querySelectorAll("[data-service-tab]").forEach(b => b.onclick = () => { state.tab = b.dataset.serviceTab; state.query = ""; if (state.tab === "data") refresh(); else render(); });
    $("services-content").querySelectorAll("[data-service-create]").forEach(b => b.onclick = () => editor());
    $("services-content").querySelectorAll("[data-service-edit]").forEach(b => b.onclick = () => editor(state.items.find(item => item.id === Number(b.dataset.serviceEdit))));
    $("services-content").querySelectorAll("[data-service-run]").forEach(b => b.onclick = () => playground(state.items.find(item => item.id === Number(b.dataset.serviceRun))));
    $("services-content").querySelectorAll("[data-service-delete]").forEach(b => b.onclick = () => remove(state.items.find(item => item.id === Number(b.dataset.serviceDelete))));
    $("services-content").querySelectorAll("[data-service-result]").forEach(b => b.onclick = () => {
      const run = state.runs.find(r => r.id === Number(b.dataset.serviceResult));
      const dialog = modal("执行结果", `<p>${e(run.service_name)} · ${e(time(run.created_at))}</p><pre class="service-output">${e(run.result || "无输出")}</pre>`, "关闭");
      dialog.querySelector("[data-service-submit]").onclick = () => dialog.close();
    });
    hydrateIcons();
  }

  function renderCatalog(items) {
    if (!items.length && state.kind === "program" && !state.canProgram) return empty("暂无可用的编程服务", "系统管理员注册并共享本地程序后，可在这里测试和使用。");
    if (!items.length) return empty(state.query ? "没有匹配的服务" : "尚未创建自定义服务", state.query ? "尝试其他名称或用途关键词。" : "接入已有 HTTP 接口，或注册接收 JSON 输入的本地程序。", !state.query);
    return `<div class="resource-table-wrap"><table class="resource-table service-table"><thead><tr><th>名称</th><th>类型</th><th>应用对象</th><th>状态</th><th>上次修改</th><th>操作</th></tr></thead><tbody>${items.map(item => `<tr><td><button class="service-name" data-service-run="${item.id}" ${!item.enabled ? "disabled" : ""}>${e(item.name)}</button><small>${e(item.description || "尚未填写用途")}</small></td><td><span class="tag">${label(item.kind)}</span></td><td>${item.agent_ids.length ? item.agent_ids.map(id => e(state.agents.find(a => a.id === id)?.name || `智能体 #${id}`)).join("、") : '<span class="hint">未绑定</span>'}</td><td><span class="status ${item.enabled ? "completed" : "cancelled"}">${item.enabled ? "已启用" : "已停用"}</span>${item.is_public ? '<small>已共享</small>' : ""}</td><td>${e(time(item.updated_at))}</td><td><div class="service-row-actions"><button class="btn small ghost" data-service-run="${item.id}" ${!item.enabled ? "disabled" : ""}>测试</button>${item.can_edit ? `<button class="btn small ghost" data-service-edit="${item.id}">配置</button><button class="btn small danger" data-service-delete="${item.id}">删除</button>` : '<span class="hint">可使用</span>'}</div></td></tr>`).join("")}</tbody></table></div>`;
  }

  function renderRuns() {
    const runs = state.runs.filter(r => r.service_name.toLowerCase().includes(state.query.toLowerCase()));
    if (!runs.length) return empty("暂无执行记录", "服务测试与智能体调用完成后，可在这里查看当前账户最近 100 条记录。");
    const statuses = {completed: "已完成", failed: "失败", cancelled: "已取消", blocked: "已拦截", pending_approval: "待审批"};
    return `<div class="resource-table-wrap"><table class="resource-table"><thead><tr><th>服务</th><th>执行状态</th><th>耗时</th><th>时间</th><th>结果</th></tr></thead><tbody>${runs.map(r => `<tr><td>${e(r.service_name)}</td><td><span class="status ${e(r.status)}">${e(statuses[r.status] || r.status)}</span></td><td>${r.duration_ms} ms</td><td>${e(time(r.created_at))}</td><td><button class="btn small ghost" data-service-result="${r.id}">查看结果</button></td></tr>`).join("")}</tbody></table></div>`;
  }

  async function refresh() {
    const ticket = ++state.generation;
    $("services-content").innerHTML = '<div class="empty-state" role="status">正在加载服务…</div>';
    try {
      const results = await Promise.all([api("/api/v1/services"), api("/api/v1/services/targets"), api("/api/v1/services/runs")]);
      if (!state.active || ticket !== state.generation) return;
      [state.items, {agents: state.agents, can_program: state.canProgram}, state.runs] = results;
      render();
    } catch (error) {
      if (!state.active || ticket !== state.generation) return;
      $("services-content").innerHTML = `<div class="empty-state"><h3>服务加载失败</h3><p>${e(error.message)}</p><button class="btn ghost" id="services-retry">重试</button></div>`;
      $("services-retry").onclick = refresh;
    }
  }

  function modal(title, body, action = "保存") {
    document.querySelectorAll("dialog.service-dialog").forEach(d => d.remove());
    const dialog = document.createElement("dialog");
    dialog.className = "service-dialog";
    dialog.setAttribute("aria-label", title);
    dialog.innerHTML = `<form method="dialog"><div class="service-dialog-heading"><h3>${title}</h3><button class="icon-btn" value="cancel" aria-label="关闭">${icon("close", 18)}</button></div></form>${body}<p class="error" data-service-error role="alert"></p><div class="dialog-actions"><button class="btn ghost" data-service-cancel>取消</button><button class="btn" data-service-submit>${action}</button></div>`;
    document.body.append(dialog);
    dialog.querySelector("[data-service-cancel]").onclick = () => dialog.close();
    dialog.addEventListener("close", () => dialog.remove());
    dialog.showModal();
    return dialog;
  }

  const fieldRow = (field = {}) => `<div class="service-input-row"><input data-input-name aria-label="参数名称" placeholder="参数名称，例如 text" value="${e(field.name)}"><select data-input-type aria-label="参数类型">${["string", "number", "boolean", "object", "array"].map(type => `<option ${type === field.type ? "selected" : ""}>${type}</option>`).join("")}</select><label><input type="checkbox" data-input-required ${field.required ? "checked" : ""}>必填</label><button type="button" class="icon-btn danger" data-remove-input aria-label="删除参数">${icon("close", 16)}</button></div>`;

  function editor(item = null) {
    const config = item?.config || {};
    const dialog = modal(item ? "配置服务" : "创建服务", `<div class="service-form-grid">
      <label>名称 *<input id="service-name" maxlength="128" value="${e(item?.name)}" placeholder="例如：文档分析"></label>
      <label>服务类型<select id="service-kind"><option value="http">网络服务 · HTTP</option>${state.canProgram ? '<option value="program">编程服务 · 本地程序</option>' : ""}</select></label>
      <label class="wide">用途说明<textarea id="service-description" placeholder="描述功能、使用场景和返回结果">${e(item?.description)}</textarea></label>
      <div class="wide service-config-section" data-kind="http"><h4>接口连接</h4><div class="service-form-grid"><label>请求方法<select id="service-method"><option>POST</option><option>GET</option></select></label><label>服务地址 *<input id="service-url" type="url" placeholder="https://api.example.com/analyze" value="${e(config.url)}"></label><label class="wide">请求头（JSON）<textarea class="code-area" id="service-headers">${e(JSON.stringify(config.headers || {}, null, 2))}</textarea><small>例如 {"Authorization":"Bearer …"}；已有值脱敏显示，保留 ******** 可继续使用。</small></label></div></div>
      <div class="wide service-config-section" data-kind="program"><h4>程序启动</h4><p class="service-help">程序从标准输入读取一行 JSON，并向标准输出写入一个 JSON 结果。通过解释器运行脚本。</p><div class="service-form-grid"><label>可执行程序 *<input id="service-command" placeholder="python.exe" value="${e(config.command)}"></label><label>工作目录<input id="service-cwd" placeholder="可选，使用绝对路径" value="${e(config.cwd)}"></label><label>启动参数（JSON 数组）<textarea class="code-area" id="service-args">${e(JSON.stringify(config.args || [], null, 2))}</textarea></label><label>环境变量（JSON）<textarea class="code-area" id="service-env">${e(JSON.stringify(config.env || {}, null, 2))}</textarea></label></div></div>
      <label>超时（秒）<input id="service-timeout" type="number" min="1" max="120" value="${config.timeout_seconds || 30}"></label>
      <div class="wide service-config-section"><h4>输入参数</h4><p class="service-help">定义字段后，测试面板会生成相应表单，并校验智能体传入的数据。留空可接收任意 JSON 对象。</p><div id="service-input-fields">${(item?.input_fields || []).map(fieldRow).join("")}</div><button type="button" class="btn small ghost" id="service-add-input">添加参数</button></div>
      <div class="wide service-config-section"><h4>绑定现有智能体</h4><div class="choice-grid service-agent-choices">${state.agents.map(a => `<label class="choice"><input type="checkbox" data-service-agent value="${a.id}" ${item?.agent_ids.includes(a.id) ? "checked" : ""}>${e(a.name)}</label>`).join("") || '<span class="hint">暂无可管理的智能体；可先保存服务。</span>'}</div><small>智能体需同时获分配 service_list、service_call 内置工具，才可发现并调用绑定的服务。</small></div>
      <div class="wide switch-row"><label><input id="service-enabled" type="checkbox" ${item?.enabled !== false ? "checked" : ""}>启用服务</label><label><input id="service-public" type="checkbox" ${item?.is_public ? "checked" : ""}>共享给其他获授权用户</label></div></div>`);
    $("service-kind").value = item?.kind || (state.kind === "program" && state.canProgram ? "program" : "http");
    $("service-method").value = config.method || "POST";
    const changeKind = () => dialog.querySelectorAll("[data-kind]").forEach(section => section.hidden = section.dataset.kind !== $("service-kind").value);
    $("service-kind").onchange = changeKind; changeKind();
    $("service-input-fields").onclick = event => event.target.closest("[data-remove-input]")?.closest(".service-input-row").remove();
    $("service-add-input").onclick = () => $("service-input-fields").insertAdjacentHTML("beforeend", fieldRow());
    dialog.querySelector("[data-service-submit]").onclick = async event => {
      const button = event.currentTarget;
      dialog.querySelector("[data-service-error]").textContent = "";
      try {
        const kind = $("service-kind").value;
        const name = $("service-name").value.trim();
        if (!name) throw new Error("请填写服务名称");
        const connection = {timeout_seconds: Number($("service-timeout").value)};
        if (kind === "http") Object.assign(connection, {url: $("service-url").value.trim(), method: $("service-method").value, headers: parse($("service-headers").value, "请求头", "object")});
        else Object.assign(connection, {command: $("service-command").value.trim(), cwd: $("service-cwd").value.trim(), args: parse($("service-args").value, "启动参数", "array"), env: parse($("service-env").value, "环境变量", "object")});
        const body = {name, description: $("service-description").value, kind, config: connection,
          input_fields: [...dialog.querySelectorAll(".service-input-row")].map(row => ({name: row.querySelector("[data-input-name]").value.trim(), type: row.querySelector("[data-input-type]").value, required: row.querySelector("[data-input-required]").checked})),
          agent_ids: [...dialog.querySelectorAll("[data-service-agent]:checked")].map(input => Number(input.value)),
          enabled: $("service-enabled").checked, is_public: $("service-public").checked, ...(item ? {revision: item.revision} : {})};
        button.disabled = true;
        await api(`/api/v1/services${item ? `/${item.id}` : ""}`, {method: item ? "PUT" : "POST", json: body});
        dialog.close(); showToast("服务已保存"); if (state.active) await refresh();
      } catch (error) { if (dialog.isConnected) dialog.querySelector("[data-service-error]").textContent = error.message; }
      finally { if (button.isConnected) button.disabled = false; }
    };
  }

  function parse(raw, name, type) {
    let result;
    try { result = JSON.parse(raw || (type === "array" ? "[]" : "{}")); } catch (_) { throw new Error(`${name}不是有效 JSON`); }
    if (type === "array" ? !Array.isArray(result) : !result || typeof result !== "object" || Array.isArray(result)) throw new Error(`${name}必须是 JSON ${type === "array" ? "数组" : "对象"}`);
    return result;
  }

  function playground(item) {
    if (!item?.enabled) return;
    const fields = item.input_fields || [];
    const dialog = modal("测试服务", `<p><strong>${e(item.name)}</strong> <span class="tag">${label(item.kind)}</span></p><p class="service-help">${e(item.description || "填写输入并查看执行结果。")}</p><div class="service-form-grid">${fields.length ? fields.map((f, index) => `<label class="wide">${e(f.name)} ${f.required ? "*" : ""}<small>${e(f.type)}</small>${f.type === "boolean" ? `<select data-run-field="${index}"><option value="">不传入</option><option value="true">true</option><option value="false">false</option></select>` : ["array", "object"].includes(f.type) ? `<textarea data-run-field="${index}" placeholder="${f.type === "array" ? "[]" : "{}"}"></textarea>` : `<input data-run-field="${index}" type="${f.type === "number" ? "number" : "text"}" step="any">`}</label>`).join("") : '<label class="wide">输入（JSON 对象）<textarea id="service-run-json" class="code-area">{}</textarea></label>'}</div><label class="service-run-confirm"><input id="service-confirm-run" type="checkbox">确认执行此服务（可能写入数据或调用外部接口）</label><div id="service-run-state" role="status" aria-live="polite"></div><pre id="service-run-output" class="service-output" hidden></pre>`, "运行服务");
    dialog.querySelector("[data-service-submit]").onclick = async event => {
      const button = event.currentTarget;
      const errorBox = dialog.querySelector("[data-service-error]");
      errorBox.textContent = "";
      try {
        if (!$("service-confirm-run").checked) throw new Error("请先确认执行此服务");
        const input = fields.length ? {} : parse($("service-run-json").value, "输入", "object");
        for (const element of dialog.querySelectorAll("[data-run-field]")) {
          const f = fields[Number(element.dataset.runField)], raw = element.value;
          if (!raw && !f.required) continue;
          if (!raw && f.required) throw new Error(`${f.name}必填`);
          input[f.name] = f.type === "boolean" ? raw === "true" : f.type === "number" ? Number(raw) : ["object", "array"].includes(f.type) ? parse(raw, f.name, f.type) : raw;
        }
        button.disabled = true; button.textContent = "正在运行…";
        $("service-run-state").textContent = "正在执行，关闭面板不会撤销已发出的调用。";
        const start = performance.now();
        const response = await api(`/api/v1/services/${item.id}/run`, {method: "POST", json: {input}});
        if (!dialog.isConnected) return;
        $("service-run-state").textContent = `执行完成 · ${Math.round(performance.now() - start)} ms`;
        $("service-run-output").hidden = false;
        $("service-run-output").textContent = JSON.stringify(response.result, null, 2);
      } catch (error) { if (dialog.isConnected) { errorBox.textContent = error.message; $("service-run-state").textContent = "执行未完成"; } }
      finally { if (button.isConnected) { button.disabled = false; button.textContent = "再次运行"; } }
    };
  }

  function remove(item) {
    const dialog = modal("删除服务", `<p>删除 ${e(item.name)} 后，智能体将无法继续调用此服务。已有执行记录会保留。</p>`, "删除");
    dialog.querySelector("[data-service-submit]").onclick = async event => {
      const button = event.currentTarget; button.disabled = true;
      try { await api(`/api/v1/services/${item.id}`, {method: "DELETE"}); dialog.close(); if (state.active) await refresh(); }
      catch (error) { if (dialog.isConnected) dialog.querySelector("[data-service-error]").textContent = error.message; }
      finally { if (button.isConnected) button.disabled = false; }
    };
  }

  window.ServicesAdmin = {
    async load({kind = ""} = {}) { state.active = true; state.kind = kind; state.query = ""; if (kind) state.tab = "custom"; await refresh(); },
    leave() { state.active = false; ++state.generation; document.querySelectorAll("dialog.service-dialog").forEach(dialog => dialog.close()); },
  };
})();
