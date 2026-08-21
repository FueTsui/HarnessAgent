/* 公共工具：令牌管理、请求封装、轻量 Markdown 渲染（本地实现，无外部依赖） */

const Auth = {
  role: () => localStorage.getItem("gca_role"),
  username: () => localStorage.getItem("gca_username"),
  modules() {
    try {
      const value = JSON.parse(localStorage.getItem("gca_modules") || "[]");
      return Array.isArray(value) ? value : [];
    } catch (_) { return []; }
  },
  allModules() {
    if (this.role() === "root") return true;
    if (localStorage.getItem("gca_all_modules") === null) return this.role() === "admin";
    return localStorage.getItem("gca_all_modules") === "1";
  },
  canModule(module) {
    if (this.role() === "root") return true;
    if (this.role() === "admin" && this.allModules()) return true;
    return this.modules().includes(module);
  },
  canAccessSettings() {
    return this.role() === "root" || this.role() === "admin" || this.modules().length > 0;
  },
  save(data) {
    localStorage.setItem("gca_role", data.role);
    localStorage.setItem("gca_username", data.username);
    if (data.all_modules !== undefined) {
      localStorage.setItem("gca_all_modules", data.all_modules ? "1" : "0");
    }
    if (data.modules !== undefined) {
      localStorage.setItem("gca_modules", JSON.stringify(data.modules || []));
    }
  },
  clear() {
    localStorage.removeItem("gca_role");
    localStorage.removeItem("gca_username");
    localStorage.removeItem("gca_all_modules");
    localStorage.removeItem("gca_modules");
  },
  requireLogin() {
    if (this.username()) return;
    fetch("/api/v1/auth/me").then(async response => {
      if (!response.ok) { location.href = "/login"; return; }
      this.save(await response.json());
    }).catch(() => { location.href = "/login"; });
  },
  isAdmin() {
    return ["root", "admin"].includes(this.role());
  },
};

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) {
    Auth.clear();
    location.href = "/login";
    throw new Error("登录已失效");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    const error = new Error(detail);
    error.status = resp.status;
    throw error;
  }
  if (resp.status === 204) return null;
  return resp.json();
}

async function downloadAuthenticated(path, filename) {
  const resp = await fetch(path);
  if (resp.status === 401) {
    Auth.clear();
    location.href = "/login";
    throw new Error("登录已失效");
  }
  if (!resp.ok) {
    let detail = resp.statusText || "下载失败";
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const blob = await resp.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename || "download";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function inlineMd(value) {
  return escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

/* 无依赖 Markdown 渲染器：支持流式更新所需的常见报告与代码结构。 */
function renderMarkdown(md) {
  const lines = String(md || "").split(/\r?\n/);
  const out = [];
  let i = 0, listType = null;
  const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { closeList(); i++; continue; }
    const fence = line.match(/^\s*```([\w.+#-]*)\s*$/);
    if (fence) {
      closeList();
      const language = fence[1] || "text";
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        code.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;
      out.push(
        `<div class="code-block">` +
        `<div class="code-toolbar"><span>${escapeHtml(language)}</span>` +
        `<button type="button" class="code-copy" data-copy-code aria-label="复制代码">${icon("copy", 14)}复制</button></div>` +
        `<pre><code class="language-${escapeHtml(language)}">${escapeHtml(code.join("\n"))}</code></pre></div>`
      );
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)/);
    if (h) { closeList(); const lv = Math.min(h[1].length, 4); out.push(`<h${lv}>${inlineMd(h[2])}</h${lv}>`); i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { closeList(); out.push("<hr>"); i++; continue; }
    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
      closeList();
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const cells = lines[i].trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
        if (!/^[\s:-]+$/.test(cells.join(""))) rows.push(cells);
        i++;
      }
      if (rows.length) {
        let html = "<table><thead><tr>" + rows[0].map(c => `<th>${inlineMd(c)}</th>`).join("") + "</tr></thead><tbody>";
        for (let r = 1; r < rows.length; r++)
          html += "<tr>" + rows[r].map(c => `<td>${inlineMd(c)}</td>`).join("") + "</tr>";
        out.push(html + "</tbody></table>");
      }
      continue;
    }
    const quote = line.match(/^\s*>\s?(.*)/);
    if (quote) { closeList(); out.push(`<blockquote>${inlineMd(quote[1])}</blockquote>`); i++; continue; }
    const ul = line.match(/^\s*[-*]\s+(.*)/);
    if (ul) {
      if (listType !== "ul") { closeList(); out.push("<ul>"); listType = "ul"; }
      out.push(`<li>${inlineMd(ul[1])}</li>`); i++; continue;
    }
    const ol = line.match(/^\s*\d+[.、]\s+(.*)/);
    if (ol) {
      if (listType !== "ol") { closeList(); out.push("<ol>"); listType = "ol"; }
      out.push(`<li>${inlineMd(ol[1])}</li>`); i++; continue;
    }
    closeList();
    out.push(`<p>${inlineMd(line)}</p>`);
    i++;
  }
  closeList();
  return out.join("\n");
}

const ICON_PATHS = {
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  panel: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  chevronDown: '<path d="m7 10 5 5 5-5"/>',
  paperclip: '<path d="m21.4 11.6-8.9 8.9a6 6 0 0 1-8.5-8.5l9.4-9.4a4 4 0 0 1 5.7 5.7l-9.4 9.4a2 2 0 1 1-2.8-2.8l8.8-8.8"/>',
  at: '<circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8"/>',
  context: '<path d="M8 4v16M4 8h8M16 4v16M12 16h8"/>',
  mic: '<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>',
  waveform: '<path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1"/>',
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
  logout: '<path d="M10 17l5-5-5-5M15 12H3"/><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>',
  sparkles: '<path d="m12 3 1.2 3.8L17 8l-3.8 1.2L12 13l-1.2-3.8L7 8l3.8-1.2Z"/><path d="m19 14 .7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7ZM5 15l.8 2.2L8 18l-2.2.8L5 21l-.8-2.2L2 18l2.2-.8Z"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
  external: '<path d="M15 3h6v6M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M15 8l3 3M17 6l3 3"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
  pin: '<path d="M12 17v5M5 4l4 4v5l-3 3h12l-3-3V8l4-4Z"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10h14V9M10 13h4"/>',
  restore: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 15H6L5 6M10 11v6M14 11v6"/>',
  folder: '<path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
  bot: '<rect x="4" y="7" width="16" height="12" rx="3"/><path d="M9 11h.01M15 11h.01M9 15h6M12 3v4M8 3h8"/>',
  flask: '<path d="M9 3h6M10 3v6l-5.5 9.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3"/><path d="M7.5 16h9"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/>',
  plug: '<path d="M12 22v-5M9 8V2M15 8V2M6 8h12v3a6 6 0 0 1-12 0Z"/>',
  package: '<path d="m12 2 9 5-9 5-9-5Z"/><path d="m3 7 9 5 9-5M3 7v10l9 5 9-5V7M12 12v10"/>',
  fileText: '<path d="M6 2h8l4 4v16H6Z"/><path d="M14 2v5h5M9 13h6M9 17h6M9 9h2"/>',
  calendarClock: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/><circle cx="15.5" cy="15.5" r="3.5"/><path d="M15.5 13.5v2.2l1.4.8"/>',
  blocks: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M17.5 14v7M14 17.5h7"/>',
  server: '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01M11 7h6M11 17h6"/>',
  webhook: '<path d="M18 16.5a4 4 0 1 1-1.2-6.8M6 7.5a4 4 0 1 1 1.2 6.8M12 3a4 4 0 0 1 3.5 6H8.5A4 4 0 0 1 12 3Z"/><path d="m15 9 1.8.7M7.2 14.3 9 15M12 9v2"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  activity: '<path d="M3 12h4l2.5-7 5 14 2.5-7h4"/>',
  clipboardList: '<rect x="5" y="4" width="14" height="18" rx="2"/><path d="M9 4V2h6v2M9 10h6M9 14h6M9 18h4"/>',
};

function icon(name, size = 18) {
  const paths = ICON_PATHS[name] || ICON_PATHS.sparkles;
  return `<svg class="ui-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}

function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach(el => {
    el.innerHTML = icon(el.dataset.icon, Number(el.dataset.iconSize || 18));
  });
}

async function copyText(value) {
  const text = String(value ?? "");
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
}

function showToast(message) {
  let region = document.getElementById("toast-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "toast-region";
    region.className = "toast-region";
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
  }
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = message;
  region.appendChild(toast);
  setTimeout(() => toast.classList.add("leaving"), 1800);
  setTimeout(() => toast.remove(), 2150);
}

document.addEventListener("click", async event => {
  const button = event.target.closest("[data-copy-code]");
  if (!button) return;
  const code = button.closest(".code-block")?.querySelector("code")?.textContent || "";
  await copyText(code);
  button.innerHTML = `${icon("check", 14)}已复制`;
  setTimeout(() => { button.innerHTML = `${icon("copy", 14)}复制`; }, 1400);
});

const Theme = {
  current() {
    return localStorage.getItem("gca_theme") || "system";
  },
  apply(mode = this.current()) {
    const dark = mode === "dark" || (mode === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
    document.querySelectorAll("[data-theme-icon]").forEach(el => {
      el.innerHTML = icon(dark ? "sun" : "moon", 18);
    });
  },
  toggle() {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("gca_theme", next);
    this.apply(next);
    showToast(next === "dark" ? "已切换到深色模式" : "已切换到浅色模式");
  },
};

Theme.apply();
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
  if (Theme.current() === "system") Theme.apply("system");
});

function initTopbar() {
  const el = document.getElementById("topbar-right");
  if (!el) return;
  let html = `<span class="badge">${escapeHtml(Auth.username() || "")}</span>`;
  // 设置页在新标签打开：不离开当前对话页，进行中的问答流不被中断
  if (Auth.canAccessSettings() && !location.pathname.startsWith("/admin")) {
    html += `<a class="btn small ghost" href="/admin" target="_blank" rel="noopener">${icon("settings", 14)}设置</a>`;
  }
  if (location.pathname.startsWith("/admin")) {
    html += `<a class="btn small ghost" href="/">${icon("external", 14)}对话页</a>`;
  }
  html += `<button class="btn small ghost" id="topbar-theme" aria-label="切换外观"><span data-theme-icon></span></button>`;
  html += `<button class="btn small ghost" id="topbar-password">${icon("key", 14)}修改密码</button>`;
  html += `<button class="btn small danger" id="topbar-logout">${icon("logout", 14)}退出</button>`;
  el.innerHTML = html;
  el.querySelector("#topbar-theme").onclick = () => Theme.toggle();
  el.querySelector("#topbar-password").onclick = () => openChangePasswordDialog();
  el.querySelector("#topbar-logout").onclick = logout;
  Theme.apply();
  hydrateIcons(el);
  applyBranding();
}

/* 应用品牌设置（系统名称 + Logo）到当前页面标题与品牌区。免鉴权读取，失败静默回退。 */
async function applyBranding() {
  let data;
  try {
    const resp = await fetch("/api/v1/settings/public");
    if (!resp.ok) return;
    data = await resp.json();
  } catch (_) { return; }
  const name = data.site_name || "智能体平台";
  document.title = location.pathname.startsWith("/admin") ? `设置 - ${name}`
    : location.pathname.startsWith("/login") ? `登录 - ${name}` : name;
  const titleEl = document.getElementById("brand-title");
  if (titleEl) {
    const logo = data.logo_url
      ? `<img class="brand-logo" src="${escapeHtml(data.logo_url)}" alt="${escapeHtml(name)} Logo" />` : "";
    titleEl.innerHTML = logo + escapeHtml(name);
  }
  applyFavicon(data.logo_url);
}

/* 用上传的系统 Logo 作为网站图标（浏览器标签页 favicon）；未设置时保留默认。 */
function applyFavicon(logoUrl) {
  if (!logoUrl) return;
  let link = document.getElementById("brand-favicon");
  if (!link) {
    link = document.createElement("link");
    link.id = "brand-favicon";
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.href = logoUrl;
}

/* 修改密码弹窗（所有登录用户可用，动态注入避免每页重复写 HTML） */
function openChangePasswordDialog() {
  let dlg = document.getElementById("pwd-dialog");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "pwd-dialog";
    dlg.innerHTML = `
      <h3>修改密码</h3>
      <label>旧密码</label>
      <input type="password" id="pwd-old" autocomplete="current-password" />
      <label>新密码（至少6位）</label>
      <input type="password" id="pwd-new" autocomplete="new-password" />
      <label>确认新密码</label>
      <input type="password" id="pwd-confirm" autocomplete="new-password" />
      <div class="error" id="pwd-error"></div>
      <div class="dialog-actions">
        <button class="btn ghost" id="pwd-cancel">取消</button>
        <button class="btn" id="pwd-submit">确认修改</button>
      </div>`;
    document.body.appendChild(dlg);
    document.getElementById("pwd-cancel").onclick = () => dlg.close();
    document.getElementById("pwd-submit").onclick = submitChangePassword;
  }
  for (const id of ["pwd-old", "pwd-new", "pwd-confirm"]) document.getElementById(id).value = "";
  document.getElementById("pwd-error").textContent = "";
  dlg.showModal();
}

async function logout() {
  try { await fetch("/api/v1/auth/logout", {method: "POST"}); } catch (_) {}
  Auth.clear();
  location.href = "/login";
}

async function submitChangePassword() {
  const errEl = document.getElementById("pwd-error");
  const oldPwd = document.getElementById("pwd-old").value;
  const newPwd = document.getElementById("pwd-new").value;
  const confirm = document.getElementById("pwd-confirm").value;
  errEl.textContent = "";
  if (!oldPwd || newPwd.length < 6) { errEl.textContent = "请填写旧密码和至少6位的新密码"; return; }
  if (newPwd !== confirm) { errEl.textContent = "两次输入的新密码不一致"; return; }
  try {
    await api("/api/v1/auth/change-password", {
      method: "POST", json: { old_password: oldPwd, new_password: newPwd },
    });
    document.getElementById("pwd-dialog").close();
    alert("密码修改成功，请使用新密码重新登录");
    Auth.clear();
    location.href = "/login";
  } catch (e) {
    errEl.textContent = e.message;
  }
}
