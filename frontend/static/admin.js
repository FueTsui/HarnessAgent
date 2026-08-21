Auth.requireLogin();
if (!Auth.canAccessSettings()) location.href = "/";
initTopbar();

const $ = id => document.getElementById(id);
const state = {
  agents: [], providers: [], skills: [], mcp: [], editingAgent: null,
  capabilities: [],
  activeResource: null, editingResource: null, resourceRows: [],
  resourceMode: "edit", providerPresets: {}, userModules: [],
  auditPage: 0, auditPageSize: 10, auditTotal: 0,
  tokenDays: 30, tokenUserId: "", tokenLimitUserId: null,
  archivedConversations: [], archivedProjects: [],
  weixinChannelId: null, weixinPollTimer: null,
  providerGovernance: new Map(), providerPriceId: null,
  operationsHours: 24,
};

const resources = {
  "token-usage": {
    title: "Token 用量", copy: "usage 明细永久保留；系统管理员可设置用户每周、每月和总量上限，并独立重置周/月计费周期。",
    endpoint: "/api/v1/token-usage", directSettings: true, readOnlyCreate: true,
  },
  archive: {
    title: "归档", copy: "集中管理当前账户已归档的对话和项目，可恢复或永久删除。",
    archiveManagement: true, directSettings: true, readOnlyCreate: true,
  },
  knowledge: {
    title: "知识库", copy: "创建者可维护知识库和文档；其他用户只能查看已共享内容，并在对话中引用使用。",
    endpoint: "/api/v1/knowledge/datasets", create: "/api/v1/knowledge/datasets",
    createLabel: "新建知识库", importLabel: "上传文档", exportLabel: "导出目录",
    exportType: "manifest",
    fields: [
      {name: "name", label: "知识库名称", required: true, createOnly: true, placeholder: "例如：产品操作手册"},
      {name: "is_public", label: "允许所有用户在对话中引用", type: "checkbox", editOnly: true},
    ],
  },
  mcp: {
    title: "MCP", copy: "通过连接向导配置 MCP 服务；请求头按键值对管理，密钥不会出现在卡片中。",
    endpoint: "/api/v1/mcp-servers",
    createLabel: "连接 MCP", importLabel: "导入 JSON", exportLabel: "导出配置",
    importAccept: ".json,application/json", importEndpoint: "/api/v1/mcp-servers/import",
    exportEndpoint: "/api/v1/mcp-servers/export", exportName: "mcp-servers.json",
    fields: [
      {name: "name", label: "连接名称", required: true, placeholder: "例如：Tavily 搜索"},
      {name: "description", label: "用途说明", type: "textarea", placeholder: "说明智能体应在什么情况下使用它"},
      {name: "transport", label: "传输协议", type: "select", options: [["http", "HTTP"], ["sse", "SSE"]], value: "http"},
      {name: "url", label: "服务地址", type: "url", required: true, placeholder: "https://example.com/mcp"},
      {name: "headers", label: "鉴权请求头", type: "keyvalue", help: "例如 Authorization → Bearer …；留空则不发送附加请求头"},
      {name: "risk_policy", label: "工具风险策略", type: "select", options: [["auto", "自动识别（推荐）"], ["read_only", "已审核为只读服务"]], value: "auto", help: "只读策略仍会拦截带写入动词或服务端写操作注解的工具。"},
      {name: "enabled", label: "启用此连接", type: "checkbox", value: true},
      {name: "is_public", label: "允许其他获授权用户使用", type: "checkbox", editOnly: true},
    ],
  },
  skills: {
    title: "Skills", copy: "用说明、执行指令和配套资源创建能力包，也可直接导入 SKILL.md 或 ZIP 包。",
    endpoint: "/api/v1/skills",
    createLabel: "新建 Skill", importLabel: "导入技能包", exportLabel: "导出全部",
    importAccept: ".json,.zip,.md,application/json,application/zip,text/markdown", importEndpoint: "/api/v1/skills/import",
    exportEndpoint: "/api/v1/skills/export", exportName: "skills.json",
    fields: [
      {name: "name", label: "Skill 名称", required: true},
      {name: "description", label: "触发说明", type: "textarea", required: true, placeholder: "描述何时应该使用此 Skill"},
      {name: "instructions", label: "执行指令", type: "textarea", required: true, large: true, placeholder: "写清步骤、约束、输入输出与验证要求"},
      {name: "resources", label: "配套资源", type: "resources", help: "添加会随 Skill 一起注入的参考文本"},
      {name: "enabled", label: "启用此 Skill", type: "checkbox", value: true},
      {name: "is_public", label: "允许其他获授权用户使用", type: "checkbox", editOnly: true},
    ],
  },
  templates: {
    title: "模板", copy: "上传 Word、PPT、Excel 或 Markdown 源文件，系统会自动识别 {{占位符}} 并保留原排版。",
    endpoint: "/api/v1/templates",
    createLabel: "新建模板", importLabel: "导入模板包", exportLabel: "",
    importAccept: ".zip,.docx,.md,.txt,.pptx,.xlsx", importEndpoint: "/api/v1/templates/import",
    fields: [
      {name: "name", label: "模板名称", required: true},
      {name: "description", label: "使用说明", type: "textarea"},
      {name: "kind", label: "模板类型", type: "select", createOnly: true, options: [["word", "Word 文档"], ["ppt", "PowerPoint"], ["excel", "Excel 工作簿"], ["md", "Markdown / 文本"]], value: "word"},
      {name: "file", label: "模板源文件", type: "file", createOnly: true, accept: ".docx,.md,.txt,.pptx,.xlsx", help: "在文件中使用 {{字段名}} 标记需要填充的位置"},
      {name: "enabled", label: "启用此模板", type: "checkbox", value: true, editOnly: true},
      {name: "is_public", label: "允许所有用户在对话中引用", type: "checkbox"},
    ],
  },
  schedules: {
    title: "定时任务", copy: "用标准五字段 Cron 表达式周期执行智能体任务；实际运行复用统一任务队列、日志和取消机制。",
    endpoint: "/api/v1/schedules",
    createLabel: "新建定时任务", importLabel: "", exportLabel: "",
    fields: [
      {name: "name", label: "任务名称", required: true, createOnly: true},
      {name: "agent_id", label: "执行智能体", type: "agent-select", required: true, createOnly: true},
      {name: "cron", label: "Cron 表达式", required: true, createOnly: true, placeholder: "例如：0 9 * * 1-5", help: "minute hour day month weekday"},
      {name: "timezone", label: "时区", value: "Asia/Shanghai", createOnly: true},
      {name: "query", label: "执行目标", type: "textarea", required: true, large: true, createOnly: true},
      {name: "enabled", label: "启用", type: "checkbox", value: true, editOnly: true},
    ],
  },
  capabilities: {
    title: "内置能力",
    copy: "仅 root 可调整全局开关。停用后所有智能体立即失去该工具；分配关系会保留，重新启用即可恢复。",
    endpoint: "/api/v1/capabilities/manage",
    readOnlyCreate: true,
  },
  providers: {
    title: "模型提供商", copy: "按协议配置任意 OpenAI 或 Anthropic 兼容网关，可在保存前识别当前凭据可用的模型。",
    endpoint: "/api/v1/providers",
    createLabel: "添加模型", importLabel: "导入 Codex 登录", exportLabel: "导出安全配置",
    exportType: "safe-providers",
    fields: [
      {name: "connection", label: "连接与协议", type: "section", copy: "选择兼容协议和实际调用线路；Azure、代理网关和私有部署均使用相同配置方式。"},
      {name: "provider_type", label: "兼容协议", type: "select", options: [["openai", "OpenAI 兼容"], ["anthropic", "Anthropic 兼容"], ["chatgpt", "ChatGPT 订阅（Codex 登录）"]], value: "openai"},
      {name: "name", label: "显示名称", required: true},
      {name: "base_url", label: "API 基础地址", type: "url", required: true, placeholder: "https://api.example.com/v1"},
      {name: "api_key", label: "API Key", type: "password", createOnly: false, help: "编辑时留空表示保留现有凭据"},
      {name: "wire_api", label: "调用线路", type: "select", options: [["responses", "Responses API"], ["chat_completions", "Chat Completions"], ["messages", "Anthropic Messages"]], value: "responses"},
      {name: "auth_type", label: "认证方式", type: "select", options: [["bearer", "Authorization: Bearer"], ["api_key", "api-key 请求头"], ["x_api_key", "x-api-key 请求头"], ["custom", "自定义请求头"], ["none", "无需认证"]], value: "bearer"},
      {name: "auth_header", label: "认证请求头名称", placeholder: "例如 X-API-Key"},
      {name: "models", label: "通用模型", type: "section", copy: "参考 OpenClaw 模型元数据：选择一个模型 ID，并显式声明它的名称、输入模态、推理能力和窗口限制。"},
      {name: "model_id", label: "模型 ID", required: true, detectModels: true, placeholder: "先识别模型或手动输入模型 ID"},
      {name: "model_name", label: "模型显示名称", placeholder: "可选，例如 GPT 5.6"},
      {name: "model_input", label: "输入模态", type: "model-input", value: ["text"], help: "文本为必选；只有确认模型原生接收图片时才开启图片。"},
      {name: "model_reasoning", label: "支持推理", type: "checkbox", help: "声明模型能够接收推理强度配置；它不是通过模型名称猜测的。"},
      {name: "generation", label: "模型限制与生成参数", type: "section"},
      {name: "context_window", label: "上下文窗口", type: "number", min: 0, value: 0, help: "0 表示使用系统默认上下文预算。"},
      {name: "max_tokens", label: "最大输出 Tokens", type: "number", min: 1, value: 8192},
      {name: "max_tokens_param", label: "输出长度参数名", type: "select", options: [["auto", "按协议自动"], ["max_tokens", "max_tokens"], ["max_completion_tokens", "max_completion_tokens"], ["max_output_tokens", "max_output_tokens"], ["none", "不发送"]], value: "auto"},
      {name: "reasoning_effort", label: "推理强度", type: "select", options: [["", "服务默认"], ["minimal", "Minimal"], ["low", "Low"], ["medium", "Medium"], ["high", "High"], ["xhigh", "XHigh"], ["max", "Max"]], value: ""},
      {name: "supports_temperature", label: "发送 Temperature 参数", type: "checkbox", value: true, help: "若模型不接受 temperature，请关闭。"},
      {name: "reliability", label: "超时与重试", type: "section"},
      {name: "timeout_ms", label: "请求超时（毫秒）", type: "number", min: 1000, value: 120000},
      {name: "stream_idle_timeout_ms", label: "流式空闲超时（毫秒）", type: "number", min: 1000, value: 300000},
      {name: "max_retries", label: "普通请求重试次数", type: "number", min: 0, value: 3},
      {name: "stream_max_retries", label: "流式请求重试次数", type: "number", min: 0, value: 3},
      {name: "advanced", label: "高级请求配置", type: "section", copy: "用于 Azure API 版本、代理网关自定义头和供应商专有参数。"},
      {name: "api_version", label: "API 版本", placeholder: "例如 2023-06-01"},
      {name: "api_version_mode", label: "API 版本传递方式", type: "select", options: [["none", "不传递"], ["header", "请求头"], ["query", "查询参数"]], value: "none"},
      {name: "model_list_path", label: "模型列表路径", value: "/models", placeholder: "/models"},
      {name: "custom_headers", label: "附加请求头", type: "keyvalue", help: "敏感请求头保存后会脱敏显示；禁止覆盖 Host、Content-Length 等传输头。"},
      {name: "extra_body", label: "附加请求参数", type: "json-keyvalue", help: "值支持 JSON 类型，如 true、0.7、[\"tag\"]；model、messages、tools 等核心字段由系统管理。"},
      {name: "enabled", label: "启用此提供商", type: "checkbox", value: true},
      {name: "is_public", label: "开放给对话用户选择", type: "checkbox", help: "开启后，该模型会出现在对话输入框的模型选择菜单中。"},
    ],
  },
  keys: {
    title: "API 密钥", copy: "为外部应用签发独立密钥。明文只展示一次，因此不提供导入或明文导出。",
    endpoint: "/api/v1/api-keys",
    createLabel: "签发密钥", fields: [{name: "name", label: "用途名称", required: true, placeholder: "例如：数据门户生产环境"}],
  },
  channels: {
    title: "消息渠道", copy: "每位用户通过二维码连接自己的微信，并将消息绑定到独立的个人智能体空间。",
    endpoint: "/api/v1/channels",
    createLabel: "连接个人微信", fields: [
      {name: "name", label: "渠道名称", required: true, value: "我的微信"},
      {name: "type", label: "渠道类型", type: "select", createOnly: true, options: [["openclaw_weixin", "微信（扫码连接）"]], value: "openclaw_weixin"},
      {name: "agent_id", label: "绑定智能体", type: "agent-select", required: true, help: "配置可共享，但对话、记忆、任务和文件空间均按当前用户隔离。"},
      {name: "enabled", label: "启用此渠道", type: "checkbox", value: true, editOnly: true},
    ],
  },
  users: {
    title: "用户", copy: "按角色创建账号，并用可视化清单分配设置模块权限。普通用户可使用获授权的通用模块。",
    endpoint: "/api/v1/users",
    createLabel: "新建用户", importLabel: "导入 CSV", exportLabel: "导出账号清单",
    importAccept: ".csv,text/csv", importType: "users-csv", exportType: "users-csv",
    fields: [
      {name: "username", label: "用户名", required: true, createOnly: true},
      {name: "password", label: "初始密码", type: "password", createRequired: true, help: "编辑时留空表示不修改"},
      {name: "role", label: "角色", type: "select", options: [["user", "普通用户"], ["admin", "管理员"], ["root", "Root 管理员"]], value: "user"},
      {name: "all_modules", label: "授予当前角色全部可用模块", type: "checkbox", value: false},
      {name: "modules", label: "可访问设置模块", type: "module-choices"},
      {name: "is_active", label: "账号启用", type: "checkbox", value: true, editOnly: true},
    ],
  },
  settings: {
    title: "系统设置", copy: "统一管理系统基础信息、公开访问地址与品牌标识。修改后直接保存并即时生效。",
    endpoint: "/api/v1/settings",
    directSettings: true, singleton: true, fields: [
      {name: "general", label: "通用设置", type: "section", copy: "用于浏览器标题、页面品牌名称以及外部回调地址。"},
      {name: "site_name", label: "系统名称", required: true},
      {name: "public_base_url", label: "公开访问地址", type: "url", placeholder: "https://agent.example.com"},
      {name: "branding", label: "品牌标识", type: "section", copy: "Logo 仅作为内容标识，不影响系统按钮和状态配色。"},
      {name: "logo", label: "品牌 Logo", type: "file", accept: ".png,.jpg,.jpeg,.svg,.webp", help: "仅用于标识，不参与界面配色"},
      {name: "remove_logo", label: "移除当前自定义 Logo", type: "checkbox"},
    ],
  },
  audit: {
    title: "操作日志", copy: "查看管理端变更记录，并导出 CSV 供审计留档；日志不可创建、导入或修改。",
    endpoint: "/api/v1/audit-logs",
    clearLabel: "清空日志", exportLabel: "导出 CSV", exportType: "audit-csv", readOnlyCreate: true,
  },
};

function json(value, fallback = {}) {
  if (value && typeof value === "object") return value;
  try { return JSON.parse(value || ""); } catch (_) { return fallback; }
}

function providerName(id) {
  return state.providers.find(item => item.id === id)?.name || (id ? `#${id}` : "默认模型");
}

function choiceHtml(items, selected, name, currentId) {
  return items.filter(item => item.id !== currentId).map(item => `
    <label class="choice"><input type="checkbox" name="${name}" value="${item.id}"
      ${(selected || []).includes(item.id) ? "checked" : ""} />${escapeHtml(item.name)}</label>`).join("")
    || '<span class="hint">暂无可选项</span>';
}

function checked(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(item => Number(item.value));
}

function checkedStrings(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(item => item.value);
}

async function loadCatalogs() {
  const agentCatalogRequest = Auth.canModule("agents")
    ? api("/api/v1/agents")
    : (Auth.canModule("schedules") || Auth.canModule("channels"))
      ? api("/api/v1/agents/enabled")
      : Promise.resolve([]);
  const results = await Promise.allSettled([
    Auth.canModule("providers") ? api("/api/v1/providers") : Promise.resolve([]),
    Auth.canModule("skills") ? api("/api/v1/skills") : Promise.resolve([]),
    Auth.canModule("mcp") ? api("/api/v1/mcp-servers") : Promise.resolve([]),
    Auth.canModule("providers") ? api("/api/v1/providers/presets") : Promise.resolve({}),
    Auth.role() === "root" ? api("/api/v1/users/modules") : Promise.resolve([]),
    api("/api/v1/capabilities"),
    agentCatalogRequest,
  ]);
  state.providers = results[0].status === "fulfilled" ? results[0].value : [];
  state.skills = results[1].status === "fulfilled" ? results[1].value : [];
  state.mcp = results[2].status === "fulfilled" ? results[2].value : [];
  state.providerPresets = results[3].status === "fulfilled" ? results[3].value : {};
  state.userModules = results[4].status === "fulfilled" ? results[4].value : [];
  state.capabilities = results[5].status === "fulfilled" ? (results[5].value.builtin_tools || []) : [];
  state.agents = results[6].status === "fulfilled" ? results[6].value : [];
}

async function refreshAgentOptions() {
  if (Auth.canModule("agents")) {
    state.agents = await api("/api/v1/agents");
  } else if (Auth.canModule("schedules") || Auth.canModule("channels")) {
    state.agents = await api("/api/v1/agents/enabled");
  } else {
    state.agents = [];
  }
  return state.agents;
}

function resourceUsesAgentSelect(key) {
  return (resources[key]?.fields || []).some(field => field.type === "agent-select");
}

async function ensureResourceDependencies(key) {
  if (resourceUsesAgentSelect(key)) await refreshAgentOptions();
}

async function loadAgents() {
  state.agents = await api("/api/v1/agents");
  $("agents-grid").innerHTML = state.agents.map(agent => `
    <article class="card agent-card ${agent.enabled ? "" : "disabled"}">
      <div class="card-title-row">
        <label class="select-check" title="选择用于导出"><input type="checkbox" data-agent-select="${agent.id}" /></label>
        <div class="agent-icon">${icon("sparkles", 17)}</div>
        <div><h3>${escapeHtml(agent.name)}</h3><span>Harness v${agent.active_version}</span></div>
        ${agent.is_default ? '<b class="tag">默认</b>' : ""}
      </div>
      <p>${escapeHtml(agent.description || "未填写用途说明")}</p>
      <div class="meta-row">
        <span>${escapeHtml(providerName(agent.provider_id))}</span>
        <span>Skills ${agent.skill_ids.length}</span>
        <span>内置 ${(agent.builtin_tools || []).filter(name =>
          state.capabilities.some(item => item.name === name && item.enabled)).length}/${(agent.builtin_tools || []).length}</span>
        <span>MCP ${agent.mcp_ids.length}</span>
        <span>${agent.memory_enabled ? "记忆开启" : "无长期记忆"}</span>
      </div>
      <div class="card-actions">
        ${agent.can_manage ? `<button class="btn small ghost" data-edit="${agent.id}">配置</button>` : ""}
        <button class="btn small ghost" data-versions="${agent.id}">版本</button>
        ${Auth.role() === "root" && !agent.is_default ? `<button class="btn small ghost" data-default="${agent.id}">设为默认</button>` : ""}
        ${agent.can_manage ? `<button class="btn small danger" data-delete="${agent.id}">删除</button>` : ""}
      </div>
    </article>`).join("") || '<div class="empty-state">暂无智能体</div>';
  $("agents-grid").querySelectorAll("[data-edit]").forEach(b => b.onclick = () => openAgent(Number(b.dataset.edit)));
  $("agents-grid").querySelectorAll("[data-versions]").forEach(b => b.onclick = () => openVersions(Number(b.dataset.versions)));
  $("agents-grid").querySelectorAll("[data-default]").forEach(b => b.onclick = async () => {
    await api(`/api/v1/agents/${b.dataset.default}`, {method: "PATCH", json: {is_default: true}});
    loadAgents();
  });
  $("agents-grid").querySelectorAll("[data-delete]").forEach(b => b.onclick = async () => {
    if (!confirm("删除智能体及其全部 Harness 版本？")) return;
    try { await api(`/api/v1/agents/${b.dataset.delete}`, {method: "DELETE"}); await loadAgents(); }
    catch (error) { alert(error.message); }
  });
}

function openAgent(id = null) {
  const agent = state.agents.find(item => item.id === id) || null;
  state.editingAgent = agent;
  $("agent-dialog-title").textContent = agent ? `配置 ${agent.name}` : "新建智能体";
  $("agent-name").value = agent?.name || "";
  $("agent-description").value = agent?.description || "";
  $("agent-opening").value = agent?.opening_statement || "";
  $("agent-prompt").value = agent?.system_prompt || "";
  $("agent-provider").innerHTML = '<option value="">默认模型</option>' + state.providers.map(item =>
    `<option value="${item.id}" ${agent?.provider_id === item.id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
  const routing = agent?.routing?.mode === "policy" ? agent.routing : {};
  const fallbackIds = new Set(routing.fallback_provider_ids || []);
  $("agent-route-strategy").value = routing.strategy || (fallbackIds.size ? "ordered" : "fixed");
  $("agent-provider-fallbacks").innerHTML = state.providers.map(item => `
    <label class="choice"><input type="checkbox" name="agent_provider_fallback" value="${item.id}"
      ${fallbackIds.has(item.id) ? "checked" : ""} />${escapeHtml(item.name)}</label>`).join("") || '<span class="hint">暂无备用 Provider</span>';
  const health = routing.health || {};
  $("agent-health-lookback").value = health.lookback_minutes || 60;
  $("agent-health-min-samples").value = health.min_samples || 3;
  $("agent-health-error-rate").value = health.max_error_rate ?? 0.6;
  $("agent-health-consecutive").value = health.consecutive_failures || 3;
  $("agent-skills").innerHTML = choiceHtml(state.skills, agent?.skill_ids, "agent_skill", id);
  $("agent-mcp").innerHTML = choiceHtml(state.mcp, agent?.mcp_ids, "agent_mcp", id);
  $("agent-children").innerHTML = choiceHtml(state.agents, agent?.agent_ids, "agent_child", id);
  if (Auth.role() === "root") {
    $("agent-builtins").innerHTML = state.capabilities.map(item => {
      const selected = (agent?.builtin_tools || []).includes(item.name);
      const unavailable = !item.enabled;
      return `<label class="choice ${unavailable ? "disabled" : ""}" title="${escapeHtml(item.description || "")}">
        <input type="checkbox" name="agent_builtin" value="${escapeHtml(item.name)}"
          ${selected ? "checked" : ""} ${unavailable ? "disabled" : ""} />
        ${escapeHtml(item.name)} · ${escapeHtml(item.group)}${unavailable ? "（全局停用）" : ""}
      </label>`;
    }).join("") || '<span class="hint">未注册内置工具</span>';
  } else {
    const assigned = new Set(agent?.builtin_tools || []);
    $("agent-builtins").innerHTML = state.capabilities
      .filter(item => assigned.has(item.name) && item.enabled)
      .map(item => `<span class="choice" title="${escapeHtml(item.description || "")}">${escapeHtml(item.name)} · ${escapeHtml(item.group)}</span>`)
      .join("") || '<span class="hint">未分配可用的内置工具</span>';
  }
  $("agent-memory").checked = !!agent?.memory_enabled;
  $("agent-public").checked = agent ? !!agent.is_public : true;
  $("agent-enabled").checked = agent ? !!agent.enabled : true;
  $("agent-error").textContent = "";
  $("agent-dialog").showModal();
}

async function saveAgent() {
  const routeStrategy = $("agent-route-strategy").value;
  const mainProviderId = $("agent-provider").value ? Number($("agent-provider").value) : null;
  const fallbackIds = checked("agent_provider_fallback").filter(id => id !== mainProviderId);
  const payload = {
    name: $("agent-name").value.trim(),
    description: $("agent-description").value.trim(),
    opening_statement: $("agent-opening").value.trim(),
    system_prompt: $("agent-prompt").value,
    provider_id: mainProviderId,
    clear_provider: !$("agent-provider").value,
    skill_ids: checked("agent_skill"),
    mcp_ids: checked("agent_mcp"),
    agent_ids: checked("agent_child"),
    memory_enabled: $("agent-memory").checked,
    is_public: $("agent-public").checked,
    enabled: $("agent-enabled").checked,
    routing: routeStrategy === "fixed" && !fallbackIds.length ? {} : {
      mode: "policy",
      default_provider_id: mainProviderId,
      fallback_provider_ids: fallbackIds,
      strategy: routeStrategy === "lowest_cost" ? "lowest_cost" : "ordered",
      health: {
        enabled: true,
        lookback_minutes: Number($("agent-health-lookback").value || 60),
        min_samples: Number($("agent-health-min-samples").value || 3),
        max_error_rate: Number($("agent-health-error-rate").value || 0.6),
        consecutive_failures: Number($("agent-health-consecutive").value || 3),
      },
      // The compact dialog edits the default route only.  Preserve advanced
      // modality/rule routing configured through the API instead of erasing it.
      rules: Array.isArray(state.editingAgent?.routing?.rules)
        ? state.editingAgent.routing.rules
        : [],
    },
  };
  if (Auth.role() === "root") {
    // 被全局停用但原本已分配的能力仍保留绑定，重新启用后自动恢复。
    const disabledAssigned = (state.editingAgent?.builtin_tools || []).filter(name =>
      state.capabilities.some(item => item.name === name && !item.enabled)
    );
    payload.builtin_tools = [...new Set([
      ...checkedStrings("agent_builtin"), ...disabledAssigned,
    ])];
  }
  if (!payload.name) { $("agent-error").textContent = "名称必填"; return; }
  try {
    if (state.editingAgent) {
      await api(`/api/v1/agents/${state.editingAgent.id}`, {method: "PATCH", json: payload});
    } else {
      await api("/api/v1/agents", {method: "POST", json: payload});
    }
    $("agent-dialog").close();
    await loadAgents();
  } catch (error) { $("agent-error").textContent = error.message; }
}

async function openVersions(agentId) {
  const agent = state.agents.find(item => item.id === agentId);
  const versions = await api(`/api/v1/agents/${agentId}/versions`);
  $("versions-title").textContent = `${agent?.name || "智能体"} · Harness 版本`;
  $("versions-list").innerHTML = versions.map(version => `
    <div class="stack-item">
      <div><strong>v${version.version}</strong><span>${escapeHtml(version.change_summary || "无变更说明")}</span></div>
      <div class="stack-actions">
        <b class="status ${version.status}">${version.status}</b>
        ${version.version !== agent.active_version ? `<button class="btn small ghost" data-publish="${version.version}">发布/回滚</button>` : ""}
      </div>
      <details><summary>查看指令与策略</summary><pre>${escapeHtml(JSON.stringify(version, null, 2))}</pre></details>
    </div>`).join("");
  $("versions-list").querySelectorAll("[data-publish]").forEach(button => {
    button.onclick = async () => {
      await api(`/api/v1/agents/${agentId}/versions/${button.dataset.publish}/publish`, {method: "POST"});
      await loadAgents();
      openVersions(agentId);
    };
  });
  $("versions-dialog").showModal();
}

async function loadImprovement() {
  const [runs, proposals] = await Promise.all([
    api("/api/v1/improvement/turns"), api("/api/v1/improvement/proposals"),
  ]);
  $("runs-list").innerHTML = runs.map(run => `
    <div class="stack-item">
      <div><strong>${escapeHtml(run.agent_name || "未知智能体")}</strong><span>${escapeHtml(run.id)} · Harness v${run.harness_version || "?"}</span></div>
      <b class="status ${run.status}">${run.status}</b>
    </div>`).join("") || '<div class="empty-state">暂无运行记录</div>';
  $("proposals-list").innerHTML = proposals.map(item => `
    <div class="stack-item">
      <div><strong>#${item.id} · Agent ${item.agent_id}</strong><span>${escapeHtml(item.hypothesis)}</span></div>
      <div class="stack-actions">
        <b class="status ${item.status}">${item.status}</b>
        ${item.status === "proposed" ? `<button class="btn small ghost" data-eval="${item.id}">启动自动评估</button>` : ""}
        ${item.status === "evaluating" ? `<button class="btn small ghost" data-eval="${item.id}">检查评估结果</button>` : ""}
        ${item.status === "evaluated" ? `<button class="btn small" data-approve="${item.id}">人工批准</button>` : ""}
      </div>
    </div>`).join("") || '<div class="empty-state">暂无改进提案</div>';
  $("proposals-list").querySelectorAll("[data-eval]").forEach(button => button.onclick = async () => {
    await api(`/api/v1/improvement/proposals/${button.dataset.eval}/evaluation`, {
      method: "POST", json: {},
    });
    loadImprovement();
  });
  $("proposals-list").querySelectorAll("[data-approve]").forEach(button => button.onclick = async () => {
    if (!confirm("批准后将发布该 Harness 版本。继续？")) return;
    await api(`/api/v1/improvement/proposals/${button.dataset.approve}/approve`, {method: "POST"});
    await Promise.all([loadImprovement(), loadAgents()]);
  });
}

function openProposal() {
  $("proposal-agent").innerHTML = state.agents.map(agent =>
    `<option value="${agent.id}">${escapeHtml(agent.name)} · v${agent.active_version}</option>`).join("");
  $("proposal-hypothesis").value = "";
  $("proposal-prompt").value = state.agents[0]?.system_prompt || "";
  $("proposal-evidence").value = "";
  $("proposal-error").textContent = "";
  $("proposal-dialog").showModal();
}

async function saveProposal() {
  try {
    await api("/api/v1/improvement/proposals", {
      method: "POST",
      json: {
        agent_id: Number($("proposal-agent").value),
        hypothesis: $("proposal-hypothesis").value.trim(),
        system_prompt: $("proposal-prompt").value,
        evidence: $("proposal-evidence").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean),
      },
    });
    $("proposal-dialog").close();
    loadImprovement();
  } catch (error) { $("proposal-error").textContent = error.message; }
}

function resourceName(item) {
  return item.name || item.username || item.site_name || item.id || item.key || "记录";
}

function fieldValue(field, item) {
  if (item && item[field.name] !== undefined && item[field.name] !== null) return item[field.name];
  return field.value ?? (field.type === "checkbox" ? false : "");
}

function fieldOptions(field) {
  if (field.type === "agent-select") {
    return state.agents
      .filter(item => item.enabled !== false)
      .map(item => [item.id, item.name]);
  }
  return typeof field.options === "function" ? field.options() : (field.options || []);
}

function renderKeyValueRows(value = {}, typed = false) {
  const entries = Object.entries(value || {});
  if (!entries.length) entries.push(["", ""]);
  return entries.map(([key, val]) => `
    <div class="repeat-row">
      <input data-part="key" value="${escapeHtml(key)}" placeholder="${typed ? "参数名称" : "Header 名称"}" />
      <input data-part="value" value="${escapeHtml(typed && typeof val !== "string" ? JSON.stringify(val) : val)}" placeholder="${typed ? "JSON 值或文本" : "Header 值"}" />
      <button type="button" class="icon-btn danger" data-remove-row aria-label="删除此行">${icon("close", 15)}</button>
    </div>`).join("");
}

function renderResourceRows(value = []) {
  const rows = Array.isArray(value) && value.length ? value : [{name: "", content: ""}];
  return rows.map(item => `
    <div class="repeat-row resource-repeat">
      <input data-part="name" value="${escapeHtml(item.name || "")}" placeholder="资源名称" />
      <textarea data-part="content" placeholder="参考内容">${escapeHtml(item.content || "")}</textarea>
      <button type="button" class="icon-btn danger" data-remove-row aria-label="删除此行">${icon("close", 15)}</button>
    </div>`).join("");
}

function renderField(field, item, editing) {
  if ((field.editOnly && !editing) || (field.createOnly && editing)) return "";
  if (field.type === "section") {
    return `<div class="form-section" data-section="${field.name}">
      <div><strong>${escapeHtml(field.label)}</strong>${field.copy ? `<small>${escapeHtml(field.copy)}</small>` : ""}</div>
    </div>`;
  }
  const value = fieldValue(field, item);
  const required = field.required || (field.createRequired && !editing);
  const help = field.help ? `<small class="field-help">${escapeHtml(field.help)}</small>` : "";
  if (field.type === "checkbox") {
    return `<div class="form-field checkbox-field" data-field-wrap="${field.name}">
      <label><input type="checkbox" id="resource-field-${field.name}" data-field="${field.name}" ${value ? "checked" : ""} />
      <span>${escapeHtml(field.label)}</span></label>${help}</div>`;
  }
  if (field.type === "select" || field.type === "agent-select") {
    const options = fieldOptions(field).map(([optionValue, label]) =>
      `<option value="${escapeHtml(optionValue)}" ${String(value) === String(optionValue) ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
    return `<div class="form-field" data-field-wrap="${field.name}"><label for="resource-field-${field.name}">${escapeHtml(field.label)}</label>
      <select id="resource-field-${field.name}" data-field="${field.name}" ${required ? "required" : ""}>${options}</select>${help}</div>`;
  }
  if (field.type === "module-choices") {
    const selected = new Set(item?.modules || []);
    return `<div class="form-field form-field-wide" data-field-wrap="${field.name}"><label>${escapeHtml(field.label)}</label>
      <div class="choice-grid permission-grid">${state.userModules.map(module =>
        `<label class="choice" data-module-roles="${escapeHtml((module.roles || ["admin"]).join(","))}"><input type="checkbox" name="resource-modules" value="${escapeHtml(module.key)}" ${selected.has(module.key) ? "checked" : ""} />${escapeHtml(module.label)}</label>`).join("") || '<span class="hint">暂无模块</span>'}</div>${help}</div>`;
  }
  if (field.type === "model-input") {
    const selected = new Set(Array.isArray(value) ? value : ["text"]);
    return `<div class="form-field form-field-wide" data-field-wrap="${field.name}"><label>${escapeHtml(field.label)}</label>
      <div class="choice-grid" id="resource-field-${field.name}" data-field="${field.name}">
        <label class="choice"><input type="checkbox" name="resource-model-input" value="text" checked disabled />文本</label>
        <label class="choice"><input type="checkbox" name="resource-model-input" value="image" ${selected.has("image") ? "checked" : ""} />图片</label>
      </div>${help}</div>`;
  }
  if (field.type === "keyvalue" || field.type === "json-keyvalue") {
    const typed = field.type === "json-keyvalue";
    return `<div class="form-field form-field-wide" data-field-wrap="${field.name}"><label>${escapeHtml(field.label)}</label>
      <div class="repeat-list" id="resource-field-${field.name}" data-field="${field.name}" data-repeat="${field.type}">${renderKeyValueRows(value, typed)}</div>
      <button type="button" class="btn small ghost add-row" data-add-row="${field.type}" data-target="${field.name}">${icon("plus", 14)}${typed ? "添加参数" : "添加请求头"}</button>${help}</div>`;
  }
  if (field.type === "resources") {
    return `<div class="form-field form-field-wide" data-field-wrap="${field.name}"><label>${escapeHtml(field.label)}</label>
      <div class="repeat-list" id="resource-field-${field.name}" data-field="${field.name}" data-repeat="resources">${renderResourceRows(value)}</div>
      <button type="button" class="btn small ghost add-row" data-add-row="resources">${icon("plus", 14)}添加资源</button>${help}</div>`;
  }
  if (field.type === "file") {
    return `<div class="form-field form-field-wide" data-field-wrap="${field.name}"><label for="resource-field-${field.name}">${escapeHtml(field.label)}</label>
      <input id="resource-field-${field.name}" data-field="${field.name}" type="file" ${field.accept ? `accept="${escapeHtml(field.accept)}"` : ""} />${help}</div>`;
  }
  if (field.type === "textarea") {
    return `<div class="form-field ${field.large ? "form-field-wide" : ""}" data-field-wrap="${field.name}"><label for="resource-field-${field.name}">${escapeHtml(field.label)}${required ? " *" : ""}</label>
      <textarea id="resource-field-${field.name}" data-field="${field.name}" class="${field.large ? "code-area" : ""}" placeholder="${escapeHtml(field.placeholder || "")}" ${required ? "required" : ""}>${escapeHtml(value)}</textarea>${help}</div>`;
  }
  return `<div class="form-field" data-field-wrap="${field.name}"><label for="resource-field-${field.name}">${escapeHtml(field.label)}${required ? " *" : ""}</label>
    <div class="${field.detectModels ? "field-action-row" : ""}">
      <input id="resource-field-${field.name}" data-field="${field.name}" type="${field.type || "text"}" value="${field.type === "password" ? "" : escapeHtml(value)}"
        ${field.detectModels ? 'list="provider-model-options"' : ""} placeholder="${escapeHtml(field.placeholder || "")}" ${field.min !== undefined ? `min="${field.min}"` : ""} ${required ? "required" : ""} />
      ${field.detectModels ? `<button type="button" class="btn ghost detect-models" id="provider-detect-models">${icon("refresh", 15)}识别模型</button><datalist id="provider-model-options"></datalist>` : ""}
    </div>${field.detectModels ? '<div id="provider-model-result" class="model-result" hidden></div>' : ""}${help}</div>`;
}

function bindResourceForm(root = $("resource-fields")) {
  root.querySelectorAll("[data-remove-row]").forEach(button => {
    button.onclick = () => {
      const list = button.closest(".repeat-list");
      button.closest(".repeat-row").remove();
      if (!list.children.length) {
        list.innerHTML = list.dataset.repeat === "resources"
          ? renderResourceRows([])
          : renderKeyValueRows({}, list.dataset.repeat === "json-keyvalue");
        bindResourceForm(root);
      }
    };
  });
  root.querySelectorAll("[data-add-row]").forEach(button => {
    button.onclick = () => {
      const type = button.dataset.addRow;
      const list = $(`resource-field-${button.dataset.target || "resources"}`);
      list.insertAdjacentHTML("beforeend", type === "resources"
        ? renderResourceRows([])
        : renderKeyValueRows({}, type === "json-keyvalue"));
      bindResourceForm(root);
    };
  });
  const providerType = $("resource-field-provider_type");
  const importedChatGPT = state.editingResource?.provider_type === "chatgpt";
  const chatGPTOption = [...(providerType?.options || [])].find(option => option.value === "chatgpt");
  if (chatGPTOption) chatGPTOption.disabled = !importedChatGPT;
  if (providerType && importedChatGPT) providerType.disabled = true;
  const updateProviderFields = applyPreset => {
    if (!providerType) return;
    const preset = state.providerPresets[providerType.value] || {};
    if (applyPreset) {
      for (const name of ["base_url", "wire_api", "auth_type", "api_version", "api_version_mode", "model_id", "model_name"]) {
        const input = $(`resource-field-${name}`);
        if (input && preset[name] !== undefined) input.value = preset[name];
      }
      const temperature = $("resource-field-supports_temperature");
      if (temperature) temperature.checked = providerType.value !== "anthropic";
      const name = $("resource-field-name");
      if (name && !name.value) name.value = preset.label || providerType.options[providerType.selectedIndex]?.text || "";
    }
    const allowedWire = providerType.value === "anthropic"
      ? ["messages"]
      : providerType.value === "chatgpt" ? ["responses"] : ["responses", "chat_completions"];
    const wire = $("resource-field-wire_api");
    [...(wire?.options || [])].forEach(option => {
      option.hidden = !allowedWire.includes(option.value);
      option.disabled = !allowedWire.includes(option.value);
    });
    if (wire && !allowedWire.includes(wire.value)) wire.value = allowedWire[0];
    const customAuth = $("resource-field-auth_type")?.value === "custom";
    document.querySelector("[data-field-wrap='auth_header']")?.toggleAttribute("hidden", !customAuth);
    const supportsReasoning = $("resource-field-model_reasoning")?.checked;
    document.querySelector("[data-field-wrap='reasoning_effort']")?.toggleAttribute("hidden", !supportsReasoning);
  };
  providerType?.addEventListener("change", () => updateProviderFields(true));
  $("resource-field-auth_type")?.addEventListener("change", () => updateProviderFields(false));
  $("resource-field-model_reasoning")?.addEventListener("change", () => updateProviderFields(false));
  updateProviderFields(false);

  $("provider-detect-models")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    const resultBox = $("provider-model-result");
    button.disabled = true;
    button.textContent = "识别中…";
    resultBox.hidden = false;
    resultBox.innerHTML = '<span class="hint">正在读取服务端模型列表…</span>';
    try {
      const editing = Boolean(state.editingResource?.id);
      const discoveryEndpoint = editing
        ? `/api/v1/providers/${state.editingResource.id}/discover-models`
        : "/api/v1/providers/discover-models";
      const result = await api(discoveryEndpoint, {
        method: "POST", json: collectResourcePayload({allowIncomplete: true}),
      });
      const models = result.models || [];
      if (!models.length) throw new Error("接口未返回任何模型，请检查模型列表路径或手动输入模型 ID");
      $("provider-model-options").innerHTML = models.map(model => `<option value="${escapeHtml(model)}"></option>`).join("");
      resultBox.innerHTML = `<label for="provider-detected-select">已识别 ${models.length} 个模型</label>
        <div class="field-action-row"><select id="provider-detected-select">${models.map(model => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("")}</select>
        <button type="button" class="btn small" id="provider-use-model">选择此模型</button></div>`;
      $("provider-use-model").onclick = () => {
        $("resource-field-model_id").value = $("provider-detected-select").value;
        if (!$("resource-field-model_name").value) {
          $("resource-field-model_name").value = $("provider-detected-select").value;
        }
      };
    } catch (error) {
      resultBox.innerHTML = `<span class="form-error">${escapeHtml(error.message)}</span>`;
    } finally {
      button.disabled = false;
      button.innerHTML = `${icon("refresh", 15)}识别模型`;
    }
  });
  const role = $("resource-field-role");
  const updatePermissionVisibility = () => {
    const show = ["admin", "user"].includes(role?.value);
    document.querySelector("[data-field-wrap='all_modules']")?.toggleAttribute("hidden", !show);
    document.querySelector("[data-field-wrap='modules']")?.toggleAttribute("hidden", !show);
    document.querySelectorAll("[data-module-roles]").forEach(choice => {
      const applies = (choice.dataset.moduleRoles || "").split(",").includes(role?.value);
      choice.toggleAttribute("hidden", !applies);
      choice.querySelector("input").disabled = !applies;
    });
    const allModules = $("resource-field-all_modules");
    const choices = document.querySelector("[data-field-wrap='modules']");
    choices?.classList.toggle("disabled", Boolean(allModules?.checked));
  };
  role?.addEventListener("change", updatePermissionVisibility);
  $("resource-field-all_modules")?.addEventListener("change", updatePermissionVisibility);
  updatePermissionVisibility();
}

function openResourceEditor(item = null) {
  const config = resources[state.activeResource];
  state.editingResource = item;
  state.resourceMode = "edit";
  const editing = Boolean(item) && !config.singleton;
  $("resource-dialog-title").textContent = editing
    ? `编辑${config.title}`
    : config.singleton ? "系统设置" : (config.createLabel || `新建${config.title}`);
  $("resource-dialog-copy").textContent = config.copy;
  $("resource-fields").innerHTML = (config.fields || []).map(field => renderField(field, item, editing)).join("");
  $("resource-save").textContent = editing ? "保存更改" : config.singleton ? "保存设置" : "创建";
  $("resource-error").textContent = "";
  bindResourceForm();
  if (!editing && state.activeResource === "providers") {
    $("resource-field-provider_type")?.dispatchEvent(new Event("change"));
  }
  $("resource-dialog").showModal();
}

function openKnowledgeImport(dataset = "") {
  const manageableDatasets = state.resourceRows.filter(item => item.can_manage !== false);
  if (!manageableDatasets.length) {
    showToast("暂无你创建的知识库，请先新建知识库");
    return;
  }
  state.resourceMode = "knowledge-import";
  state.editingResource = null;
  $("resource-dialog-title").textContent = "上传知识库文档";
  $("resource-dialog-copy").textContent = "选择目标知识库并上传一个或多个文件，系统会自动解析可检索文本。";
  $("resource-fields").innerHTML = `
    <div class="form-field"><label for="resource-field-dataset">目标知识库</label>
      <select id="resource-field-dataset">${manageableDatasets.map(item =>
        `<option value="${escapeHtml(item.key)}" ${item.key === dataset ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></div>
    <div class="form-field form-field-wide"><label for="resource-field-documents">选择文档</label>
      <input id="resource-field-documents" type="file" multiple accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.xls" />
      <small class="field-help">支持文本、Markdown、CSV、JSON、PDF、Word 和 Excel；单个文件遵循系统上传大小限制。</small></div>`;
  $("resource-save").textContent = "上传文档";
  $("resource-error").textContent = "";
  $("resource-dialog").showModal();
}

function collectResourcePayload({allowIncomplete = false} = {}) {
  const config = resources[state.activeResource];
  const editing = Boolean(state.editingResource) && !config.singleton;
  const payload = {};
  for (const field of config.fields || []) {
    if ((field.editOnly && !editing) || (field.createOnly && editing)) continue;
    if (field.type === "section") continue;
    if (field.type === "module-choices") {
      payload[field.name] = [...document.querySelectorAll("input[name='resource-modules']:checked")].map(input => input.value);
      continue;
    }
    const input = $(`resource-field-${field.name}`);
    if (!input) continue;
    if (field.type === "checkbox") payload[field.name] = input.checked;
    else if (field.type === "model-input") {
      payload[field.name] = ["text", ...[...input.querySelectorAll("input:checked:not(:disabled)")].map(choice => choice.value)];
    }
    else if (field.type === "number") payload[field.name] = Number(input.value || 0);
    else if (field.type === "file") payload[field.name] = input.files?.[0] || null;
    else if (field.type === "keyvalue" || field.type === "json-keyvalue") {
      payload[field.name] = {};
      input.querySelectorAll(".repeat-row").forEach(row => {
        const key = row.querySelector("[data-part='key']").value.trim();
        if (!key) return;
        const raw = row.querySelector("[data-part='value']").value;
        if (field.type === "json-keyvalue") {
          try { payload[field.name][key] = JSON.parse(raw); }
          catch (_) { payload[field.name][key] = raw; }
        } else payload[field.name][key] = raw;
      });
    } else if (field.type === "resources") {
      payload[field.name] = [...input.querySelectorAll(".repeat-row")].map(row => ({
        name: row.querySelector("[data-part='name']").value.trim(),
        content: row.querySelector("[data-part='content']").value,
      })).filter(item => item.name || item.content);
    } else {
      const value = input.value.trim();
      if (field.type === "password" && editing && !value) continue;
      payload[field.name] = value;
    }
    const required = field.required || (field.createRequired && !editing);
    if (!allowIncomplete && required && !payload[field.name]) throw new Error(`${field.label}必填`);
  }
  return payload;
}

function formDataWithFile(payload) {
  const form = new FormData();
  for (const [key, value] of Object.entries(payload)) {
    if (value instanceof File) form.append(key, value);
    else if (value !== null && value !== undefined) form.append(key, String(value));
  }
  return form;
}

async function refreshResourceCatalog(key) {
  if (["providers", "skills", "mcp"].includes(key)) await loadCatalogs();
}

async function persistSystemSettings(payload) {
  const config = resources.settings;
  const {logo, remove_logo, ...settingsPayload} = payload;
  await api(config.endpoint, {method: "PUT", json: settingsPayload});
  if (remove_logo) await api(`${config.endpoint}/logo`, {method: "DELETE"});
  if (logo) {
    const form = new FormData();
    form.append("file", logo);
    await api(`${config.endpoint}/logo`, {method: "POST", body: form});
  }
  applyBranding();
}

async function saveResource() {
  const config = resources[state.activeResource];
  $("resource-error").textContent = "";
  let resultNotice = null;
  let createdResource = null;
  try {
    if (state.resourceMode === "knowledge-import") {
      const dataset = $("resource-field-dataset").value;
      const files = [...$("resource-field-documents").files];
      if (!dataset || !files.length) throw new Error("请选择目标知识库和至少一个文档");
      for (const file of files) {
        const form = new FormData();
        form.append("file", file);
        await api(`/api/v1/knowledge/${encodeURIComponent(dataset)}`, {method: "POST", body: form});
      }
      showToast(`已上传 ${files.length} 个文档`);
    } else if (state.resourceMode === "template-file") {
      const file = $("resource-field-template-file").files?.[0];
      if (!file) throw new Error("请选择新的模板源文件");
      const form = new FormData();
      form.append("file", file);
      await api(`/api/v1/templates/${state.editingResource.id}/file`, {method: "POST", body: form});
      showToast("模板源文件已替换");
    } else {
      const payload = collectResourcePayload();
      const editing = Boolean(state.editingResource) && !config.singleton;
      if (state.activeResource === "knowledge") {
        if (editing) {
          await api(`/api/v1/knowledge/datasets/${encodeURIComponent(state.editingResource.key)}/visibility`, {
            method: "PATCH", json: {is_public: payload.is_public},
          });
        } else {
          await api(config.create, {method: "POST", json: {name: payload.name}});
        }
      } else if (state.activeResource === "templates" && !editing) {
        const createPayload = {...payload};
        delete createPayload.enabled;
        await api(config.endpoint, {method: "POST", body: formDataWithFile(createPayload)});
      } else if (state.activeResource === "settings") {
        await persistSystemSettings(payload);
      } else {
        const endpoint = editing ? `${config.endpoint}/${state.editingResource.id}` : config.endpoint;
        const result = await api(endpoint, {method: editing ? "PATCH" : "POST", json: payload});
        if (!editing) createdResource = result;
        if (state.activeResource === "keys" && result?.api_key) {
          resultNotice = ["API 密钥已签发", "请立即复制并安全保存；关闭后无法再次查看明文。", result.api_key];
        }
      }
    }
    $("resource-dialog").close();
    await refreshResourceCatalog(state.activeResource);
    await loadResource(state.activeResource);
    if (resultNotice) showResult(...resultNotice);
    if (state.activeResource === "channels" && createdResource?.type === "openclaw_weixin") {
      await openWeixinLogin(createdResource.id);
    }
  } catch (error) {
    $("resource-error").textContent = error.message;
  }
}

function statusBadge(enabled, yes = "已启用", no = "已停用") {
  return `<b class="status ${enabled ? "done" : "cancelled"}">${enabled ? yes : no}</b>`;
}

function channelStatusBadge(status) {
  const labels = {
    connected: ["done", "已连接"], binding: ["running", "等待扫码"],
    error: ["cancelled", "连接异常"], unbound: ["cancelled", "未连接"],
  };
  const [kind, label] = labels[status] || ["cancelled", "未连接"];
  return `<b class="status ${kind}">${label}</b>`;
}

function meta(label, value) {
  return `<span><small>${escapeHtml(label)}</small><strong>${escapeHtml(value ?? "—")}</strong></span>`;
}

function cardActions(actions) {
  const visible = actions.filter(Boolean);
  return visible.length ? `<div class="card-actions">${visible.join("")}</div>` : "";
}

function resourceCard(item, key) {
  const manageable = item.can_manage !== false;
  const edit = manageable ? `<button class="btn small ghost" data-resource-action="edit" data-id="${escapeHtml(item.id ?? item.key ?? "")}">编辑</button>` : "";
  const remove = manageable ? `<button class="btn small danger" data-resource-action="delete" data-id="${escapeHtml(item.id ?? item.key ?? "")}">删除</button>` : "";
  if (key === "capabilities") return `<article class="card resource-card ${item.enabled ? "" : "disabled"}">
    <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>${escapeHtml(item.group)} · ${item.mutating ? "有副作用" : "只读"}</span></div>${statusBadge(item.enabled)}</div>
    <p>${escapeHtml(item.description || "未填写用途说明")}</p>
    ${cardActions([`<button class="btn small ${item.enabled ? "danger" : ""}" data-resource-action="capability-toggle" data-id="${escapeHtml(item.name)}">${item.enabled ? "全局停用" : "全局启用"}</button>`])}
  </article>`;
  if (key === "knowledge") {
    const files = (item.files || []).map(file => `<span class="file-chip">${icon("paperclip", 12)}${escapeHtml(file)}
      ${manageable ? `<button data-resource-action="delete-file" data-id="${escapeHtml(item.key)}" data-file="${escapeHtml(file)}" aria-label="删除文档">×</button>` : ""}</span>`).join("");
    return `<article class="card resource-card">
      <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>${escapeHtml(item.key)} · ${manageable ? "我创建的" : "只读，可引用"}</span></div>${statusBadge(item.is_public, "公开", "私有")}</div>
      <div class="resource-stats">${meta("文档", `${(item.files || []).length} 个`)}${meta("类型", item.builtin ? "内置知识库" : "自建知识库")}</div>
      <div class="file-list">${files || '<span class="hint">尚未上传文档</span>'}</div>
      ${cardActions(manageable ? [
        `<button class="btn small" data-resource-action="upload" data-id="${escapeHtml(item.key)}">上传文档</button>`,
        `<button class="btn small ghost" data-resource-action="edit" data-id="${escapeHtml(item.key)}">可见性</button>`, remove,
      ] : [])}</article>`;
  }
  if (key === "mcp") return `<article class="card resource-card">
    <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>${escapeHtml((item.transport || "http").toUpperCase())} · v${item.version || 0}</span></div>${item.review_required ? '<b class="status cancelled">目录待复核</b>' : statusBadge(item.enabled)}</div>
    <p>${escapeHtml(item.description || "未填写用途说明")}</p>
    <div class="resource-stats">${meta("服务地址", item.url)}${meta("鉴权头", `${Object.keys(item.headers || {}).length} 项`)}${meta("风险策略", item.risk_policy === "read_only" ? "已审核只读" : "自动识别")}${meta("配置哈希", item.content_hash ? item.content_hash.slice(0, 12) : "未建立")}${meta("目录哈希", item.catalog_hash ? item.catalog_hash.slice(0, 12) : "未测试")}</div>
    ${cardActions(manageable ? [edit, `<button class="btn small ghost" data-resource-action="test" data-id="${item.id}">测试连接</button>`,
      item.review_required ? `<button class="btn small" data-resource-action="ack-catalog" data-id="${item.id}">确认目录变更</button>` : "",
      `<button class="btn small ghost" data-resource-action="versions" data-id="${item.id}">版本</button>`, `<button class="btn small ghost" data-resource-action="export-one" data-id="${item.id}">导出</button>`, remove] : [])}</article>`;
  if (key === "skills") return `<article class="card resource-card">
    <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>Skill · v${item.version || 0}</span></div>${statusBadge(item.enabled)}</div>
    <p>${escapeHtml(item.description || "未填写触发说明")}</p>
    <div class="resource-stats">${meta("指令", `${(item.instructions || "").length} 字`)}${meta("资源", `${(item.resources || []).length} 项`)}${meta("内容哈希", item.content_hash ? item.content_hash.slice(0, 12) : "未建立")}</div>
    ${cardActions(manageable ? [edit, `<button class="btn small ghost" data-resource-action="versions" data-id="${item.id}">版本</button>`, `<button class="btn small ghost" data-resource-action="export-one" data-id="${item.id}">导出 ZIP</button>`, remove] : [])}</article>`;
  if (key === "templates") return `<article class="card resource-card">
    <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>${escapeHtml((item.kind || "").toUpperCase())}</span></div>${statusBadge(item.enabled)}</div>
    <p>${escapeHtml(item.description || "未填写使用说明")}</p>
    <div class="resource-stats">${meta("源文件", item.has_file ? `已上传 ${item.ext}` : "未上传")}${meta("占位符", `${(item.placeholders || []).length} 个`)}</div>
    <div class="tag-list">${(item.placeholders || []).map(name => `<span class="tag">{{${escapeHtml(name)}}}</span>`).join("")}</div>
    ${cardActions(manageable ? [edit, item.has_file ? `<button class="btn small ghost" data-resource-action="download" data-id="${item.id}">下载源文件</button>` : "",
      `<button class="btn small ghost" data-resource-action="replace-file" data-id="${item.id}">${item.has_file ? "替换源文件" : "上传源文件"}</button>`,
      `<button class="btn small ghost" data-resource-action="export-one" data-id="${item.id}">导出模板包</button>`, remove] : [])}</article>`;
  if (key === "schedules") return `<article class="card resource-card">
    <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>${escapeHtml(item.cron)} · ${escapeHtml(item.timezone)}</span></div>${statusBadge(item.enabled)}</div>
    <p>${escapeHtml(item.query || "")}</p>
    <div class="resource-stats">${meta("执行智能体", `#${item.agent_id}`)}${meta("下次执行", formatAuditTime(item.next_run_at))}${meta("最近 Job", item.last_job_id || "尚未执行")}</div>
    ${cardActions([edit, remove])}</article>`;
  if (key === "providers") {
    const governance = state.providerGovernance.get(Number(item.id)) || {};
    const health = governance.health || {};
    const price = governance.price || null;
    const healthLabel = health.state === "healthy" ? "健康" : health.state === "unhealthy" ? "异常" : "待观测";
    return `<article class="card resource-card ${health.state === "unhealthy" ? "disabled" : ""}">
      <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>${item.provider_type === "chatgpt" ? "ChatGPT 订阅（Codex）" : item.provider_type === "anthropic" ? "Anthropic 兼容" : "OpenAI 兼容"} · ${escapeHtml(item.wire_api || "chat_completions")}</span></div>${statusBadge(item.enabled)}</div>
      <div class="resource-stats">${meta("模型", item.model_name || item.model_id || "未选择")}${meta("输入", (item.model_input || ["text"]).join(" + "))}${meta("健康", `${healthLabel}${health.samples ? ` · ${health.samples} 次` : ""}`)}${meta("平均延迟", health.average_latency_ms ? `${health.average_latency_ms} ms` : "—")}${meta("当前价格", price ? `输入 $${price.input_usd_per_million} / 输出 $${price.output_usd_per_million}` : "未定价")}${meta("对话选择", item.is_public ? "已开放" : "未开放")}</div>
      <p class="mono-line">${escapeHtml(item.base_url)}</p>
      ${cardActions(manageable ? [edit, `<button class="btn small ghost" data-resource-action="provider-price" data-id="${item.id}">价格版本</button>`, `<button class="btn small ghost" data-resource-action="test" data-id="${item.id}">测试连接</button>`, remove] : [])}</article>`;
  }
  if (key === "keys") return `<article class="card resource-card">
    <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>${escapeHtml(item.prefix)}…</span></div>${statusBadge(item.is_active, "有效", "已停用")}</div>
    <p>密钥明文已隐藏；如已遗失，请删除后重新签发。</p>
    ${cardActions([`<button class="btn small ghost" data-resource-action="toggle" data-id="${item.id}">${item.is_active ? "停用" : "启用"}</button>`, remove])}</article>`;
  if (key === "channels") {
    const personalWeixin = item.type === "openclaw_weixin";
    const connectAction = item.connection_status === "connected"
      ? `<button class="btn small danger" data-resource-action="disconnect-weixin" data-id="${item.id}">解绑微信</button>`
      : `<button class="btn small" data-resource-action="scan-weixin" data-id="${item.id}">扫码连接</button>`;
    return `<article class="card resource-card">
      <div class="card-title-row"><div><h3>${escapeHtml(item.name)}</h3><span>${personalWeixin ? "个人微信" : item.type === "wechat_mp" ? "微信公众号（兼容）" : "Webhook（兼容）"}</span></div>${personalWeixin ? channelStatusBadge(item.connection_status) : statusBadge(item.enabled)}</div>
      <div class="resource-stats">${meta("绑定智能体", item.agent_name || `#${item.agent_id}`)}${meta("独立空间", item.workspace_key || "—")}</div>
      <p class="mono-line">${escapeHtml(personalWeixin ? (item.account_id || item.last_error || "等待扫码绑定") : (item.webhook_url || item.webhook_path || ""))}</p>
      ${cardActions(personalWeixin ? [connectAction, edit, remove] : [edit, `<button class="btn small ghost" data-resource-action="copy-url" data-id="${item.id}">复制地址</button>`, remove])}</article>`;
  }
  if (key === "users") return `<article class="card resource-card">
    <div class="card-title-row"><div><h3>${escapeHtml(item.username)}</h3><span>${escapeHtml(item.role)}</span></div>${statusBadge(item.is_active, "正常", "已禁用")}</div>
    <div class="resource-stats">${meta("权限范围", item.role === "root" ? "全部模块" : item.all_modules ? "全部可用模块" : `${(item.modules || []).length} 个模块`)}</div>
    ${cardActions([edit, remove])}</article>`;
  return "";
}

function renderSystemSettings(item) {
  state.editingResource = item;
  const fields = resources.settings.fields.map(field => renderField(field, item, false)).join("");
  const logo = item.logo_url
    ? `<div class="settings-logo-preview"><img src="${escapeHtml(item.logo_url)}" alt="当前品牌 Logo" />
        <div><strong>当前 Logo</strong><span>${item.logo_is_custom ? "后台上传" : "品牌目录资源"}</span></div></div>`
    : `<div class="settings-logo-preview empty">${icon("panel", 22)}<div><strong>当前未配置 Logo</strong><span>系统使用默认标识</span></div></div>`;
  return `<section class="settings-panel">
    <div class="settings-panel-head">
      <div><h3>通用设置</h3><p>所有设置在当前页面直接修改，不使用配置文件导入或导出。</p></div>
      ${item.logo_url ? '<b class="status done">品牌标识已配置</b>' : ""}
    </div>
    ${logo}
    <div class="form-grid settings-form" id="settings-inline-fields">${fields}</div>
    <div class="error" id="settings-inline-error"></div>
    <div class="settings-save-row">
      <span class="hint">保存后系统名称和品牌标识会立即同步到前台。</span>
      <button type="button" class="btn" id="settings-inline-save">${icon("check", 15)}保存设置</button>
    </div>
  </section>`;
}

function formatTokens(value) {
  const count = Number(value || 0);
  if (count >= 1e8) return `${(count / 1e8).toFixed(count >= 1e9 ? 1 : 2)} 亿`;
  if (count >= 1e4) return `${(count / 1e4).toFixed(count >= 1e5 ? 1 : 2)} 万`;
  return count.toLocaleString("zh-CN");
}

const TOKEN_LIMIT_UNIT = 10_000;

function tokenLimitToWan(value) {
  const tokens = Math.max(0, Math.round(Number(value || 0)));
  const whole = Math.floor(tokens / TOKEN_LIMIT_UNIT);
  const remainder = tokens % TOKEN_LIMIT_UNIT;
  if (!remainder) return String(whole);
  return `${whole}.${String(remainder).padStart(4, "0").replace(/0+$/, "")}`;
}

function formatTokenLimitWan(value) {
  const amount = Number(tokenLimitToWan(value));
  return `${amount.toLocaleString("zh-CN", {maximumFractionDigits: 4})} 万`;
}

function tokenLimitFromWan(value) {
  const amount = Number(value || 0);
  const scaled = amount * TOKEN_LIMIT_UNIT;
  const tokens = Math.round(scaled);
  if (!Number.isFinite(amount) || amount < 0 || !Number.isSafeInteger(tokens)
      || Math.abs(scaled - tokens) > 0.000001) return null;
  return tokens;
}

function renderTokenHeatmap(daily) {
  const rows = (daily || []).slice(-182);
  const max = Math.max(1, ...rows.map(item => Number(item.total_tokens || 0)));
  return `<div class="token-heatmap" aria-label="Token 活动热力图">${rows.map(item => {
    const value = Number(item.total_tokens || 0);
    const level = value ? Math.max(1, Math.ceil((value / max) * 4)) : 0;
    return `<span class="token-cell level-${level}" title="${escapeHtml(item.date)} · ${formatTokens(value)} Tokens"></span>`;
  }).join("")}</div>`;
}

function renderTokenPeriod(period = {}) {
  const used = Number(period.used || 0);
  const limit = Number(period.limit || 0);
  const remaining = period.remaining == null ? null : Number(period.remaining || 0);
  return `<div class="token-period ${period.exceeded ? "exceeded" : ""}">
    <strong>${formatTokens(used)}</strong>
    <small>${limit ? `上限 ${formatTokenLimitWan(limit)} · 剩余 ${formatTokenLimitWan(remaining)}` : "不限额"}</small>
  </div>`;
}

function renderTokenUsage(data) {
  const totals = data?.totals || {};
  const users = data?.users || [];
  const userOptionsData = data?.user_options || users;
  const recent = data?.recent || [];
  const daily = data?.daily || [];
  const canManageUsage = Auth.role() === "root";
  const costs = data?.model_costs?.totals || {};
  const coverage = Number(costs.requests || 0)
    ? Math.round(Number(costs.priced_requests || 0) / Number(costs.requests) * 100)
    : 0;
  const selectedUser = String(state.tokenUserId || "");
  const maxDaily = Math.max(1, ...daily.map(item => Number(item.total_tokens || 0)));
  const trend = daily.slice(-60).map(item => {
    const height = Math.max(2, Math.round((Number(item.total_tokens || 0) / maxDaily) * 92));
    return `<span style="height:${height}px" title="${escapeHtml(item.date)} · ${formatTokens(item.total_tokens)} Tokens"></span>`;
  }).join("");
  const userOptions = userOptionsData.map(item =>
    `<option value="${item.user_id}" ${String(item.user_id) === selectedUser ? "selected" : ""}>${escapeHtml(item.username)}</option>`
  ).join("");
  const userRows = users.map((item, index) => `<tr>
    <td><span class="token-rank">${index + 1}</span></td>
    <td><strong>${escapeHtml(item.username)}</strong><small>${escapeHtml(item.role || "user")}</small></td>
    <td>${renderTokenPeriod(item.periods?.weekly)}</td>
    <td>${renderTokenPeriod(item.periods?.monthly)}</td>
    <td>${renderTokenPeriod(item.periods?.total)}</td>
    <td>${Number(item.requests || 0).toLocaleString("zh-CN")}</td>
    <td class="audit-time">${item.last_used_at ? formatAuditTime(item.last_used_at) : "—"}</td>
    ${canManageUsage ? `<td><div class="token-row-actions">
      <button type="button" class="btn small ghost token-row-action" data-token-action="configure-limits" data-user-id="${item.user_id}" data-username="${escapeHtml(item.username)}">${icon("settings", 14)}设置</button>
      <button type="button" class="btn small ghost token-row-action" data-token-action="reset-week" data-user-id="${item.user_id}" data-username="${escapeHtml(item.username)}">${icon("restore", 14)}重置本周</button>
      <button type="button" class="btn small ghost token-row-action" data-token-action="reset-month" data-user-id="${item.user_id}" data-username="${escapeHtml(item.username)}">${icon("restore", 14)}重置本月</button>
    </div></td>` : ""}
  </tr>`).join("");
  const recentRows = recent.map(item => `<tr>
    <td><strong>${escapeHtml(item.username)}</strong></td>
    <td><span class="tag">${escapeHtml(item.model || "默认模型")}</span></td>
    <td>${formatTokens(item.input_tokens)}</td><td>${formatTokens(item.output_tokens)}</td>
    <td><strong>${formatTokens(item.total_tokens)}</strong></td>
    <td class="audit-time">${formatAuditTime(item.created_at)}</td>
  </tr>`).join("");
  return `<div class="token-dashboard">
    <div class="token-toolbar">
      <div><strong>用量统计</strong><span>统计口径为上游模型返回的实际 usage 数据</span></div>
      <div>
        <label>用户<select id="token-user-filter"><option value="">全部用户</option>${userOptions}</select></label>
        <label>时间<select id="token-days-filter">
          <option value="7" ${state.tokenDays === 7 ? "selected" : ""}>近 7 天</option>
          <option value="30" ${state.tokenDays === 30 ? "selected" : ""}>近 30 天</option>
          <option value="90" ${state.tokenDays === 90 ? "selected" : ""}>近 90 天</option>
          <option value="365" ${state.tokenDays === 365 ? "selected" : ""}>近 1 年</option>
          <option value="0" ${state.tokenDays === 0 ? "selected" : ""}>全部时间</option>
        </select></label>
      </div>
    </div>
    <section class="token-metrics">
      <div><span>累计 Token</span><strong>${formatTokens(totals.total_tokens)}</strong><small>${Number(totals.requests || 0).toLocaleString("zh-CN")} 次模型请求</small></div>
      <div><span>输入 Token</span><strong>${formatTokens(totals.input_tokens)}</strong><small>含 ${formatTokens(totals.cached_tokens)} 缓存命中</small></div>
      <div><span>输出 Token</span><strong>${formatTokens(totals.output_tokens)}</strong><small>含 ${formatTokens(totals.reasoning_tokens)} 推理 Token</small></div>
      <div><span>活跃用户</span><strong>${Number(totals.active_users || 0).toLocaleString("zh-CN")}</strong><small>当前筛选范围内</small></div>
    </section>
    <section class="token-metrics cost-metrics">
      <div><span>估算成本</span><strong>$${escapeHtml(costs.estimated_usd ?? "0")}</strong><small>按生效价格版本估算，并非上游账单</small></div>
      <div><span>定价覆盖率</span><strong>${coverage}%</strong><small>${Number(costs.priced_requests || 0)} / ${Number(costs.requests || 0)} 次可估算</small></div>
      <div><span>未知成本请求</span><strong>${Number(costs.unknown_requests || 0).toLocaleString("zh-CN")}</strong><small>缺少对应模型价格版本</small></div>
      <div><span>成本口径</span><strong>版本化</strong><small>输入、输出、缓存、推理分别计价</small></div>
    </section>
    <section class="token-activity-card">
      <div class="token-card-head"><div><strong>Token 活动</strong><span>颜色越深表示当日用量越高</span></div></div>
      ${renderTokenHeatmap(daily)}
      <div class="token-trend" aria-label="每日 Token 趋势">${trend || '<div class="empty-state">暂无用量数据</div>'}</div>
    </section>
    <section class="token-table-card">
      <div class="token-card-head"><div><strong>用户消耗排行</strong><span>按累计 Token 从高到低排列</span></div><b>${users.length} 位用户</b></div>
      <div class="audit-table-wrap"><table class="audit-table token-user-table"><thead><tr>
        <th>#</th><th>用户</th><th>本周</th><th>本月</th><th>历史总量</th><th>筛选请求数</th><th>最近使用</th>${canManageUsage ? "<th>管理</th>" : ""}
      </tr></thead><tbody>${userRows || `<tr><td colspan="${canManageUsage ? 8 : 7}"><div class="empty-state">当前范围暂无用量记录</div></td></tr>`}</tbody></table></div>
    </section>
    <section class="token-table-card">
      <div class="token-card-head"><div><strong>最近调用</strong><span>展示最新 20 条模型响应记录</span></div></div>
      <div class="audit-table-wrap"><table class="audit-table token-recent-table"><thead><tr>
        <th>用户</th><th>模型</th><th>输入</th><th>输出</th><th>总量</th><th>时间</th>
      </tr></thead><tbody>${recentRows || '<tr><td colspan="6"><div class="empty-state">暂无调用记录</div></td></tr>'}</tbody></table></div>
    </section>
  </div>`;
}

function bindTokenUsageFilters() {
  $("token-days-filter").onchange = event => {
    state.tokenDays = Number(event.target.value);
    loadResource("token-usage");
  };
  $("token-user-filter").onchange = event => {
    state.tokenUserId = event.target.value;
    loadResource("token-usage");
  };
  $("resource-grid").onclick = handleTokenUsageAction;
}

async function handleTokenUsageAction(event) {
  const button = event.target.closest("[data-token-action]");
  if (!button || Auth.role() !== "root") return;
  const action = button.dataset.tokenAction;
  const username = button.dataset.username || "该用户";
  const userId = Number(button.dataset.userId);
  if (action === "configure-limits") {
    openTokenLimitDialog(userId);
    return;
  }
  if (action === "reset-week" && !confirm(`确定重置“${username}”的本周计费用量吗？历史 usage 明细会完整保留。`)) return;
  if (action === "reset-month" && !confirm(`确定重置“${username}”的本月计费用量吗？历史 usage 明细会完整保留。`)) return;
  button.disabled = true;
  try {
    if (action === "reset-week" || action === "reset-month") {
      const period = action === "reset-week" ? "week" : "month";
      await api(`/api/v1/token-usage/users/${userId}/reset-${period}`, {method: "POST"});
      showToast(`已重置 ${username} 的本${period === "week" ? "周" : "月"}计费用量，历史明细保持不变`);
    } else {
      return;
    }
    await loadResource("token-usage");
  } catch (error) {
    button.disabled = false;
    showToast(`操作失败：${error.message}`);
  }
}

function openTokenLimitDialog(userId) {
  const data = state.resourceRows[0] || {};
  const user = (data.users || []).find(item => Number(item.user_id) === Number(userId))
    || (data.user_options || []).find(item => Number(item.user_id) === Number(userId));
  if (!user) { showToast("用户不存在或已删除"); return; }
  state.tokenLimitUserId = Number(userId);
  $("token-limit-username").textContent = user.username;
  $("token-weekly-limit").value = tokenLimitToWan(user.periods?.weekly?.limit);
  $("token-monthly-limit").value = tokenLimitToWan(user.periods?.monthly?.limit);
  $("token-total-limit").value = tokenLimitToWan(user.periods?.total?.limit);
  $("token-limit-error").textContent = "";
  $("token-limit-dialog").showModal();
}

async function saveTokenLimits() {
  const button = $("token-limit-save");
  const errorBox = $("token-limit-error");
  const values = {
    weekly_limit: tokenLimitFromWan($("token-weekly-limit").value),
    monthly_limit: tokenLimitFromWan($("token-monthly-limit").value),
    total_limit: tokenLimitFromWan($("token-total-limit").value),
  };
  if (Object.values(values).some(value => value == null || value > 9_000_000_000_000_000)) {
    errorBox.textContent = "请输入大于或等于 0 的万 Token 数量，最多保留 4 位小数";
    return;
  }
  button.disabled = true;
  errorBox.textContent = "";
  try {
    await api(`/api/v1/token-usage/users/${state.tokenLimitUserId}/limits`, {
      method: "PUT", json: values,
    });
    $("token-limit-dialog").close();
    showToast("Token 用量限制已保存（单位：万 Token）");
    await loadResource("token-usage");
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function auditMethod(method) {
  return {
    POST: ["新建", "create"], PUT: ["更新", "update"],
    PATCH: ["修改", "update"], DELETE: ["删除", "delete"],
    GET: ["查看", "read"],
  }[String(method || "").toUpperCase()] || [method || "操作", "read"];
}

function formatAuditTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", {hour12: false});
}

function renderAuditList(rows, total = rows.length, page = 0, pageSize = 20) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(page + 1, totalPages);
  const start = total ? page * pageSize + 1 : 0;
  const end = Math.min((page + 1) * pageSize, total);
  return `<div class="audit-list-shell">
    <div class="audit-list-summary">共 ${Number(total).toLocaleString()} 条记录，当前显示 ${start}–${end} 条</div>
    ${rows.length ? `<div class="audit-table-wrap">
      <table class="audit-table">
        <thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>资源路径</th><th>状态</th><th>IP 地址</th></tr></thead>
        <tbody>${rows.map(item => {
          const [methodLabel, methodClass] = auditMethod(item.method);
          const ok = Number(item.status_code) < 400;
          return `<tr>
            <td class="audit-time">${escapeHtml(formatAuditTime(item.created_at))}</td>
            <td><strong>${escapeHtml(item.username || "未知")}</strong><small>${escapeHtml(item.role || "—")}</small></td>
            <td><span class="audit-method ${methodClass}">${escapeHtml(methodLabel)}</span><small>${escapeHtml(item.method || "")}</small></td>
            <td class="audit-path">${escapeHtml(item.path || "—")}</td>
            <td><b class="status ${ok ? "done" : "failed"}">${escapeHtml(item.status_code || "—")}</b></td>
            <td class="audit-ip">${escapeHtml(item.ip || "—")}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>` : '<div class="empty-state">暂无操作日志</div>'}
    <div class="audit-pagination">
      <label for="audit-page-size">每页
        <select id="audit-page-size">${[10, 20, 50, 100].map(size =>
          `<option value="${size}" ${size === pageSize ? "selected" : ""}>${size}</option>`).join("")}</select>
        条
      </label>
      <span>第 ${currentPage} / ${totalPages} 页</span>
      <div>
        <button type="button" class="btn small ghost" id="audit-prev" ${page <= 0 ? "disabled" : ""}>上一页</button>
        <button type="button" class="btn small ghost" id="audit-next" ${(page + 1) * pageSize >= total ? "disabled" : ""}>下一页</button>
      </div>
    </div>
  </div>`;
}

async function saveInlineSettings() {
  const button = $("settings-inline-save");
  const errorBox = $("settings-inline-error");
  errorBox.textContent = "";
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    await persistSystemSettings(collectResourcePayload());
    showToast("系统设置已保存");
    await loadResource("settings");
  } catch (error) {
    errorBox.textContent = error.message;
    button.disabled = false;
    button.innerHTML = `${icon("check", 15)}保存设置`;
  }
}

function archivedThreadRows(rows) {
  const threads = new Map();
  for (const row of rows) {
    if (!row.session_id) continue;
    const current = threads.get(row.session_id);
    if (!current) threads.set(row.session_id, {...row});
    else if (!current.title && row.title) current.title = row.title;
  }
  return [...threads.values()].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
}

function archiveThreadTitle(row) {
  return String(row.title || row.query || "新对话").trim().slice(0, 80) || "新对话";
}

function renderArchiveManagement() {
  const conversations = archivedThreadRows(state.archivedConversations);
  const projects = state.archivedProjects;
  const conversationRows = conversations.map(row => `
    <div class="archive-row">
      <div><strong>${escapeHtml(archiveThreadTitle(row))}</strong><span>${row.created_at ? escapeHtml(new Date(row.created_at).toLocaleDateString("zh-CN")) : ""}</span></div>
      <button class="archive-row-action" data-archive-action="restore-thread" data-id="${escapeHtml(row.session_id)}">${icon("restore", 15)}恢复</button>
      <button class="archive-row-action danger" data-archive-action="delete-thread" data-id="${escapeHtml(row.session_id)}">${icon("trash", 15)}删除</button>
    </div>`).join("") || '<div class="archive-empty">暂无归档对话</div>';
  const projectRows = projects.map(project => `
    <div class="archive-row">
      <div><strong>${escapeHtml(project.name)}</strong><span>${project.conversation_count || 0} 个对话</span></div>
      <button class="archive-row-action" data-archive-action="restore-project" data-id="${project.id}">${icon("restore", 15)}恢复</button>
      <button class="archive-row-action danger" data-archive-action="delete-project" data-id="${project.id}">${icon("trash", 15)}删除</button>
    </div>`).join("") || '<div class="archive-empty">暂无归档项目</div>';
  return `<div class="archive-management-grid">
    <section class="archive-settings">
      <div class="archive-settings-head"><div><strong>归档对话</strong><span>恢复后会重新显示在最近对话中。</span></div><b>${conversations.length} 条</b></div>
      <div class="archive-list">${conversationRows}</div>
    </section>
    <section class="archive-settings">
      <div class="archive-settings-head"><div><strong>归档项目</strong><span>恢复后，项目及其原有对话会重新显示。</span></div><b>${projects.length} 个</b></div>
      <div class="archive-list">${projectRows}</div>
    </section>
  </div>`;
}

async function loadArchiveManagement() {
  $("resource-grid").classList.add("list-layout");
  $("resource-grid").innerHTML = '<div class="archive-empty">正在加载…</div>';
  try {
    [state.archivedConversations, state.archivedProjects] = await Promise.all([
      api("/api/v1/chat/turns?archived=true"),
      api("/api/v1/projects?archived=true"),
    ]);
    state.resourceRows = [];
    $("resource-grid").innerHTML = renderArchiveManagement();
    $("resource-grid").onclick = handleArchiveAction;
  } catch (error) {
    $("resource-grid").innerHTML = `<div class="archive-empty">加载失败：${escapeHtml(error.message)}</div>`;
    $("resource-grid").onclick = null;
  }
}

async function handleArchiveAction(event) {
  const button = event.target.closest("[data-archive-action]");
  if (!button) return;
  const action = button.dataset.archiveAction;
  const id = button.dataset.id;
  if (action === "delete-thread" && !confirm("确定永久删除这条归档对话吗？此操作不可撤销。")) return;
  if (action === "delete-project" && !confirm("确定删除这个归档项目吗？项目内的对话会移至最近，此操作不可撤销。")) return;
  button.disabled = true;
  try {
    if (action === "restore-thread") {
      await api(`/api/v1/chat/threads/${encodeURIComponent(id)}`, {method: "PATCH", json: {archived: false}});
      showToast("对话已恢复");
    } else if (action === "delete-thread") {
      await api(`/api/v1/chat/threads/${encodeURIComponent(id)}`, {method: "DELETE"});
      showToast("归档对话已删除");
    } else if (action === "restore-project") {
      await api(`/api/v1/projects/${id}`, {method: "PATCH", json: {archived: false}});
      showToast("项目已恢复");
    } else if (action === "delete-project") {
      await api(`/api/v1/projects/${id}`, {method: "DELETE"});
      showToast("归档项目已删除，对话已移至最近");
    }
    await loadArchiveManagement();
  } catch (error) {
    button.disabled = false;
    showToast(`操作失败：${error.message}`);
  }
}

async function loadResource(key) {
  state.activeResource = key;
  const config = resources[key];
  $("resource-title").textContent = config.title;
  $("resource-copy").textContent = config.copy;
  $("resource-actions").hidden = !!config.directSettings;
  $("resource-create").hidden = !!config.readOnlyCreate || !!config.directSettings;
  $("resource-create-label").textContent = config.createLabel || "新建";
  $("resource-import").hidden = !(config.importLabel || config.importEndpoint || config.importType);
  $("resource-import-label").textContent = config.importLabel || "导入";
  $("resource-clear").hidden = !config.clearLabel;
  $("resource-clear").disabled = !!config.clearLabel;
  $("resource-clear-label").textContent = config.clearLabel || "清空";
  $("resource-export").hidden = !(config.exportLabel || config.exportEndpoint || config.exportType);
  $("resource-export-label").textContent = config.exportLabel || "导出";
  $("resource-import-input").accept = config.importAccept || "";
  if (config.archiveManagement) {
    await loadArchiveManagement();
    return;
  }
  const requestEndpoint = key === "audit"
    ? `${config.endpoint}?limit=${state.auditPageSize}&offset=${state.auditPage * state.auditPageSize}`
    : key === "token-usage"
      ? `${config.endpoint}?days=${state.tokenDays}${state.tokenUserId ? `&user_id=${encodeURIComponent(state.tokenUserId)}` : ""}`
    : config.endpoint;
  let response = await api(requestEndpoint);
  if (key === "token-usage") {
    try {
      response.model_costs = await api(
        `/api/v1/model-governance/costs?days=${state.tokenDays}${state.tokenUserId ? `&user_id=${encodeURIComponent(state.tokenUserId)}` : ""}`
      );
    } catch (_) {
      response.model_costs = null;
    }
  }
  if (key === "providers") {
    try {
      const governance = await api("/api/v1/model-governance/providers?lookback_minutes=60");
      state.providerGovernance = new Map(
        (governance?.items || []).map(item => [Number(item.provider_id), item])
      );
    } catch (_) {
      state.providerGovernance = new Map();
    }
  }
  let rows;
  if (key === "settings" || key === "token-usage") rows = [response || {}];
  else if (key === "audit") {
    state.auditTotal = Number(response?.total || 0);
    $("resource-clear").disabled = state.auditTotal === 0;
    const lastPage = Math.max(0, Math.ceil(state.auditTotal / state.auditPageSize) - 1);
    if (state.auditPage > lastPage) {
      state.auditPage = lastPage;
      return loadResource(key);
    }
    rows = response?.items || [];
  }
  else rows = Array.isArray(response) ? response : [];
  state.resourceRows = rows;
  if (key === "knowledge") {
    // Uploading changes a knowledge base. Keep the page-level shortcut aligned
    // with the per-card ownership controls instead of opening a form that will
    // inevitably fail with 403 for read-only users.
    $("resource-import").hidden = !rows.some(item => item.can_manage !== false);
  }
  $("resource-grid").classList.toggle("list-layout", ["audit", "settings", "token-usage"].includes(key));
  if (key === "settings") {
    $("resource-grid").innerHTML = renderSystemSettings(rows[0] || {});
    bindResourceForm($("settings-inline-fields"));
    $("settings-inline-save").onclick = saveInlineSettings;
    $("resource-grid").onclick = null;
  } else if (key === "token-usage") {
    $("resource-grid").innerHTML = renderTokenUsage(rows[0] || {});
    bindTokenUsageFilters();
  } else if (key === "audit") {
    $("resource-grid").innerHTML = renderAuditList(
      rows, state.auditTotal, state.auditPage, state.auditPageSize
    );
    $("audit-page-size").onchange = event => {
      state.auditPageSize = Number(event.target.value);
      state.auditPage = 0;
      loadResource("audit");
    };
    $("audit-prev").onclick = () => {
      if (state.auditPage <= 0) return;
      state.auditPage -= 1;
      loadResource("audit");
    };
    $("audit-next").onclick = () => {
      if ((state.auditPage + 1) * state.auditPageSize >= state.auditTotal) return;
      state.auditPage += 1;
      loadResource("audit");
    };
    $("resource-grid").onclick = null;
  } else {
    $("resource-grid").innerHTML = rows.map(item => resourceCard(item, key)).join("") || '<div class="empty-state">暂无数据</div>';
    $("resource-grid").onclick = handleResourceAction;
  }
}

function currentResourceItem(id) {
  return state.resourceRows.find(item =>
    String(item.id ?? item.key ?? item.name ?? "settings") === String(id)
  );
}

function showResult(title, copy, value) {
  $("result-title").textContent = title;
  $("result-copy").textContent = copy;
  $("result-value").textContent = value;
  $("result-dialog").showModal();
}

async function authorizedFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const response = await fetch(path, {...options, headers});
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response;
}

function downloadData(value, filename, type = "application/json;charset=utf-8") {
  const blob = value instanceof Blob ? value : new Blob([value], {type});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function downloadEndpoint(path, filename) {
  const response = await authorizedFetch(path);
  downloadData(await response.blob(), filename);
}

function csvCell(value) {
  const text = Array.isArray(value) ? value.join("|") : String(value ?? "");
  return `"${text.replace(/"/g, '""')}"`;
}

function csvText(headers, rows) {
  return "\ufeff" + [headers.map(csvCell).join(","), ...rows.map(row => headers.map(key => csvCell(row[key])).join(","))].join("\r\n");
}

function parseCsv(text) {
  const rows = [];
  let row = [], cell = "", quoted = false;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { cell += '"'; index++; }
      else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { row.push(cell); cell = ""; }
    else if (char === "\n") { row.push(cell.replace(/\r$/, "")); rows.push(row); row = []; cell = ""; }
    else cell += char;
  }
  if (cell || row.length) { row.push(cell.replace(/\r$/, "")); rows.push(row); }
  const headers = (rows.shift() || []).map(value => value.replace(/^\ufeff/, "").trim());
  return rows.filter(values => values.some(value => value.trim())).map(values =>
    Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""])));
}

async function importUsersCsv(file) {
  const rows = parseCsv(await file.text());
  if (!rows.length) throw new Error("CSV 中没有可导入的用户");
  const errors = [];
  let imported = 0;
  for (const [index, row] of rows.entries()) {
    try {
      const modules = String(row.modules || "").split("|").map(value => value.trim()).filter(Boolean);
      await api("/api/v1/users", {method: "POST", json: {
        username: String(row.username || "").trim(),
        password: String(row.password || ""),
        role: String(row.role || "user").trim(),
        all_modules: ["true", "1", "yes"].includes(String(row.all_modules || "").toLowerCase()),
        modules,
      }});
      imported++;
    } catch (error) {
      errors.push(`第 ${index + 2} 行：${error.message}`);
    }
  }
  if (errors.length) showResult(`已导入 ${imported} 个用户`, "部分行未能导入，请修正后重试。", errors.join("\n"));
  else showToast(`已导入 ${imported} 个用户`);
}

function openTemplateFileReplace(item) {
  state.resourceMode = "template-file";
  state.editingResource = item;
  $("resource-dialog-title").textContent = `替换 ${item.name} 的源文件`;
  $("resource-dialog-copy").textContent = "上传新源文件后会重新识别占位符；模板名称、权限和启用状态保持不变。";
  $("resource-fields").innerHTML = `<div class="form-field form-field-wide"><label for="resource-field-template-file">模板源文件</label>
    <input id="resource-field-template-file" type="file" accept=".docx,.md,.txt,.pptx,.xlsx" />
    <small class="field-help">支持 Word、PowerPoint、Excel、Markdown 和文本。</small></div>`;
  $("resource-save").textContent = "替换文件";
  $("resource-error").textContent = "";
  $("resource-dialog").showModal();
}

async function handleResourceAction(event) {
  const button = event.target.closest("[data-resource-action]");
  if (!button) return;
  const action = button.dataset.resourceAction;
  const item = currentResourceItem(button.dataset.id);
  const config = resources[state.activeResource];
  try {
    if (action === "edit") {
      await ensureResourceDependencies(state.activeResource);
      openResourceEditor(item);
    }
    else if (action === "provider-price") await openProviderPriceDialog(item);
    else if (action === "versions") {
      const result = await api(`${config.endpoint}/${item.id}/versions`);
      showResult(`${item.name} · 生命周期版本`, "版本记录仅含无凭据快照与内容哈希。", JSON.stringify(result, null, 2));
    }
    else if (action === "ack-catalog") {
      await api(`/api/v1/mcp-servers/${item.id}/acknowledge-catalog`, {method: "POST"});
      showToast("已确认当前 MCP 能力目录，可用于新任务");
      await loadResource("mcp");
    }
    else if (action === "scan-weixin") await openWeixinLogin(item.id);
    else if (action === "disconnect-weixin") {
      if (!confirm("确认解绑当前微信？已有网页对话会保留，但微信将停止收发消息。")) return;
      await api(`/api/v1/channels/${item.id}/disconnect`, {method: "POST"});
      showToast("微信已解绑");
      await loadResource("channels");
    }
    else if (action === "upload") openKnowledgeImport(item?.key);
    else if (action === "replace-file") openTemplateFileReplace(item);
    else if (action === "delete") {
      const deleteMessage = state.activeResource === "users"
        ? `确认删除用户“${resourceName(item)}”？该账号的运行产物将一并永久删除；其他关联业务数据仍需先转移或删除。此操作不可撤销。`
        : `确认删除“${resourceName(item)}”？此操作不可撤销。`;
      if (!confirm(deleteMessage)) return;
      const endpoint = state.activeResource === "knowledge"
        ? `/api/v1/knowledge/datasets/${encodeURIComponent(item.key)}`
        : `${config.endpoint}/${item.id}`;
      await api(endpoint, {method: "DELETE"});
      await loadResource(state.activeResource);
    } else if (action === "delete-file") {
      if (!confirm(`确认从知识库删除文档“${button.dataset.file}”？`)) return;
      await api(`/api/v1/knowledge/${encodeURIComponent(button.dataset.id)}/${encodeURIComponent(button.dataset.file)}`, {method: "DELETE"});
      await loadResource("knowledge");
    } else if (action === "test") {
      button.disabled = true;
      button.textContent = "测试中…";
      const result = await api(`${config.endpoint}/${item.id}/test`, {method: "POST"});
      showToast(result?.reply ? `连接成功：${result.reply}` : "连接测试成功");
      await loadResource(state.activeResource);
    } else if (action === "export-one") {
      if (state.activeResource === "skills") await downloadEndpoint(`/api/v1/skills/${item.id}/export`, `${item.name}.zip`);
      else if (state.activeResource === "templates") await downloadEndpoint(`/api/v1/templates/${item.id}/export`, `${item.name}.zip`);
      else await downloadEndpoint(`/api/v1/mcp-servers/export?ids=${item.id}`, `${item.name}.json`);
    } else if (action === "download") {
      await downloadEndpoint(`/api/v1/templates/${item.id}/download`, `${item.name}${item.ext || ""}`);
    } else if (action === "toggle") {
      await api(`${config.endpoint}/${item.id}`, {method: "PATCH"});
      await loadResource(state.activeResource);
    } else if (action === "capability-toggle") {
      await api(`/api/v1/capabilities/${encodeURIComponent(item.name)}`, {
        method: "PATCH", json: {enabled: !item.enabled},
      });
      await loadCatalogs();
      await loadResource("capabilities");
    } else if (action === "copy-url") {
      await copyText(item.webhook_url || item.webhook_path || "");
      showToast("接入地址已复制");
    }
  } catch (error) {
    showToast(error.message);
    if (button.isConnected) {
      button.disabled = false;
      if (action === "test") button.textContent = "测试连接";
    }
  }
}

async function openProviderPriceDialog(item) {
  state.providerPriceId = Number(item.id);
  $("provider-price-name").textContent = `${item.name} · ${item.model_id || "未选择模型"}`;
  $("provider-price-error").textContent = "";
  const response = await api(`/api/v1/model-governance/providers/${item.id}/prices`);
  const latest = (response?.items || []).at(-1) || {};
  $("provider-price-input").value = latest.input_usd_per_million ?? "";
  $("provider-price-output").value = latest.output_usd_per_million ?? "";
  $("provider-price-cached").value = latest.cached_usd_per_million ?? "";
  $("provider-price-reasoning").value = latest.reasoning_usd_per_million ?? "";
  $("provider-price-priced").checked = latest.priced !== false;
  $("provider-price-dialog").showModal();
}

async function saveProviderPrice() {
  const button = $("provider-price-save");
  button.disabled = true;
  $("provider-price-error").textContent = "";
  try {
    await api(`/api/v1/model-governance/providers/${state.providerPriceId}/pricing`, {
      method: "PUT",
      json: {
        input_usd_per_million: $("provider-price-input").value || 0,
        output_usd_per_million: $("provider-price-output").value || 0,
        cached_usd_per_million: $("provider-price-cached").value || 0,
        reasoning_usd_per_million: $("provider-price-reasoning").value || 0,
        priced: $("provider-price-priced").checked,
      },
    });
    $("provider-price-dialog").close();
    await loadResource("providers");
    showToast("已追加价格版本；历史价格保持不变");
  } catch (error) {
    $("provider-price-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function stopWeixinPolling() {
  if (state.weixinPollTimer) clearTimeout(state.weixinPollTimer);
  state.weixinPollTimer = null;
}

function renderWeixinLogin(result) {
  $("weixin-login-status").textContent = result.message || "等待扫码。";
  const image = $("weixin-qr-image");
  if (result.qr_data_url) {
    image.src = result.qr_data_url;
    image.hidden = false;
  } else {
    image.removeAttribute("src");
    image.hidden = true;
  }
  $("weixin-pair-code").hidden = !result.needs_pair_code;
  $("weixin-qr-refresh").hidden = !["expired", "error", "already_connected"].includes(result.status);
}

async function pollWeixinLogin() {
  stopWeixinPolling();
  if (!state.weixinChannelId || !$("weixin-login-dialog").open) return;
  try {
    const result = await api(`/api/v1/channels/${state.weixinChannelId}/login`);
    renderWeixinLogin(result);
    if (result.status === "connected") {
      showToast("个人微信已连接");
      await loadResource("channels");
      return;
    }
    if (["expired", "error", "already_connected"].includes(result.status)) return;
  } catch (error) {
    $("weixin-login-status").textContent = `状态查询失败：${error.message}`;
  }
  state.weixinPollTimer = setTimeout(pollWeixinLogin, 1500);
}

async function openWeixinLogin(channelId) {
  state.weixinChannelId = Number(channelId);
  stopWeixinPolling();
  $("weixin-qr-image").removeAttribute("src");
  $("weixin-qr-image").hidden = false;
  $("weixin-pair-code").hidden = true;
  $("weixin-qr-refresh").hidden = true;
  $("weixin-login-status").textContent = "正在生成安全二维码…";
  $("weixin-login-dialog").showModal();
  try {
    const result = await api(`/api/v1/channels/${channelId}/login`, {method: "POST"});
    renderWeixinLogin(result);
    state.weixinPollTimer = setTimeout(pollWeixinLogin, 1000);
  } catch (error) {
    renderWeixinLogin({status: "error", message: error.message});
  }
}

$("weixin-pair-code-submit").onclick = async () => {
  const code = $("weixin-pair-code-input").value.trim();
  if (!/^\d{1,12}$/.test(code)) return showToast("请输入手机显示的数字配对码");
  try {
    const result = await api(`/api/v1/channels/${state.weixinChannelId}/pair-code`, {method: "POST", json: {code}});
    renderWeixinLogin(result);
    state.weixinPollTimer = setTimeout(pollWeixinLogin, 500);
  } catch (error) { showToast(error.message); }
};

$("weixin-qr-refresh").onclick = () => openWeixinLogin(state.weixinChannelId);
$("weixin-login-dialog").addEventListener("close", stopWeixinPolling);

async function importResourceFile(file) {
  const config = resources[state.activeResource];
  if (config.importType === "users-csv") await importUsersCsv(file);
  else {
    const form = new FormData();
    form.append("file", file);
    const result = await api(config.importEndpoint, {method: "POST", body: form});
    showToast(result?.imported ? `已导入 ${result.imported} 项` : "导入完成");
  }
  await refreshResourceCatalog(state.activeResource);
  await loadResource(state.activeResource);
}

async function triggerResourceImport() {
  const config = resources[state.activeResource];
  if (state.activeResource === "knowledge") {
    openKnowledgeImport();
    return;
  }
  if (state.activeResource === "providers") {
    try {
      await api("/api/v1/providers/import-codex", {method: "POST"});
      await loadCatalogs();
      await loadResource("providers");
      showToast("已导入本机 Codex 登录");
    } catch (error) { showToast(error.message); }
    return;
  }
  $("resource-import-input").accept = config.importAccept || "";
  $("resource-import-input").click();
}

async function clearResource() {
  const key = state.activeResource;
  const config = resources[key];
  if (!config?.clearLabel || key !== "audit" || state.auditTotal === 0) return;
  if (!confirm(`确定清空全部 ${state.auditTotal.toLocaleString()} 条操作日志吗？此操作不可撤销。`)) return;

  const button = $("resource-clear");
  button.disabled = true;
  try {
    await api(config.endpoint, {method: "DELETE"});
    state.auditPage = 0;
    await loadResource(key);
    showToast("操作日志已清空");
  } catch (error) {
    button.disabled = false;
    showToast(`清空失败：${error.message}`);
  }
}

async function exportResource() {
  const key = state.activeResource;
  const config = resources[key];
  try {
    if (config.exportEndpoint) await downloadEndpoint(config.exportEndpoint, config.exportName || `${key}.json`);
    else if (config.exportType === "manifest") {
      const rows = state.resourceRows.map(item => ({
        key: item.key, name: item.name, visibility: item.is_public ? "public" : "private",
        builtin: item.builtin, files: (item.files || []).join("|"),
      }));
      downloadData(csvText(["key", "name", "visibility", "builtin", "files"], rows), "knowledge-manifest.csv", "text/csv;charset=utf-8");
    } else if (config.exportType === "safe-providers") {
      const rows = state.resourceRows.map(item => ({
        name: item.name, provider_type: item.provider_type, base_url: item.base_url,
        wire_api: item.wire_api, auth_type: item.auth_type, auth_header: item.auth_header,
        api_version: item.api_version, api_version_mode: item.api_version_mode,
        model_list_path: item.model_list_path, model_id: item.model_id,
        model_name: item.model_name, model_input: item.model_input,
        model_reasoning: item.model_reasoning, context_window: item.context_window,
        reasoning_effort: item.reasoning_effort, max_tokens: item.max_tokens,
        max_tokens_param: item.max_tokens_param,
        supports_temperature: item.supports_temperature, timeout_ms: item.timeout_ms,
        max_retries: item.max_retries, stream_max_retries: item.stream_max_retries,
        stream_idle_timeout_ms: item.stream_idle_timeout_ms, extra_body: item.extra_body,
        custom_header_names: Object.keys(item.custom_headers || {}), enabled: item.enabled,
      }));
      downloadData(JSON.stringify(rows, null, 2), "model-providers.safe.json");
    } else if (config.exportType === "users-csv") {
      downloadData(csvText(["username", "role", "is_active", "all_modules", "modules"], state.resourceRows), "users.safe.csv", "text/csv;charset=utf-8");
    } else if (config.exportType === "audit-csv") {
      downloadData(csvText(["id", "created_at", "username", "role", "method", "path", "status_code", "ip"], state.resourceRows), "audit-log.csv", "text/csv;charset=utf-8");
    }
  } catch (error) { showToast(error.message); }
}

async function importAgents(file) {
  const form = new FormData();
  form.append("file", file);
  const result = await api("/api/v1/agents/import", {method: "POST", body: form});
  showToast(`已导入 ${result.imported || 0} 个智能体`);
  await loadAgents();
}

async function exportAgents() {
  const ids = [...document.querySelectorAll("[data-agent-select]:checked")].map(input => input.value);
  if (!ids.length) { showToast("请先勾选要导出的智能体"); return; }
  try { await downloadEndpoint(`/api/v1/agents/export?ids=${ids.join(",")}`, "agents.json"); }
  catch (error) { showToast(error.message); }
}

function operationsMetric(label, value, detail = "") {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

function operationsResourceRows(type, items = []) {
  return items.slice(0, 12).map(item => `<tr>
    <td><strong>${escapeHtml(item.name || item.worker_id || item.scheduler_id || `#${item.id}`)}</strong></td>
    <td>${escapeHtml(item.connection_status || item.state || (item.active === false ? "离线" : item.enabled === false ? "停用" : "正常"))}</td>
    <td>${escapeHtml(item.last_error || item.model_id || item.risk_policy || item.last_seen || "—")}</td>
    <td>${item.id == null || !["provider", "mcp", "channel"].includes(type) ? "" : `<button class="btn small ${item.enabled ? "danger" : "ghost"}" data-operations-action="resource" data-resource-type="${type}" data-id="${item.id}" data-enabled="${item.enabled ? "1" : "0"}">${item.enabled ? "暂停" : "恢复"}</button>`}</td>
  </tr>`).join("");
}

function renderOperations(summary, failures) {
  const queue = summary.queue || {};
  const alerts = summary.alerts || [];
  const failureRows = (failures.items || []).map(item => `<tr>
    <td><strong>${escapeHtml(item.username || `#${item.owner_id || "—"}`)}</strong><small>${escapeHtml(item.agent_name || `Agent #${item.agent_id || "—"}`)}</small></td>
    <td><span class="status cancelled">${escapeHtml(item.status)}</span><small>${escapeHtml(item.error_class || "unclassified")}</small></td>
    <td>${escapeHtml(item.error || "未记录错误正文")}</td>
    <td>${Number(item.attempt_count || 0)} / ${Number(item.max_attempts || 0)}</td>
    <td class="audit-time">${formatAuditTime(item.updated_at)}</td>
    <td><div class="token-row-actions"><button class="btn small ghost" data-operations-action="detail" data-id="${item.id}">任务树</button><button class="btn small" data-operations-action="retry" data-id="${item.id}">复制重试</button></div></td>
  </tr>`).join("");
  const schedulerRows = (summary.schedulers?.items || []).map(item => `<tr>
    <td><strong>${escapeHtml(item.scheduler_id)}</strong></td><td>${item.active ? "活跃" : "离线"}</td>
    <td>${escapeHtml(item.last_error || "无错误")}</td><td>${item.last_error ? `<button class="btn small ghost" data-operations-action="ack" data-id="${escapeHtml(item.scheduler_id)}">确认错误</button>` : ""}</td>
  </tr>`).join("");
  return `<div class="operations-alerts">${alerts.map(item => `<div class="operations-alert ${escapeHtml(item.severity)}"><strong>${escapeHtml(item.severity === "critical" ? "严重" : "警告")}</strong><span>${escapeHtml(item.message)}</span></div>`).join("") || '<div class="operations-alert healthy"><strong>正常</strong><span>当前未触发运行告警</span></div>'}</div>
    <section class="token-metrics operations-metrics">
      ${operationsMetric("队列中", queue.in_flight || 0, `总计 ${queue.total || 0}`)}
      ${operationsMetric("死信", queue.dead_letter || 0, `最早等待 ${queue.oldest_pending_seconds || 0}s`)}
      ${operationsMetric("活跃 Worker", summary.workers?.active || 0, `容量 ${summary.workers?.capacity || 0}`)}
      ${operationsMetric("等待 P50 / P95", `${queue.wait_seconds?.p50 || 0}s / ${queue.wait_seconds?.p95 || 0}s`, `${queue.wait_seconds?.samples || 0} 个样本`)}
      ${operationsMetric("运行 P50 / P95", `${queue.run_seconds?.p50 || 0}s / ${queue.run_seconds?.p95 || 0}s`, `${queue.run_seconds?.samples || 0} 个样本`)}
      ${operationsMetric("工具失败", summary.tools?.failed || 0, `${summary.tools?.calls || 0} 次调用`)}
    </section>
    <section class="token-table-card"><div class="token-card-head"><div><strong>失败与死信任务</strong><span>复制重试会创建新 Job/Turn，原记录保持终态，审批重置为 ask</span></div><b>${failures.total || 0} 项</b></div>
      <div class="audit-table-wrap"><table class="audit-table"><thead><tr><th>主体</th><th>状态</th><th>脱敏错误</th><th>尝试</th><th>时间</th><th>操作</th></tr></thead><tbody>${failureRows || '<tr><td colspan="6"><div class="empty-state">当前没有失败或死信任务</div></td></tr>'}</tbody></table></div></section>
    <div class="operations-resource-grid">
      <section class="token-table-card"><div class="token-card-head"><div><strong>Provider</strong><span>暂停只影响新任务选择</span></div></div><div class="audit-table-wrap"><table class="audit-table"><tbody>${operationsResourceRows("provider", summary.providers?.items)}</tbody></table></div></section>
      <section class="token-table-card"><div class="token-card-head"><div><strong>MCP</strong><span>暂停后复制重试会过滤该能力</span></div></div><div class="audit-table-wrap"><table class="audit-table"><tbody>${operationsResourceRows("mcp", summary.mcp_servers?.items)}</tbody></table></div></section>
      <section class="token-table-card"><div class="token-card-head"><div><strong>消息渠道</strong><span>连接状态与脱敏错误</span></div></div><div class="audit-table-wrap"><table class="audit-table"><tbody>${operationsResourceRows("channel", summary.channels?.items)}</tbody></table></div></section>
      <section class="token-table-card"><div class="token-card-head"><div><strong>Scheduler</strong><span>确认只清当前心跳错误，不改历史任务</span></div></div><div class="audit-table-wrap"><table class="audit-table"><tbody>${schedulerRows || '<tr><td><div class="empty-state">暂无 Scheduler 心跳</div></td></tr>'}</tbody></table></div></section>
    </div>`;
}

async function loadOperations() {
  if (Auth.role() !== "root") return;
  $("operations-content").innerHTML = '<div class="empty-state">正在加载运行状态…</div>';
  try {
    const [summary, failures] = await Promise.all([
      api(`/api/v1/admin/operations/summary?hours=${state.operationsHours}`),
      api("/api/v1/admin/operations/failures?status=failed,dead_letter&limit=50"),
    ]);
    $("operations-content").innerHTML = renderOperations(summary, failures);
    $("operations-content").onclick = handleOperationsAction;
  } catch (error) {
    $("operations-content").innerHTML = `<div class="empty-state">运行状态加载失败：${escapeHtml(error.message)}</div>`;
  }
}

async function handleOperationsAction(event) {
  const button = event.target.closest("[data-operations-action]");
  if (!button) return;
  const action = button.dataset.operationsAction;
  button.disabled = true;
  try {
    if (action === "detail") {
      const result = await api(`/api/v1/admin/operations/jobs/${button.dataset.id}/tree`);
      showResult("任务执行树", "仅展示脱敏状态、归属和执行谱系，不包含原始 payload 或推理。", JSON.stringify(result, null, 2));
    } else if (action === "retry") {
      if (!confirm("确认复制为新任务重试？原失败事实保持不变，审批将重置为 ask。")) return;
      const result = await api(`/api/v1/admin/operations/jobs/${button.dataset.id}/retry-copy`, {method: "POST"});
      showToast(`已创建新任务 ${result.job?.id || ""}`);
      await loadOperations();
    } else if (action === "ack") {
      await api(`/api/v1/admin/operations/schedulers/${encodeURIComponent(button.dataset.id)}/ack-error`, {method: "POST"});
      await loadOperations();
    } else if (action === "resource") {
      const enabled = button.dataset.enabled !== "1";
      await api(`/api/v1/admin/operations/resources/${button.dataset.resourceType}/${button.dataset.id}/state`, {method: "PATCH", json: {enabled}});
      await loadOperations();
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

function showPanel(name) {
  const tab = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!tab || tab.style.display === "none") return;
  document.querySelectorAll(".tab").forEach(item => {
    const active = item.dataset.tab === name;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  if (window.matchMedia("(max-width: 760px)").matches) {
    tab.scrollIntoView({behavior: "smooth", block: "nearest", inline: "center"});
  }
  $("panel-agents").hidden = name !== "agents";
  $("panel-improvement").hidden = name !== "improvement";
  $("panel-operations").hidden = name !== "operations";
  $("panel-resource").hidden = ["agents", "improvement", "operations"].includes(name);
  if (name === "agents") loadAgents();
  else if (name === "improvement") loadImprovement();
  else if (name === "operations") loadOperations();
  else loadResource(name);
  localStorage.setItem("harness_admin_tab", name);
}

const tabModuleKey = tab => tab === "token-usage" ? "token_usage" : tab;
document.querySelectorAll(".root-only").forEach(el => el.style.display = Auth.role() === "root" ? "" : "none");
document.querySelectorAll(".tab:not(.root-only)").forEach(tab => {
  tab.style.display = Auth.canModule(tabModuleKey(tab.dataset.tab)) ? "" : "none";
});
document.querySelectorAll(".tab").forEach(tab => tab.onclick = () => showPanel(tab.dataset.tab));
document.querySelectorAll("[data-close]").forEach(button => button.onclick = () => $(button.dataset.close).close());
$("new-agent").onclick = () => openAgent();
$("agent-save").onclick = saveAgent;
$("provider-price-save").onclick = saveProviderPrice;
$("operations-refresh").onclick = loadOperations;
$("operations-hours").onchange = event => {
  state.operationsHours = Number(event.target.value || 24);
  loadOperations();
};
$("new-proposal").onclick = openProposal;
$("proposal-save").onclick = saveProposal;
$("resource-create").onclick = async () => {
  const button = $("resource-create");
  button.disabled = true;
  try {
    await ensureResourceDependencies(state.activeResource);
    openResourceEditor(resources[state.activeResource]?.singleton ? state.resourceRows[0] : null);
  } catch (error) {
    showToast(`加载智能体失败：${error.message}`);
  } finally {
    button.disabled = false;
  }
};
$("resource-save").onclick = saveResource;
$("resource-import").onclick = triggerResourceImport;
$("resource-clear").onclick = clearResource;
$("resource-export").onclick = exportResource;
$("resource-import-input").onchange = async event => {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  try { await importResourceFile(file); }
  catch (error) { showToast(error.message); }
};
$("agent-import").onclick = () => $("agent-import-input").click();
$("agent-import-input").onchange = async event => {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  try { await importAgents(file); }
  catch (error) { showToast(error.message); }
};
$("agent-export").onclick = exportAgents;
$("result-copy-button").onclick = async () => {
  await copyText($("result-value").textContent);
  showToast("已复制");
};
$("token-limit-save").onclick = saveTokenLimits;

hydrateIcons();
loadCatalogs().then(() => {
  const remembered = localStorage.getItem("harness_admin_tab");
  const visibleTabs = [...document.querySelectorAll(".tab")].filter(item => item.style.display !== "none");
  const tab = visibleTabs.find(item => item.dataset.tab === remembered) || visibleTabs[0];
  if (!tab) {
    $("agents-grid").innerHTML = '<div class="fatal-state">当前账号没有可访问的设置模块</div>';
    return;
  }
  showPanel(tab.dataset.tab);
}).catch(error => {
  $("agents-grid").innerHTML = `<div class="fatal-state">加载失败：${escapeHtml(error.message)}</div>`;
});
