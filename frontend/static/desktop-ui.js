/* Desktop-only chrome. The browser edition keeps its existing layout and behavior. */
(() => {
  "use strict";
  const desktop = window.harnessDesktop;
  if (!desktop || typeof desktop.getInfo !== "function") return;
  document.documentElement.dataset.desktop = "true";

  function button(label, action, className = "") {
    const item = document.createElement("button");
    item.type = "button";
    item.textContent = label;
    item.className = className;
    item.addEventListener("click", async () => {
      try { await action(); }
      catch { showNotice("操作未完成，请稍后重试或从应用菜单查看日志。"); }
    });
    return item;
  }

  function showNotice(text) {
    let notice = document.getElementById("desktop-notice");
    if (!notice) {
      notice = document.createElement("div");
      notice.id = "desktop-notice";
      notice.setAttribute("role", "status");
      document.body.append(notice);
    }
    notice.textContent = text;
    notice.hidden = false;
    window.setTimeout(() => { notice.hidden = true; }, 6000);
  }

  function install() {
    const toolbar = document.createElement("header");
    toolbar.className = "desktop-titlebar";
    toolbar.setAttribute("aria-label", "桌面应用导航");
    const brand = document.createElement("a");
    brand.href = "/";
    brand.className = "desktop-app-name";
    brand.textContent = "Harness Agent";
    toolbar.append(brand);
    const location = document.createElement("span");
    location.className = "desktop-location";
    location.textContent = document.body.classList.contains("admin-page") ? "设置" : "工作区";
    toolbar.append(location);

    const actions = document.createElement("div");
    actions.className = "desktop-titlebar-actions";
    const status = document.createElement("span");
    status.className = "desktop-connection";
    status.textContent = "本地";
    status.title = "Agent 服务在本机运行";
    actions.append(status);

    const details = document.createElement("details");
    details.className = "desktop-app-menu";
    const summary = document.createElement("summary");
    summary.textContent = "应用";
    summary.setAttribute("aria-label", "应用菜单");
    details.append(summary);
    const menu = document.createElement("div");
    menu.className = "desktop-menu-items";
    const entries = [
      ["首次登录信息", () => desktop.showInitialLogin()],
      ["打开数据目录", () => desktop.openDataDirectory()],
      ["查看运行日志", () => desktop.openLogs()],
      ["重启本地服务", () => desktop.restartService()],
    ];
    entries.forEach(([label, action]) => menu.append(button(label, () => { details.open = false; return action(); })));
    details.append(menu);
    actions.append(details);
    toolbar.append(actions);
    document.body.prepend(toolbar);
    document.addEventListener("click", event => { if (!details.contains(event.target)) details.open = false; });
    document.addEventListener("keydown", event => { if (event.key === "Escape") details.open = false; });

    if (document.body.classList.contains("login-page")) {
      const help = document.createElement("div");
      help.className = "desktop-login-help";
      help.append(button("首次使用？查看本机登录信息", () => desktop.showInitialLogin()));
      const detail = document.createElement("p");
      detail.textContent = "登录后配置模型，即可开始你的第一个任务。";
      help.append(detail);
      document.querySelector(".login-card")?.append(help);
    }

    if (document.body.classList.contains("chat-page")) {
      const sidebar = document.getElementById("sidebar");
      const shortcuts = document.createElement("nav");
      shortcuts.className = "desktop-workspace-shortcuts";
      shortcuts.setAttribute("aria-label", "工作区功能");
      const links = [
        ["智能体", "agents", "blocks"],
        ["自动任务", "schedules", "calendarClock"],
        ["技能与工具", "skills", "sparkles"],
      ];
      links.forEach(([label, section, glyph]) => {
        const link = document.createElement("a");
        link.href = `/admin#${section}`;
        link.dataset.module = section;
        const iconElement = document.createElement("span");
        iconElement.dataset.icon = glyph;
        link.append(iconElement, document.createTextNode(label));
        link.hidden = true;
        shortcuts.append(link);
      });
      const search = document.querySelector(".history-search");
      if (sidebar && search) sidebar.insertBefore(shortcuts, search);
      if (typeof hydrateIcons === "function") hydrateIcons(shortcuts);
      if (typeof Auth !== "undefined") {
        Auth.ensureSession().then(() => {
          shortcuts.querySelectorAll("a").forEach(link => { link.hidden = !Auth.canModule(link.dataset.module); });
        }).catch(() => {});
      }
      const newButton = document.getElementById("new-chat-btn");
      newButton?.setAttribute("title", "新建任务 · Ctrl+N");
      const label = newButton?.querySelector("span:not(.nav-icon)");
      if (label) label.textContent = "新建任务";
      const key = newButton?.querySelector("kbd");
      if (key) key.textContent = "Ctrl N";
      const query = document.getElementById("query");
      if (query) query.placeholder = "描述任务，或用 @ 引用知识和工具";
    }

    let lastTheme = null;
    const syncTheme = () => {
      const theme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.desktopAccent = localStorage.getItem("gca_theme_color") || "default";
      if (theme !== lastTheme) {
        lastTheme = theme;
        desktop.setTheme(theme).catch(() => {});
      }
    };
    syncTheme();
    new MutationObserver(syncTheme).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme", "style"]});
    window.addEventListener("harness-desktop-theme", async event => {
      const theme = event.detail?.theme;
      if (!["light", "dark"].includes(theme)) return;
      if (typeof Theme !== "undefined") Theme.apply(theme);
      if (typeof setAccountTheme === "function") { await setAccountTheme(theme); return; }
      if (typeof Auth === "undefined" || !Auth.username()) return;
      try {
        const latest = await api("/api/v1/users/me/preferences");
        const saved = await api("/api/v1/users/me/preferences", {method: "PATCH", json: {theme, revision: latest.revision}});
        Theme.usePreferences(saved);
        window.dispatchEvent(new CustomEvent("harness-desktop-preferences", {detail: saved}));
      } catch { showNotice("当前外观已切换，账户偏好未保存，请稍后在设置中重试。"); }
    });
    window.addEventListener("harness-desktop-action", event => {
      if (event.detail?.action === "new-task") document.getElementById("new-chat-btn")?.click();
    });
    desktop.getInfo().then(info => { status.title = `${info.name} ${info.version} · 本机运行`; }).catch(() => {});
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, {once: true});
  else install();
})();
