/* Account-owned settings. Shared admin shell; each API retains its owner/ACL checks. */
window.PersonalSettings = (() => {
  let request = 0;
  let preferenceCleanup = () => {};
  const host = () => document.getElementById("personal-settings-content");
  const el = id => document.getElementById(id);
  const esc = value => escapeHtml(value ?? "");
  const selected = (a, b) => String(a ?? "") === String(b ?? "") ? " selected" : "";
  const option = (value, label, current) => `<option value="${esc(value)}"${selected(value, current)}>${esc(label)}</option>`;
  const heading = (title, detail, actions = "") => `<header class="personal-heading"><div><h1>${esc(title)}</h1><p>${esc(detail)}</p></div>${actions}</header>`;
  const error = message => `<div class="personal-error" role="alert">${esc(message)}</div>`;
  const field = (label, id, control, hint = "") => `<div class="personal-field"><label for="${id}">${esc(label)}</label><div>${control}${hint ? `<small>${esc(hint)}</small>` : ""}</div></div>`;
  const actions = label => `<div id="personal-save-error" class="personal-error" role="alert"></div><div class="personal-actions"><button type="submit" class="btn" id="personal-save">${label}</button></div>`;
  function active(ticket) { return request === ticket; }
  function routeId() { try { return decodeURIComponent(location.hash.split("/").slice(1).join("/")); } catch (_) { return ""; } }
  function go(page, id = "") {
    const hash = `#${page}${id ? `/${encodeURIComponent(id)}` : ""}`;
    if (location.hash === hash) load(page);
    else location.hash = hash;
  }
  async function submit(form, ticket, task) {
    const button = form.querySelector('[type="submit"]');
    const box = form.querySelector("#personal-save-error");
    button.disabled = true;
    box.textContent = "";
    try { await task(); }
    catch (e) { if (active(ticket)) box.textContent = e.message; }
    finally { if (active(ticket)) button.disabled = false; }
  }

  const validColor = value => /^#[0-9a-f]{6}$/i.test(value);
  const colorChoices = customColor => [
    ...Theme.palette.filter(item => item.id !== "custom"),
    {id: "custom", label: "自定义", color: customColor},
  ];
  function accentField(value) {
    const choices = colorChoices(value.custom_color);
    const choice = choices.find(item => item.id === value.theme_color) || choices[0];
    return `<div class="personal-field personal-accent-field"><span class="personal-field-label" id="personal-accent-label">主题色</span><div>
      <details class="personal-color-picker" id="personal-color-picker">
        <summary id="personal-color-summary" aria-labelledby="personal-accent-label personal-color-name" aria-describedby="personal-color-hint"><span class="personal-color-dot" id="personal-color-dot" style="background:${esc(choice.color)}" aria-hidden="true"></span><span id="personal-color-name">${esc(choice.label)}</span><span class="personal-color-chevron" aria-hidden="true">⌄</span></summary>
        <fieldset class="personal-color-options" id="personal-color-options"><legend class="sr-only">选择主题色</legend>${choices.map(item => `<label class="personal-color-option"><input type="radio" name="personal-theme-color" value="${esc(item.id)}" ${item.id === choice.id ? "checked" : ""}><span class="personal-color-dot" ${item.id === "custom" ? 'id="personal-custom-dot"' : ""} style="background:${esc(item.color)}" aria-hidden="true"></span><span>${esc(item.label)}</span><span class="personal-color-check" aria-hidden="true">✓</span></label>`).join("")}</fieldset>
      </details>
      <div class="personal-custom-color" id="personal-custom-color" ${choice.id === "custom" ? "" : "hidden"}><label for="personal-color-input">自定义颜色</label><div class="personal-custom-controls"><input type="color" id="personal-color-input" value="${esc(value.custom_color)}" aria-label="选取自定义颜色"><input type="text" id="personal-color-hex" value="${esc(value.custom_color)}" maxlength="7" spellcheck="false" autocomplete="off" aria-label="自定义颜色 HEX 值" aria-describedby="personal-custom-hint personal-color-error"></div><small id="personal-custom-hint">使用 #RRGGBB 格式，例如 #8b5cf6。</small><p class="personal-error" id="personal-color-error" role="alert"></p></div>
      <small id="personal-color-hint">选择后可在下方预览，保存偏好后应用到整个界面。颜色会随外观适配，确保文字清晰。</small>
      <div class="personal-theme-preview" id="personal-theme-preview" role="group" aria-label="主题色效果预览"><span class="personal-preview-label">效果预览</span><div class="personal-preview-content"><span class="personal-preview-button">开始任务</span><span class="personal-preview-selected"><span aria-hidden="true">✓</span> 已选项目</span></div></div>
    </div></div>`;
  }
  function bindAccentPreview() {
    const picker = el("personal-color-picker");
    const summary = el("personal-color-summary");
    const menu = el("personal-color-options");
    const radios = [...picker.querySelectorAll('[name="personal-theme-color"]')];
    const hex = el("personal-color-hex");
    const input = el("personal-color-input");
    const preview = el("personal-theme-preview");
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const current = () => radios.find(radio => radio.checked)?.value || "default";
    const close = () => { picker.open = false; summary.focus(); };
    const clearError = () => { hex.removeAttribute("aria-invalid"); el("personal-color-error").textContent = ""; };
    function placeMenu() {
      if (!picker.open) return;
      const rect = summary.getBoundingClientRect();
      const viewport = window.visualViewport;
      const viewportTop = viewport?.offsetTop || 0;
      const top = Math.max(viewportTop, document.querySelector(".admin-page > .topbar")?.getBoundingClientRect().bottom || 0);
      const bottom = viewportTop + (viewport?.height || window.innerHeight);
      const above = Math.max(0, rect.top - top - 14);
      const below = Math.max(0, bottom - rect.bottom - 14);
      const openAbove = below < Math.min(360, menu.scrollHeight) && above > below;
      menu.style.top = openAbove ? "auto" : "calc(100% + 6px)";
      menu.style.bottom = openAbove ? "calc(100% + 6px)" : "auto";
      menu.style.maxHeight = `${Math.min(360, openAbove ? above : below)}px`;
    }
    function revealOption(radio) {
      if (!picker.open || !radio) return;
      const option = radio.parentElement;
      const bottom = option.offsetTop + option.offsetHeight;
      if (option.offsetTop < menu.scrollTop) menu.scrollTop = option.offsetTop;
      else if (bottom > menu.scrollTop + menu.clientHeight) menu.scrollTop = bottom - menu.clientHeight;
    }
    function updatePreview() {
      const customColor = validColor(hex.value) ? hex.value : input.value;
      const choice = colorChoices(customColor).find(item => item.id === current());
      el("personal-color-name").textContent = choice.label;
      el("personal-color-dot").style.background = choice.color;
      el("personal-custom-dot").style.background = customColor;
      el("personal-custom-color").hidden = current() !== "custom";
      const appearance = el("personal-theme").value;
      const dark = appearance === "dark" || (appearance === "system" && media.matches);
      preview.dataset.previewTheme = dark ? "dark" : "light";
      Object.entries(Theme.colors(current(), customColor, dark)).forEach(([name, color]) => preview.style.setProperty(name, color));
    }
    radios.forEach(radio => {
      radio.onchange = () => { clearError(); updatePreview(); };
      radio.onclick = event => { if (event.detail > 0) close(); };
      radio.onfocus = () => revealOption(radio);
    });
    picker.ontoggle = () => { placeMenu(); revealOption(radios.find(radio => radio.checked)); };
    summary.onkeydown = event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault(); picker.open = true; placeMenu(); radios.find(radio => radio.checked)?.focus();
      }
    };
    picker.onkeydown = event => {
      if (event.key === "Escape" || (event.key === "Enter" && radios.includes(event.target))) {
        event.preventDefault(); event.stopPropagation(); close();
      }
    };
    input.oninput = () => { hex.value = input.value; clearError(); updatePreview(); };
    hex.oninput = () => {
      if (validColor(hex.value)) { input.value = hex.value; clearError(); updatePreview(); }
    };
    el("personal-theme").onchange = updatePreview;
    const outside = event => { if (!picker.contains(event.target)) picker.open = false; };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("scroll", placeMenu, true);
    window.addEventListener("resize", placeMenu);
    window.visualViewport?.addEventListener("resize", placeMenu);
    window.visualViewport?.addEventListener("scroll", placeMenu);
    media.addEventListener("change", updatePreview);
    preferenceCleanup = () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("scroll", placeMenu, true);
      window.removeEventListener("resize", placeMenu);
      window.visualViewport?.removeEventListener("resize", placeMenu);
      window.visualViewport?.removeEventListener("scroll", placeMenu);
      media.removeEventListener("change", updatePreview);
    };
    updatePreview();
    return () => {
      if (current() === "custom" && !validColor(hex.value)) {
        hex.setAttribute("aria-invalid", "true");
        el("personal-color-error").textContent = "请输入有效的 HEX 颜色，格式为 #RRGGBB。";
        hex.focus();
        return null;
      }
      clearError();
      return {theme_color: current(), custom_color: (validColor(hex.value) ? hex.value : input.value).toLowerCase()};
    };
  }

  async function preferences(ticket) {
    const [value, agents] = await Promise.all([api("/api/v1/users/me/preferences"), api("/api/v1/agents/enabled")]);
    if (!active(ticket)) return;
    value.theme_color ||= "default";
    value.custom_color ||= "#8b5cf6";
    Theme.setAccent(value.theme_color, value.custom_color);
    Theme.apply(value.theme);
    localStorage.setItem("gca_theme", value.theme);
    host().innerHTML = heading("个人设置", "偏好保存在当前账户，新的任务使用最新设置。") +
      `<form id="personal-form" class="personal-form"><h2>外观与对话</h2>` +
      field("外观", "personal-theme", `<select id="personal-theme">${option("system", "跟随系统", value.theme)}${option("light", "浅色", value.theme)}${option("dark", "深色", value.theme)}</select>`) +
      accentField(value) +
      field("默认智能体", "personal-agent", `<select id="personal-agent">${option("", "使用项目或平台默认值", value.default_agent_id)}${agents.map(a => option(a.id, a.name, value.default_agent_id)).join("")}</select>`, "模型、路由和规划策略由智能体配置统一管理。") +
      field("对话排序", "personal-sort", `<select id="personal-sort">${option("priority", "置顶优先", value.recent_sort)}${option("updated", "最近更新", value.recent_sort)}</select>`) +
      `<h2>任务执行</h2>` +
      field("审批偏好", "personal-approval", `<select id="personal-approval">${option("ask", "请求批准", value.approval_policy)}${option("auto", "低风险操作自动批准", value.approval_policy)}${Auth.role() === "root" ? option("full_access", "完全访问权限", value.approval_policy) : ""}</select>`, "只影响是否询问；工具授权、护栏和工作区限制仍然生效。") +
      actions("保存偏好") + `</form><section class="personal-security"><div><h2>账户安全</h2><p>修改密码后需要重新登录。</p></div><button class="btn ghost" id="personal-password">修改密码</button></section>`;
    el("personal-password").onclick = openChangePasswordDialog;
    const accentPayload = bindAccentPreview();
    const desktopPreferences = event => {
      const saved = event.detail;
      if (!active(ticket) || !saved || !Number.isInteger(saved.revision)) return;
      value.revision = saved.revision;
      const appearance = el("personal-theme");
      if (appearance && ["light", "dark", "system"].includes(saved.theme)) {
        appearance.value = saved.theme;
        appearance.dispatchEvent(new Event("change"));
      }
    };
    window.addEventListener("harness-desktop-preferences", desktopPreferences);
    const cleanupAccent = preferenceCleanup;
    preferenceCleanup = () => {
      cleanupAccent();
      window.removeEventListener("harness-desktop-preferences", desktopPreferences);
    };
    el("personal-form").onsubmit = event => {
      event.preventDefault();
      const form = event.currentTarget;
      const accent = accentPayload();
      if (!accent) return;
      return submit(form, ticket, async () => {
        const updated = await api("/api/v1/users/me/preferences", {method: "PATCH", json: {
          revision: value.revision,
          ...accent,
          theme: el("personal-theme").value,
          default_agent_id: el("personal-agent").value ? Number(el("personal-agent").value) : null,
          recent_sort: el("personal-sort").value,
          approval_policy: el("personal-approval").value,
        }});
        if (!active(ticket)) return;
        value.revision = updated.revision;
        localStorage.setItem("gca_theme", updated.theme);
        Theme.setAccent(updated.theme_color, updated.custom_color);
        Theme.apply(updated.theme);
        showToast("偏好已保存，新任务将使用这些设置");
      });
    };
  }

  async function projects(ticket) {
    const [rows, agents, datasets] = await Promise.all([
      api("/api/v1/projects"), api("/api/v1/agents/enabled"), api("/api/v1/knowledge/chat-datasets"),
    ]);
    if (!active(ticket)) return;
    const id = routeId();
    const project = rows.find(row => String(row.id) === id);
    if (id && id !== "new" && !project) throw new Error("项目不存在、已归档或不属于当前账户");
    if (!id) {
      host().innerHTML = heading("项目设置", "集中管理项目背景、默认智能体与知识库。", '<button class="btn" id="personal-new-project">新建项目</button>') +
        `<div class="personal-list">${rows.map(row => `<article class="personal-list-row"><div><h3>${esc(row.name)}${row.default ? '<span class="badge">默认</span>' : ""}</h3><p>${esc(row.description || "尚未填写项目说明")}</p></div><button type="button" class="btn ghost" data-personal-project="${row.id}">设置</button></article>`).join("") || '<p class="personal-empty">还没有项目，创建一个项目来整理相关任务。</p>'}</div>`;
      el("personal-new-project").onclick = () => go("projects", "new");
      host().querySelectorAll("[data-personal-project]").forEach(button => button.onclick = () => go("projects", button.dataset.personalProject));
      return;
    }
    const current = project || {};
    const unavailableAgent = current.default_agent_id != null && !agents.some(a => a.id === current.default_agent_id);
    const known = new Set(datasets.map(d => d.key));
    const choices = [...datasets, ...(current.dataset_ids || []).filter(key => !known.has(key)).map(key => ({key, name: `${key}（当前不可用，可取消绑定）`}))];
    host().innerHTML = heading(project ? project.name : "新建项目", "项目背景作为任务参考，不改变智能体指令与权限。", '<button class="btn ghost" id="personal-back">返回项目</button>') +
      `<form id="personal-form" class="personal-form">` +
      field("项目名称", "personal-project-name", `<input id="personal-project-name" required maxlength="80" value="${esc(current.name)}">`) +
      field("项目说明", "personal-project-description", `<input id="personal-project-description" maxlength="500" value="${esc(current.description)}">`) +
      field("项目背景", "personal-project-context", `<textarea id="personal-project-context" maxlength="8000" rows="6" placeholder="项目目标、相关背景与交付要求">${esc(current.context_text)}</textarea>`) +
      field("默认智能体", "personal-project-agent", `<select id="personal-project-agent">${option("", "使用当前选择", current.default_agent_id)}${unavailableAgent ? option(current.default_agent_id, "当前智能体不可用，请更换或取消绑定", current.default_agent_id) : ""}${agents.map(a => option(a.id, a.name, current.default_agent_id)).join("")}</select>`) +
      `<fieldset class="personal-checks"><legend>默认知识库</legend>${choices.map(d => `<label><input type="checkbox" name="personal-dataset" value="${esc(d.key)}" ${(current.dataset_ids || []).includes(d.key) ? "checked" : ""}>${esc(d.name)}</label>`).join("") || '<p class="hint">没有可用知识库</p>'}</fieldset>` +
      `<label class="personal-check"><input type="checkbox" id="personal-project-default" ${current.default ? "checked" : ""}>设为默认项目</label>` + actions("保存项目") + `</form>`;
    el("personal-back").onclick = () => go("projects");
    el("personal-form").onsubmit = event => {
      event.preventDefault();
      const form = event.currentTarget;
      submit(form, ticket, async () => {
        const payload = {
          name: el("personal-project-name").value.trim(), description: el("personal-project-description").value.trim(),
          context_text: el("personal-project-context").value.trim(),
          default_agent_id: el("personal-project-agent").value ? Number(el("personal-project-agent").value) : null,
          dataset_ids: [...form.querySelectorAll('[name="personal-dataset"]:checked')].map(input => input.value),
          default: el("personal-project-default").checked,
        };
        if (!payload.name) throw new Error("请输入项目名称");
        if (unavailableAgent && payload.default_agent_id === current.default_agent_id) {
          el("personal-project-agent").focus();
          throw new Error("原默认智能体已不可用，请明确选择其他智能体或取消绑定");
        }
        const result = await api(project ? `/api/v1/projects/${project.id}` : "/api/v1/projects", {method: project ? "PATCH" : "POST", json: payload});
        if (!active(ticket)) return;
        showToast("项目已保存");
        go("projects", result.id);
      });
    };
  }

  async function memory(ticket) {
    const id = routeId();
    if (!id) {
      const turns = await api("/api/v1/chat/turns");
      if (!active(ticket)) return;
      const threads = new Map();
      turns.forEach(turn => { const key = turn.thread_id || turn.session_id; if (key && !threads.has(key)) threads.set(key, turn); });
      host().innerHTML = heading("对话记忆", "管理最近 100 轮任务所涉及的对话；只作用于自己的记忆来源与召回。", '<button class="btn ghost" id="personal-export-memory">导出我的记忆</button>') +
        `<div class="personal-list">${[...threads.entries()].map(([key, turn]) => `<article class="personal-list-row"><div><h3>${esc(turn.title || turn.query || "未命名对话")}</h3></div><button class="btn ghost" data-personal-memory="${esc(key)}">管理记忆</button></article>`).join("") || '<p class="personal-empty">还没有可管理的对话记忆。</p>'}</div>`;
      host().querySelectorAll("[data-personal-memory]").forEach(button => button.onclick = () => go("conversation-memory", button.dataset.personalMemory));
      el("personal-export-memory").onclick = async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const data = await api("/api/v1/chat/memory/export");
          if (!active(ticket)) return;
          const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json;charset=utf-8"}));
          const link = document.createElement("a"); link.href = url; link.download = "my-memory.json"; link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (e) { if (active(ticket)) showToast(e.message); }
        finally { if (active(ticket)) button.disabled = false; }
      };
      return;
    }
    const value = await api(`/api/v1/chat/threads/${encodeURIComponent(id)}/memory`);
    if (!active(ticket)) return;
    host().innerHTML = heading("对话记忆", "关闭来源资格会停止未来召回，原始对话和审计记录仍然保留。", '<button class="btn ghost" id="personal-back">返回对话列表</button>') +
      `<form id="personal-form" class="personal-form"><label class="personal-check"><input id="personal-memory-recall" type="checkbox" ${value.enabled ? "checked" : ""}>允许本对话召回记忆</label>` +
      `<label class="personal-check"><input id="personal-memory-source" type="checkbox" ${!value.source_excluded ? "checked" : ""}>允许本对话作为其他任务的记忆来源</label>` +
      `<p class="hint">实际召回：${value.effective_enabled ? "开启" : "关闭"}，同时受智能体与运行策略约束。</p>` + actions("保存记忆设置") + `</form>` +
      `<section class="personal-recall"><h2>当前召回来源</h2><p class="hint">候选 ${Number(value.candidate_count || 0)} · 命中 ${Number(value.selected_count || 0)}</p>${(value.sources || []).map(source => `<article><h3>${esc(source.thread_title || "历史对话")}</h3><p>${esc(source.query)}</p></article>`).join("") || '<p class="personal-empty">当前没有命中来源。</p>'}</section>`;
    el("personal-back").onclick = () => go("conversation-memory");
    el("personal-form").onsubmit = event => {
      event.preventDefault();
      submit(event.currentTarget, ticket, async () => {
        await api(`/api/v1/chat/threads/${encodeURIComponent(id)}/memory`, {method: "PATCH", json: {
          enabled: el("personal-memory-recall").checked, source_excluded: !el("personal-memory-source").checked,
        }});
        if (active(ticket)) { showToast("记忆设置已保存"); await load("conversation-memory"); }
      });
    };
  }

  async function load(page) {
    preferenceCleanup();
    preferenceCleanup = () => {};
    const ticket = ++request;
    host().innerHTML = '<p class="personal-empty" role="status">正在加载…</p>';
    try { await ({preferences, projects, "conversation-memory": memory}[page] || preferences)(ticket); }
    catch (e) {
      if (!active(ticket)) return;
      host().innerHTML = error(e.message) + '<button class="btn ghost" id="personal-retry">重新加载</button>';
      el("personal-retry").onclick = () => load(page);
    }
  }
  return {load, leave() { request++; preferenceCleanup(); preferenceCleanup = () => {}; }};
})();
