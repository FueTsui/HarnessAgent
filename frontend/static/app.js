applyBranding();

// Account preferences provide defaults; composer choices apply to new turns only.
const APPROVAL_POLICIES = {
  ask: {label: "请求批准"},
  auto: {label: "帮我批准"},
  full_access: {label: "完全访问权限"},
};

const state = {
  sessionReady: false,
  agents: [],
  agentId: null,
  preferences: null,
  themeSaving: false,
  approvalPolicy: "ask",
  composerSelection: ChatWorkspace.newComposerSelection(),
  models: [],
  modelsLoading: false,
  modelRequest: 0,
  modelError: "",
  submitting: false,
  submissionToken: null,
  sessionId: null,
  runningJob: null,
  attachments: [],
  datasets: [],
  templates: [],
  skills: [],
  mcpServers: [],
  callableAgents: [],
  selectedDatasets: new Set(),
  selectedTemplates: new Set(),
  selectedSkills: new Set(),
  selectedMcp: new Set(),
  selectedAgentCalls: new Set(),
  paletteMode: null,
  paletteIndex: 0,
  activeTrigger: null,
  catalogLoadedAt: 0,
  catalogLoading: null,
  voiceRecognition: null,
  voiceListening: false,
  voiceBase: "",
  conversations: [],
  projects: [],
  activeProjectId: null,
  recentSortMode: "priority",
  contextProjectId: null,
  contextProjectPinned: false,
  contextSessionId: null,
  contextThreadTitle: "",
  contextThreadPinned: false,
  renameSessionId: null,
  activeJobs: [],
  agentStatuses: new Map(),
  agentSeen: null,
  agentStatusPollTimer: null,
  agentStatusPollInFlight: false,
  schedules: [],
  scheduleSeen: new Map(),
  schedulePollTimer: null,
  schedulePollInFlight: false,
  pageLeaving: false,
  followLatest: true,
  lastScrollTop: 0,
  runEventFilter: "all",
};

const $ = id => document.getElementById(id);
const log = $("chat-log");
const query = $("query");
const send = $("send-btn");
const conversationPane = document.querySelector(".conversation-pane");
const PASTED_TEXT_ATTACHMENT_THRESHOLD = 8000;
const attachmentPreviewUrls = new WeakMap();
let attachmentDragDepth = 0;

const CAPABILITY_LABELS = {
  "daily-official-writing": "日常写作",
};

function setSendState() {
  const canSend = Boolean(query.value.trim() || state.attachments.length);
  const stopping = Boolean(state.runningJob && !canSend);
  const hasModel = ChatWorkspace.modelAvailable(ChatWorkspace.selectedComposerModel(state.models, state.composerSelection));
  send.disabled = !state.sessionReady || !state.agentId || state.submitting
    || (!stopping && (state.modelsLoading || !hasModel));
  $("agent-selector").disabled = state.submitting;
  query.disabled = !state.sessionReady || state.submitting;
  ["approval-policy-btn", "model-btn", "add-menu-btn", "attach-input", "mic-btn"].forEach(id => {
    $(id).disabled = !state.sessionReady || state.submitting || (id === "model-btn" && state.modelsLoading);
  });
  setReasoningState();
  $("attach-chips").querySelectorAll("[data-file]").forEach(button => { button.disabled = state.submitting; });
  const mode = stopping ? "stop" : canSend ? "send" : "waveform";
  send.innerHTML = icon(mode, mode === "stop" ? 15 : 18);
  send.classList.toggle("stop", stopping);
  const hasStructuredContext = Boolean(
    state.attachments.length || state.selectedDatasets.size || state.selectedTemplates.size
    || state.selectedSkills.size || state.selectedMcp.size || state.selectedAgentCalls.size
  );
  const title = !state.sessionReady ? "请登录后使用问答" : stopping ? "停止生成" : !hasModel ? "暂无可用模型，请联系管理员" : canSend
    ? state.runningJob
      ? hasStructuredContext || hasComposerOverrides() ? "加入对话队列" : "添加到对话引导"
      : "发送"
    : "开始语音输入";
  send.title = title;
  send.setAttribute("aria-label", title);
}

function setReasoningState() {
  const current = ChatWorkspace.selectedComposerModel(state.models, state.composerSelection);
  const adjustable = ChatWorkspace.reasoningSlider(current, state.composerSelection).efforts.length > 0;
  const busy = Boolean(state.submitting || state.modelsLoading);
  $("reasoning-slider").disabled = busy || !adjustable;
  $("reasoning-reset").disabled = busy || !state.composerSelection?.reasoning_effort;
  $("reasoning-model-btn").disabled = busy;
}

function emptyComposerContext() {
  return {
    attachments: [], datasets: [], templates: [], skills: [], mcpServers: [], agentCalls: [],
  };
}

function hasStructuredComposerContext(context) {
  return Boolean(
    context.attachments.length || context.datasets.length || context.templates.length
    || context.skills.length || context.mcpServers.length || context.agentCalls.length
  );
}

function stagedMessageRows() {
  if (!state.runningJob || !state.sessionId) return [];
  const current = state.activeJobs.find(job => job.job_id === state.runningJob);
  const guidance = (current?.guidance || []).map(row => ({
    id: row.id,
    jobId: state.runningJob,
    kind: "guidance",
    text: row.content,
    label: "对话引导",
  }));
  const queued = state.activeJobs
    .filter(job => job.source !== "cron"
      && job.session_id === state.sessionId
      && job.job_id !== state.runningJob)
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at))
    .map((job, index) => ({
      id: job.job_id,
      jobId: job.job_id,
      kind: "queue",
      text: job.query,
      label: index ? `队列中 · ${index + 1}` : "队列中",
    }));
  return [...guidance, ...queued];
}

function renderStagedMessages() {
  const panel = $("message-staging");
  const rows = stagedMessageRows();
  panel.innerHTML = rows.map(row => `
    <div class="staged-message ${row.kind}" data-stage-id="${escapeHtml(row.id)}"
      data-stage-kind="${row.kind}">
      <span class="staged-grip" aria-hidden="true">${icon("menu", 14)}</span>
      <span class="staged-message-text" title="${escapeHtml(row.text)}">${escapeHtml(row.text)}</span>
      <button type="button" class="staged-mode" data-stage-change="${escapeHtml(row.id)}"
        title="${row.kind === "guidance" ? "当前为对话引导；点击加入队列" : "当前在队列中；点击改为对话引导"}"
        aria-label="${row.kind === "guidance" ? "对话引导，点击加入队列" : "队列消息，点击改为对话引导"}">
        ${row.kind === "guidance" ? icon("history", 13) : icon("clipboardList", 13)}
        <span>${escapeHtml(row.label)}</span>
      </button>
      <button type="button" class="staged-icon" data-stage-more="${escapeHtml(row.id)}"
        aria-label="更多消息操作" title="更多">•••</button>
      <div class="staged-message-menu" data-stage-menu="${escapeHtml(row.id)}" hidden>
        <button type="button" data-stage-edit="${escapeHtml(row.id)}">${icon("edit", 14)}编辑消息</button>
        <button type="button" data-stage-redirect="${escapeHtml(row.id)}">${icon("restore", 14)}设为当前目标</button>
        <button type="button" data-stage-new="${escapeHtml(row.id)}">${icon("panel", 14)}在新对话中发送</button>
        <button type="button" data-stage-change="${escapeHtml(row.id)}">
          ${row.kind === "guidance" ? icon("clipboardList", 14) : icon("history", 14)}
          ${row.kind === "guidance" ? "加入队列" : "改为引导"}
        </button>
        <button type="button" class="danger" data-stage-delete="${escapeHtml(row.id)}">
          ${icon("trash", 14)}移除消息
        </button>
      </div>
    </div>`).join("");
  panel.classList.toggle("active", Boolean(rows.length));
  panel.querySelectorAll("[data-stage-more]").forEach(button => {
    button.onclick = event => {
      event.stopPropagation();
      const menu = panel.querySelector(`[data-stage-menu="${button.dataset.stageMore}"]`);
      panel.querySelectorAll(".staged-message-menu").forEach(item => {
        if (item !== menu) item.hidden = true;
      });
      menu.hidden = !menu.hidden;
    };
  });
  panel.querySelectorAll("[data-stage-delete]").forEach(button => {
    button.onclick = () => removeStagedMessage(button.dataset.stageDelete);
  });
  panel.querySelectorAll("[data-stage-edit]").forEach(button => {
    button.onclick = () => editStagedMessage(button.dataset.stageEdit);
  });
  panel.querySelectorAll("[data-stage-new]").forEach(button => {
    button.onclick = () => moveStagedMessage(button.dataset.stageNew, "new");
  });
  panel.querySelectorAll("[data-stage-redirect]").forEach(button => {
    button.onclick = () => moveStagedMessage(button.dataset.stageRedirect, "redirect");
  });
  panel.querySelectorAll("[data-stage-change]").forEach(button => {
    button.onclick = () => moveStagedMessage(button.dataset.stageChange, "toggle");
  });
}

function stagedMessageById(id) {
  return stagedMessageRows().find(row => row.id === id);
}

async function handleStagedActionFailure(row, error, prefix) {
  try {
    await loadActiveJobs();
  } catch (_) {
    showToast(`${prefix}：${error.message}`);
    return;
  }
  const accepted = row.kind === "guidance"
    && error.status === 409
    && !stagedMessageById(row.id);
  showToast(accepted ? "引导已送达当前任务" : `${prefix}：${error.message}`);
}

async function discardStagedMessage(row) {
  if (row.kind === "guidance") {
    await api(`/api/v1/chat/turns/${row.jobId}/guidance/${row.id}`, {method: "DELETE"});
    const active = state.activeJobs.find(job => job.job_id === row.jobId);
    if (active) active.guidance = (active.guidance || []).filter(item => item.id !== row.id);
  } else {
    await api(`/api/v1/chat/turns/${row.jobId}/cancel`, {method: "POST"});
    state.activeJobs = state.activeJobs.filter(job => job.job_id !== row.jobId);
  }
  renderStagedMessages();
  renderProjects();
  renderHistory();
}

async function removeStagedMessage(id) {
  const row = stagedMessageById(id);
  if (!row) return;
  try {
    await discardStagedMessage(row);
  } catch (error) {
    await handleStagedActionFailure(row, error, "无法移除");
  }
}

async function editStagedMessage(id) {
  const row = stagedMessageById(id);
  if (!row) return;
  const content = window.prompt("编辑消息", row.text);
  if (content === null || content.trim() === row.text.trim()) return;
  try {
    await api(`/api/v1/chat/staged/${row.kind}/${row.id}`, {
      method: "PATCH",
      json: {content, job_id: row.jobId},
    });
    await loadActiveJobs();
    showToast("消息已更新");
  } catch (error) {
    await handleStagedActionFailure(row, error, "无法编辑");
  }
}

async function moveStagedMessage(id, target) {
  const row = stagedMessageById(id);
  if (!row) return;
  try {
    const transformTarget = ["new", "redirect"].includes(target)
      ? target
      : row.kind === "guidance" ? "queue" : "guidance";
    if (transformTarget === "guidance") {
      const queued = state.activeJobs.find(job => job.job_id === row.jobId);
      const active = state.activeJobs.find(job => job.job_id === state.runningJob);
      if (!ChatWorkspace.sameTurnOptions(active, queued)) {
        showToast("该消息使用不同设置，保留为下一轮任务");
        return;
      }
    }
    await api(`/api/v1/chat/staged/${row.kind}/${row.id}/transform`, {
      method: "POST",
      json: {
        target: transformTarget,
        job_id: row.jobId,
        target_job_id: state.runningJob,
      },
    });
    await loadActiveJobs();
    showToast(transformTarget === "redirect"
      ? "已设为当前目标，正在停止旧目标"
      : transformTarget === "new" ? "已移动到新对话"
      : transformTarget === "queue" ? "已加入队列" : "已改为引导当前任务");
  } catch (error) {
    await handleStagedActionFailure(row, error, "操作未完成");
  }
}

function capabilityLabel(item, fallback) {
  const name = String(item?.display_name || item?.name || fallback || "");
  return CAPABILITY_LABELS[name] || name;
}

function submittedContextItems(context) {
  if (!context) return {references: [], commands: []};
  const references = [
    ...(context.attachments || []).map(file => ({
      iconName: "fileText", label: file.name, kind: "attachment",
      url: file.url || "", inherited: Boolean(file.inherited),
    })),
    ...(context.datasets || []).map(id => ({
      iconName: "database",
      label: state.datasets.find(item => (item.key || item.id) == id)?.name || id,
      kind: "reference",
    })),
    ...(context.templates || []).map(id => ({
      iconName: "fileText",
      label: state.templates.find(item => item.id == id)?.name || id,
      kind: "reference",
    })),
  ];
  const commands = [
    ...(context.skills || []).map(id => {
      const item = state.skills.find(skill => skill.id == id);
      return {iconName: "package", label: capabilityLabel(item, id)};
    }),
    ...(context.mcpServers || []).map(id => {
      const item = state.mcpServers.find(server => server.id == id);
      return {iconName: "plug", label: capabilityLabel(item, id)};
    }),
    ...(context.agentCalls || []).map(id => {
      const item = state.callableAgents.find(agent => agent.id == id);
      return {iconName: "bot", label: capabilityLabel(item, id)};
    }),
  ];
  return {references, commands};
}

function renderSubmittedReferences(items) {
  if (!items.length) return "";
  return `<div class="submitted-context">${items.map(item => {
    const tag = item.url ? "a" : "span";
    const href = item.url ? ` href="${escapeHtml(String(item.url))}" download` : "";
    const inherited = item.inherited ? " inherited" : "";
    return `
    <${tag}${href} class="submitted-reference ${item.kind}${inherited}"
      title="${item.inherited ? "沿用本任务前序附件 · " : ""}${escapeHtml(String(item.label))}">
      ${icon(item.iconName, 14)}<span>${escapeHtml(String(item.label))}</span>
    </${tag}>`;
  }).join("")}</div>`;
}

function renderUserMessageContent(content, commands) {
  const prompt = `<span class="submitted-prompt">${escapeHtml(content || "").replace(/\n/g, "<br>")}</span>`;
  if (!commands.length) return prompt;
  return `<span class="submitted-invocations">${commands.map(item => `
    <span class="submitted-invocation">${icon(item.iconName, 14)}<strong>${escapeHtml(String(item.label))}</strong></span>`).join("")}</span>${prompt}`;
}

function initials(name) {
  return (name || "U").trim().slice(0, 1).toUpperCase();
}

function setWelcome(show) {
  const el = $("welcome");
  if (el) el.style.display = show ? "" : "none";
}

function resizeComposer() {
  query.style.height = "auto";
  query.style.height = Math.min(query.scrollHeight, 220) + "px";
  saveComposerDraft();
  setSendState();
}

function addMessage(role, content, options = {}) {
  setWelcome(false);
  const article = document.createElement("article");
  article.className = `message ${role}`;
  article._messageText = content || "";
  const avatar = role === "user" ? initials(Auth.username()) : icon("sparkles", 16);
  const submitted = role === "user"
    ? submittedContextItems(options.context)
    : {references: [], commands: []};
  article.innerHTML = `
    <div class="message-avatar">${avatar}</div>
    <div class="message-body">
      <div class="message-name">${role === "user" ? "你" : escapeHtml(activeAgent()?.name || "智能体")}</div>
      ${role === "assistant" ? '<div class="run-phase-inline" hidden></div><div class="run-approval-slot"></div>' : ""}
      ${role === "user" ? renderSubmittedReferences(submitted.references) : ""}
      ${role === "assistant"
        ? '<div class="message-answer-state" role="status" aria-live="polite" hidden></div>'
        : ""}
      <div class="message-content">${role === "assistant"
        ? renderMarkdown(content || "")
        : renderUserMessageContent(content, submitted.commands)}</div>
      <div class="message-meta">${options.meta || ""}</div>
      ${role === "assistant" ? '<div class="agent-work-slot"></div>' : ""}
      ${role === "assistant" ? `<div class="message-actions">
        <button type="button" data-copy-message aria-label="复制回答">${icon("copy", 15)}<span>复制</span></button>
      </div>` : ""}
    </div>`;
  log.appendChild(article);
  article.querySelector("[data-copy-message]")?.addEventListener("click", async event => {
    await copyText(article._messageText || article.querySelector(".message-content").textContent);
    const button = event.currentTarget;
    button.innerHTML = `${icon("check", 15)}<span>已复制</span>`;
    setTimeout(() => { button.innerHTML = `${icon("copy", 15)}<span>复制</span>`; }, 1400);
  });
  scrollToLatest();
  return article;
}

function scrollToLatest(force = false) {
  if (force) state.followLatest = true;
  if (state.followLatest) {
    log.scrollTop = log.scrollHeight;
    state.lastScrollTop = log.scrollTop;
  }
  $("jump-to-latest").hidden = state.followLatest || RunWorkspace.nearLatest(log);
}

log.addEventListener("scroll", () => {
  const nearLatest = RunWorkspace.nearLatest(log);
  // New content can increase scrollHeight before a queued scroll event runs.
  // Only an actual upward movement opts out of following; layout growth does not.
  if (log.scrollTop < state.lastScrollTop - 2) state.followLatest = nearLatest;
  else if (nearLatest) state.followLatest = true;
  state.lastScrollTop = log.scrollTop;
  $("jump-to-latest").hidden = state.followLatest;
}, {passive: true});
$("jump-to-latest").onclick = () => scrollToLatest(true);
// Streaming text, plan updates and loaded images can all change message height.
const conversationResizeObserver = new ResizeObserver(() => scrollToLatest());
conversationResizeObserver.observe(log);
const conversationMutationObserver = new MutationObserver(() => scrollToLatest());
conversationMutationObserver.observe(log, {childList: true, subtree: true, characterData: true});
log.addEventListener("load", () => scrollToLatest(), true);

function setAnswerDeliveryState(article, status = "") {
  const indicator = article?.querySelector?.(".message-answer-state");
  const content = article?.querySelector?.(".message-content");
  if (!indicator || !content) return;
  const copy = {
    streaming: "正在生成，当前内容尚未最终确认",
    finalizing: "正在核验，当前内容尚未最终确认",
    failed: "生成未完成，以上内容未最终确认",
    cancelled: "生成已停止，以上内容未最终确认",
  }[status] || "";
  article.dataset.answerState = status || "confirmed";
  indicator.textContent = copy;
  indicator.hidden = !copy;
  content.setAttribute("aria-busy", String(["streaming", "finalizing"].includes(status)));
}

function setMessageAttachmentContext(article, attachments) {
  if (!article || !Array.isArray(attachments) || !attachments.length) return;
  const body = article.querySelector(".message-body");
  const content = article.querySelector(".message-content");
  if (!body || !content || body.querySelector(".submitted-context")) return;
  const references = submittedContextItems({attachments}).references;
  content.insertAdjacentHTML("beforebegin", renderSubmittedReferences(references));
}

function formatElapsed(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

function summarizeTaskGoal(text) {
  const value = String(text || "")
    .replace(/\s+/g, " ")
    .replace(/^(请|麻烦|帮我|请帮我)\s*/i, "")
    .trim();
  if (!value) return "分析当前附件";
  return value;
}

function buildTaskProgressSteps(objective = "") {
  return [
    {
      node: "task_1",
      title: summarizePlanStep(objective, "完成当前任务"),
      detail: "正在执行当前任务",
      status: "current",
    },
  ];
}

function summarizePlanStep(text, fallback) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  if (!value) return fallback;
  return value.length > 34 ? `${value.slice(0, 33)}…` : value;
}

/* AGENT_WORK_STATE_HELPERS_START */
function agentWorkPlanSnapshot(steps = []) {
  const rows = Array.isArray(steps) ? steps : [];
  const normalized = rows.map(step => ({
    ...step,
    status: {
      completed: "done",
      in_progress: "current",
    }[step?.status] || String(step?.status || "pending"),
  }));
  const counts = {
    total: normalized.length,
    done: 0,
    current: 0,
    pending: 0,
    failed: 0,
    blocked: 0,
    skipped: 0,
  };
  normalized.forEach(step => {
    const status = Object.hasOwn(counts, step.status) ? step.status : "pending";
    counts[status] += 1;
  });
  let activeIndex = normalized.findLastIndex(step => step.status === "current");
  if (activeIndex < 0) activeIndex = normalized.findIndex(step => step.status === "pending");
  if (activeIndex < 0) {
    activeIndex = normalized.findLastIndex(step => ["failed", "blocked"].includes(step.status));
  }
  if (activeIndex < 0) activeIndex = normalized.findLastIndex(step => step.status === "done");
  if (activeIndex < 0) activeIndex = normalized.findLastIndex(step => step.status === "skipped");
  if (activeIndex < 0) activeIndex = 0;
  const terminalized = counts.total > 0 && counts.current + counts.pending === 0;
  const allCompleted = terminalized && counts.done === counts.total;
  const hasIssues = counts.failed + counts.blocked + counts.skipped > 0;
  let label = counts.total ? `第 ${activeIndex + 1} / ${counts.total} 步` : "暂无任务步骤";
  if (allCompleted) {
    label = `计划已完成：${counts.done} / ${counts.total}`;
  } else if (terminalized) {
    const parts = [
      counts.done ? `${counts.done} 完成` : "",
      counts.failed ? `${counts.failed} 失败` : "",
      counts.blocked ? `${counts.blocked} 阻塞` : "",
      counts.skipped ? `${counts.skipped} 跳过` : "",
    ].filter(Boolean);
    label = `计划已收尾：${parts.join("、") || "无完成步骤"}`;
  }
  return {counts, activeIndex, terminalized, allCompleted, hasIssues, label};
}

function agentWorkStepIndex(work, stepId = "") {
  const steps = Array.isArray(work?.steps) ? work.steps : [];
  const requested = String(stepId || "");
  if (requested) {
    const matched = steps.findIndex(step => String(step?.node || "") === requested);
    if (matched >= 0) return matched;
  }
  return agentWorkPlanSnapshot(steps).activeIndex;
}

function recordAgentWorkActivityState(work, activity = {}) {
  if (!work) return null;
  const clean = String(activity.text || "").trim();
  if (!clean) return null;
  const stageIndex = agentWorkStepIndex(work, activity.stepId);
  const items = work.activities[stageIndex] || (work.activities[stageIndex] = []);
  const key = String(activity.key || `${activity.kind || "progress"}:${clean}`);
  const existing = work.activities.flat().findLast(item => item.key === key);
  if (existing) {
    if (activity.increment) existing.count += 1;
    existing.text = clean.length > 88 ? `${clean.slice(0, 87)}…` : clean;
    existing.kind = activity.kind || "progress";
    return existing;
  }
  const created = {
    key,
    text: clean.length > 88 ? `${clean.slice(0, 87)}…` : clean,
    kind: activity.kind || "progress",
    count: 1,
  };
  items.push(created);
  if (items.length > 5) items.splice(0, items.length - 5);
  return created;
}

function runtimeActivityIdentity(eventType, payload = {}) {
  if (["model.role.selected", "model.role.fallback"].includes(eventType)) {
    const role = ["executor", "planner", "router", "critic"].includes(payload.role) ? payload.role : "unknown";
    return `${eventType}:${role}`;
  }
  if (eventType === "tools.selection") {
    const decision = ["accepted", "invalid_selection", "low_confidence", "model_unavailable"].includes(payload.decision)
      ? payload.decision : "unknown";
    return `${eventType}:${decision}`;
  }
  const terminalStatus = String(
    payload.completion_status
    || payload.status
    || {
      "task.completed": "completed",
      "task.completed_with_issues": "completed_with_issues",
      "task.failed": "failed",
      "task.cancelled": "cancelled",
      "loop.completed": "completed",
      "turn.completed": "completed",
    }[eventType]
    || ""
  );
  if (
    ["loop.completed", "turn.completed"].includes(eventType)
    || (eventType.startsWith("task.")
      && ["completed", "completed_with_issues", "failed", "cancelled"].includes(terminalStatus))
  ) {
    // task.status -> loop.completed -> task.completed* 是同一业务终态的不同审计层。
    // 主卡只保留一条可更新活动，底层 Item 仍全部保留在数据库中。
    return "terminal-outcome";
  }
  const stepId = String(payload.step_id || "");
  if (stepId) return `step:${stepId}:${eventType}`;
  const node = String(payload.node || "");
  if (node) return `graph:${node}`;
  const tool = String(payload.tool || "");
  if (tool) return `tool:${tool}`;
  return String(eventType || "runtime");
}

function runtimeStopReasonText(reason) {
  return {
    successful_tool_budget: "已达到本轮业务工具调用预算，转入结果整理",
    repeated_tool_call: "检测到重复工具调用，已停止继续执行",
    max_iterations: "已达到本轮循环次数上限",
    verification_failed: "完成条件校验未通过",
    assistant_message: "模型已转入结果整理阶段",
  }[String(reason || "")] || `执行循环已停止${reason ? `（${reason}）` : ""}`;
}

function agentWorkCompletionState(work = {}) {
  const reported = String(work.completionStatus || work.taskStatus || "");
  if (["completed_with_issues", "blocked", "partial"].includes(reported)) {
    return "completed_with_issues";
  }
  if (agentWorkPlanSnapshot(work.steps).hasIssues) return "completed_with_issues";
  return reported || "completed";
}

function isTerminalConversationStatus(status) {
  return ["completed", "completed_with_issues", "failed"].includes(
    String(status || "").toLowerCase()
  );
}
/* AGENT_WORK_STATE_HELPERS_END */

function fallbackProgressPhaseForText(text) {
  const value = String(text || "");
  if (/结果已|保存|已完成/.test(value)) return "finish";
  if (/验证|修订|整理最终/.test(value)) return /修订/.test(value) ? "revise" : "verify";
  if (/记忆|历史|上下文/.test(value)) return "resolve_memory";
  if (/路由|能力目录/.test(value)) return "route";
  if (/Turn 已创建|已排队|理解目标|准备任务|恢复任务/.test(value)) return "intake";
  return "agent_loop";
}

function currentAgentWorkIndex(work) {
  return agentWorkPlanSnapshot(work.steps).activeIndex;
}

function agentWorkStepMarkup(work, index) {
  const step = work.steps[index];
  let stateName = step.status;
  if (stateName === "current" && work.status === "failed") stateName = "failed";
  if (stateName === "current" && work.status === "cancelled") stateName = "cancelled";
  return `<div class="agent-work-step ${stateName}" role="listitem"
    data-plan-step="${escapeHtml(step.node)}"
    aria-label="${escapeHtml(step.title)}">
    <i aria-hidden="true"></i>
    <span>${escapeHtml(step.title)}</span>
  </div>`;
}

function updateAgentWorkStepElement(element, work, index) {
  const step = work.steps[index];
  let stateName = step.status;
  if (stateName === "current" && work.status === "failed") stateName = "failed";
  if (stateName === "current" && work.status === "cancelled") stateName = "cancelled";
  element.className = `agent-work-step ${stateName}`;
  element.dataset.planStep = step.node;
  element.setAttribute("aria-label", step.title);
  element.querySelector("span").textContent = step.title;
}

let progressDockSequence = 0;

function syncTurnProgressDock(article) {
  const work = article?._agentWork;
  const dock = $("turn-progress-dock");
  if (!work || !dock) return;
  if (!work.dockId) work.dockId = `turn-progress-${++progressDockSequence}`;
  if (work.status !== "running" || !work.planAware) {
    if (dock.dataset.owner === work.dockId) {
      dock.hidden = true;
      dock.dataset.owner = "";
    }
    return;
  }
  dock.dataset.owner = work.dockId;
  dock.hidden = false;
  dock.classList.toggle("collapsed", !work.dockExpanded);
  const plan = agentWorkPlanSnapshot(work.steps);
  dock.classList.toggle("terminalized", plan.terminalized);
  dock.classList.toggle("has-issues", plan.hasIssues);
  const stepsPanel = $("turn-progress-steps");
  const existingByStepId = new Map(
    [...stepsPanel.children].map(element => [element.dataset.planStep, element]),
  );
  work.steps.forEach((step, index) => {
    let element = existingByStepId.get(step.node);
    if (element) {
      existingByStepId.delete(step.node);
    } else {
      const template = document.createElement("template");
      template.innerHTML = agentWorkStepMarkup(work, index).trim();
      element = template.content.firstElementChild;
    }
    updateAgentWorkStepElement(element, work, index);
    stepsPanel.appendChild(element);
  });
  existingByStepId.forEach(element => element.remove());
  $("turn-progress-count").textContent = plan.label;
  $("turn-progress-card").setAttribute(
    "aria-label",
    plan.terminalized
      ? plan.label
      : `任务进度，已完成 ${plan.counts.done} / ${plan.counts.total} 步`,
  );
  const toggle = $("turn-progress-toggle");
  toggle.setAttribute("aria-expanded", String(work.dockExpanded));
  toggle.setAttribute(
    "aria-label",
    `${work.dockExpanded ? "收起" : "展开"}任务进度，${plan.label}`,
  );
  toggle.title = work.dockExpanded ? "移开鼠标收起任务进度" : "悬停查看任务进度";
}

function agentWorkActivityMarkup(activity) {
  const count = activity.count > 1 ? `<em>×${activity.count}</em>` : "";
  return `<div class="agent-work-activity ${activity.kind}">
    <i aria-hidden="true"></i>
    <span>${escapeHtml(activity.text)}</span>
    ${count}
  </div>`;
}

function agentWorkActivitiesMarkup(work) {
  return work.activities.map((items, index) => {
    if (!items.length) return "";
    return `<section class="agent-work-activity-group">
      <div class="agent-work-activity-title">
        <span>第 ${index + 1} 步</span>
        <strong>${escapeHtml(work.steps[index]?.title || `任务步骤 ${index + 1}`)}</strong>
      </div>
      <div>${items.map(agentWorkActivityMarkup).join("")}</div>
    </section>`;
  }).join("");
}

function renderAgentWork(article) {
  const work = article._agentWork;
  if (!work) return;
  const panel = article.querySelector(".agent-work");
  const elapsed = formatElapsed((work.finishedAt || Date.now()) - work.startedAt);
  const statusLabel = work.statusText || {
    running: "已处理", done: "已处理", failed: "执行失败", cancelled: "已停止",
  }[work.status] || "处理中";
  const activeIndex = currentAgentWorkIndex(work);
  const activeStep = work.steps[activeIndex];
  const plan = agentWorkPlanSnapshot(work.steps);
  const latestActivity = work.activities.flat().at(-1);
  const latestText = latestActivity?.text || activeStep?.detail || work.objective;
  const loopNote = work.status === "running"
    ? work.planAware
      ? plan.terminalized
        ? `${plan.label}；最终结果仍在核验，尚未最终确认。`
        : "计划状态会随执行进展同步；工作范围变化时会说明原因并调整步骤。"
      : ""
    : work.status === "done"
      ? plan.hasIssues
        ? `${plan.label}；已基于现有证据交付受限结果。`
        : "已完成计划检查并形成最终结果。"
      : work.status === "failed" ? "任务在完成前遇到错误。" : "任务已停止。";
  const completionClass = agentWorkCompletionState(work) === "completed_with_issues"
    ? " limited" : "";
  panel.className = `agent-work ${work.status}${completionClass}`;
  panel.innerHTML = `
    <details class="agent-work-disclosure" ${work.expanded ? "open" : ""}>
      <summary class="agent-work-head" aria-label="展开或收起处理过程">
        <span class="process-chevron">${icon("chevronDown", 13)}</span>
        <strong>${work.status === "done" && !plan.hasIssues ? "查看过程" : statusLabel}</strong><time>${elapsed}</time>
      </summary>
      <div class="agent-work-body">
        <p class="agent-work-commentary">${escapeHtml(latestText)}</p>
        <div class="agent-work-activities" aria-label="任务执行记录">
          ${agentWorkActivitiesMarkup(work)}
        </div>
        ${loopNote ? `<p class="agent-work-loop-note">${escapeHtml(loopNote)}</p>` : ""}
      </div>
    </details>`;
  const disclosure = panel.querySelector(".agent-work-disclosure");
  disclosure?.addEventListener("toggle", () => {
    work.expanded = disclosure.open;
  });
  syncTurnProgressDock(article);
}

function startAgentWork(article, objective = "", progressText = "") {
  $("details-toggle").hidden = false;
  const slot = article.querySelector(".agent-work-slot");
  if (!slot) return;
  slot.innerHTML = '<section class="agent-work running" aria-live="polite"></section>';
  const goal = summarizeTaskGoal(objective);
  const initialNode = fallbackProgressPhaseForText(progressText || "正在理解目标");
  const initialSteps = buildTaskProgressSteps(goal);
  article._agentWork = {
    startedAt: Date.now(), finishedAt: null, status: "running",
    objective: goal, loopAware: false, loopVersion: "",
    expanded: false, dockExpanded: false, dockId: "",
    planAware: false, planRevision: 0, planExplanation: "",
    taskStatus: "planning", lastEventRevision: 0, seenEventIds: new Set(),
    completionStatus: "", stopReason: "", planCloseout: null,
    answerStreaming: false,
    reconnectAttempts: 0,
    steps: initialSteps,
    activities: initialSteps.map(() => []),
    timer: null,
  };
  article._runWorkspace = RunWorkspace.createProjection();
  if (initialNode !== "intake") updateAgentWork(article, progressText);
  renderAgentWork(article);
  article._agentWork.timer = setInterval(() => renderAgentWork(article), 1000);
}

function updateAgentWork(article, text) {
  const work = article._agentWork;
  const clean = String(text || "").trim();
  if (!work || !clean || work.loopAware) return;
  const node = fallbackProgressPhaseForText(clean);
  activateProgressStep(work, node);
  renderAgentWork(article);
}

function addAgentWorkActivity(
  article, text, kind = "progress", key = "", increment = false, stepId = ""
) {
  const work = article._agentWork;
  const clean = String(text || "")
    .replace(/规划与推理中(?:…|\.\.\.)?/g, "正在分析任务并形成候选结果")
    .replace(/\s+/g, " ").trim();
  if (!work || !clean) return;
  recordAgentWorkActivityState(work, {text: clean, kind, key, increment, stepId});
  renderAgentWork(article);
}

function completedActivityText(text) {
  const value = String(text || "");
  if (/^正在整理最终结果$/.test(value)) return "最终结果整理完成";
  if (/^正在(.+)$/.test(value)) return `${value.slice(2)}完成`;
  if (/^等待(.+)$/.test(value)) return `${value.slice(2)}已处理`;
  if (/中(?:…|\.\.\.)?$/.test(value)) return `${value.replace(/中(?:…|\.\.\.)?$/, "")}完成`;
  return value;
}

function settleAgentWorkActivities(article) {
  const work = article._agentWork;
  if (!work) return;
  work.activities.flat().forEach(activity => {
    if (activity.kind !== "running"
      && !(activity.kind === "progress"
        && (/^正在/.test(activity.text) || /中(?:…|\.\.\.)?$/.test(activity.text)))) return;
    activity.kind = "done";
    activity.text = completedActivityText(activity.text);
  });
  renderAgentWork(article);
}

function rememberAgentWorkEvent(work, event = {}) {
  if (!work) return false;
  const eventId = String(event.event_id || "");
  if (eventId && work.seenEventIds.has(eventId)) return false;
  if (eventId) work.seenEventIds.add(eventId);
  const revision = Number(event.revision || 0);
  if (Number.isFinite(revision)) {
    work.lastEventRevision = Math.max(work.lastEventRevision, revision);
  }
  return true;
}

function applyTaskRuntimeEvent(article, eventType, payload = {}) {
  const work = article._agentWork;
  if (!work || !eventType.startsWith("task.")) return false;
  const inferred = {
    "task.queued": "queued",
    "task.started": "planning",
    "task.completed": "completed",
    "task.completed_with_issues": "completed_with_issues",
    "task.failed": "failed",
    "task.cancelled": "cancelled",
  }[eventType];
  const reported = String(
    payload.completion_status || payload.status || inferred || work.taskStatus
  );
  const inferredLimited = reported === "completed"
    && agentWorkPlanSnapshot(work.steps).hasIssues;
  const incoming = inferredLimited ? "completed_with_issues" : reported;
  const stickyLimited = work.taskStatus === "completed_with_issues" && incoming === "completed";
  work.taskStatus = stickyLimited ? work.taskStatus : incoming;
  if (work.taskStatus === "completed_with_issues") {
    work.completionStatus = "completed_with_issues";
  }
  work.statusText = {
    queued: "已排队",
    planning: "正在规划",
    executing: "执行中",
    waiting_approval: "等待批准",
    finalizing: work.answerStreaming ? "正在生成结果（尚未最终确认）" : "正在收尾",
    completed_with_issues: "受限完成",
    completed: "已处理",
    failed: "执行失败",
    cancelled: "已停止",
  }[work.taskStatus] || work.statusText;
  if (work.taskStatus === "finalizing" && work.answerStreaming) {
    setAnswerDeliveryState(article, "finalizing");
  }
  if (
    work.answerStreaming
    && ["completed", "completed_with_issues"].includes(work.taskStatus)
  ) {
    work.statusText = "结果待最终确认";
    setAnswerDeliveryState(article, "finalizing");
  }
  renderAgentWork(article);
  return true;
}

function replayAgentWork(article, process = {}) {
  const work = article._agentWork;
  if (!work || !process) return;
  const elapsed = Math.max(0, Number(process.elapsed_ms || 0));
  work.startedAt = Date.now() - elapsed;
  for (const event of (process.events || [])) {
    if (!rememberAgentWorkEvent(work, event)) continue;
    const eventType = String(event.event_type || "");
    const payload = event.payload || {};
    if (eventType === "turn.progress") {
      const text = String(payload.text || "");
      updateAgentWork(article, text);
      addAgentWorkActivity(article, text, "progress", `progress:${text}`);
      addTurnEvent(text, "progress", "", "activity", event.timestamp);
      continue;
    }
    if (payload.execution_scope !== "inline_subagent") {
      applyPlanRuntimeEvent(article, eventType, payload);
      applyTaskRuntimeEvent(article, eventType, payload);
      applyLoopRuntimeEvent(article, eventType, payload);
    }
    const presentation = runtimeEventPresentation(eventType, payload, work);
    recordRuntimeActivity(article, eventType, payload, presentation);
    recordWorkspaceDrawerEvent(eventType, payload, presentation, event.timestamp);
  }
  if (process.task_status) {
    applyTaskRuntimeEvent(article, "task.status", {status: process.task_status});
    updateRunPhase(article, "task.status", {status: work.taskStatus});
  }
  if (["done", "failed", "cancelled"].includes(process.status)) {
    if (process.status === "done") {
      const limited = agentWorkCompletionState(work) === "completed_with_issues";
      addAgentWorkActivity(
        article,
        limited ? "受限结果已完成并保存" : "最终结果已完成并保存",
        limited ? "warning" : "done",
        "terminal-outcome",
      );
    }
    finishAgentWork(article, process.status, "");
  } else {
    renderAgentWork(article);
  }
}

function failAgentWorkActivities(article, message = "") {
  const work = article._agentWork;
  if (!work) return;
  work.activities.flat().forEach(activity => {
    if (activity.kind === "running") activity.kind = "failed";
  });
  if (message) {
    addAgentWorkActivity(article, message, "failed", "run-failed");
  }
  renderAgentWork(article);
}

function activateProgressStep(work, node) {
  if (work.planAware) return;
  const copy = {
    intake: ["分析任务并确定下一步", "正在结合目标、上下文与可用能力决定下一步"],
    resolve_memory: ["关联相关上下文", "正在筛选与当前目标直接相关的历史和资料"],
    route: ["选择下一步行动", "正在根据当前证据决定下一步"],
    gather_evidence: ["补充必要证据", "正在收集完成任务所需的外部信息"],
    agent_loop: ["执行当前行动", "正在观察结果并动态决定后续行动"],
    verify: ["核验完成条件", "正在检查结果是否完整满足目标"],
    revise: ["修正结果缺口", "正在根据验证反馈修订结果"],
    finish: ["整理最终结果", "正在形成可展示、可保存的交付结果"],
  }[node] || ["推进当前任务", "正在动态决定下一步"];
  const currentIndex = currentAgentWorkIndex(work);
  const current = work.steps[currentIndex];
  if (!current) return;
  current.detail = copy[1];
  if (current.status !== "failed") current.status = "current";
}

function applyPlanRuntimeEvent(article, eventType, payload = {}) {
  const work = article._agentWork;
  if (!work || !["plan.created", "plan.updated"].includes(eventType)) return false;
  const items = Array.isArray(payload.steps) ? payload.steps : [];
  if (items.length < 2 || items.length > 24) return false;
  const previousSteps = work.steps;
  const previousActivities = new Map(
    previousSteps.map((step, index) => [step.node, work.activities[index] || []]),
  );
  const previousByTitle = new Map(previousSteps.map(step => [step.title, step]));
  work.planAware = true;
  work.planRevision = Math.max(
    work.planRevision + 1,
    Number(payload.revision || 0),
  );
  work.planExplanation = String(payload.explanation || "").trim();
  const sameShape = items.length === previousSteps.length;
  work.steps = items.map((item, index) => {
    const status = {
      pending: "pending",
      in_progress: "current",
      completed: "done",
      failed: "failed",
      blocked: "blocked",
      skipped: "skipped",
    }[item.status] || "pending";
    const title = summarizePlanStep(item.step, `任务步骤 ${index + 1}`);
    const sameTitle = previousByTitle.get(title);
    const node = String(
      item.id || sameTitle?.node
      || (sameShape ? previousSteps[index]?.node : "")
      || `plan_${work.planRevision}_${index + 1}`
    );
    return {
      node,
      title,
      detail: title,
      status,
    };
  });
  work.activities = work.steps.map(step => previousActivities.get(step.node) || []);
  renderAgentWork(article);
  return true;
}

function applyLoopRuntimeEvent(article, eventType, payload = {}) {
  const work = article._agentWork;
  if (!work || !/^(runtime|turn|loop|verification|evaluation|interaction|plan\.closeout)\./.test(eventType)) return false;
  if (eventType === "runtime.started") {
    work.loopAware = true;
    work.loopVersion = String(payload.loop_version || "");
  } else if (eventType === "turn.started") {
    work.loopAware = true;
    if (!work.planAware) activateProgressStep(work, "intake");
  } else if (eventType === "loop.iteration.started") {
    work.loopAware = true;
    if (!work.planAware) activateProgressStep(work, "agent_loop");
  } else if (eventType === "verification.started") {
    if (!work.planAware) activateProgressStep(work, "verify");
  } else if (eventType === "loop.stopped") {
    work.stopReason = String(payload.reason || "");
  } else if (eventType === "plan.closeout.started") {
    work.planCloseout = {status: "running", reason: String(payload.reason || work.stopReason || "")};
  } else if (eventType === "plan.closeout.completed") {
    work.planCloseout = {...payload, status: String(payload.status || "completed")};
    if (payload.all_completed === false || payload.terminalized === false) {
      work.completionStatus = "completed_with_issues";
    }
  } else if (eventType === "verification.completed") {
    const completionStatus = String(payload.completion_status || "");
    if (completionStatus) work.completionStatus = completionStatus;
    if (payload.plan_passed === false) work.completionStatus = "completed_with_issues";
  } else if (eventType === "loop.completed" || eventType === "turn.completed") {
    const completionStatus = String(
      payload.completion_status || payload.checkpoint?.status || ""
    );
    if (completionStatus) work.completionStatus = completionStatus;
    if (!work.planAware) work.steps.forEach(step => { step.status = "done"; });
    settleAgentWorkActivities(article);
  }
  renderAgentWork(article);
  return true;
}

function finishAgentWork(article, status, text) {
  const work = article._agentWork;
  if (!work) return;
  if (text && !work.loopAware) updateAgentWork(article, text);
  work.status = status;
  work.answerStreaming = false;
  if (status === "done") {
    // 流式 end 事件可能先于 task.completed 运行时事件到达，且连接会在 end 后
    // 立即结束。此处必须主动收敛胶囊的业务终态，不能继续沿用 finalizing
    // 留下的“正在收尾”；刷新后的事件回放只是兜底，不应成为状态更新条件。
    const completionState = agentWorkCompletionState(work);
    work.completionStatus = completionState;
    work.taskStatus = completionState;
    work.statusText = completionState === "completed_with_issues" ? "受限完成" : "已处理";
    if (!work.planAware) work.steps.forEach(step => { step.status = "done"; });
    settleAgentWorkActivities(article);
  } else if (status === "failed") {
    work.taskStatus = "failed";
    work.statusText = "执行失败";
    const current = work.steps[currentAgentWorkIndex(work)];
    if (current) current.status = "failed";
    failAgentWorkActivities(article);
  } else if (status === "cancelled") {
    work.taskStatus = "cancelled";
    work.statusText = "已停止";
  }
  work.finishedAt = Date.now();
  work.expanded = false;
  clearInterval(work.timer);
  work.timer = null;
  renderAgentWork(article);
}

function activeAgent() {
  return state.agents.find(agent => agent.id === state.agentId);
}

const AGENT_LIGHTS = {
  idle: {label: "空闲", color: "#e0e0e0"},
  unread: {label: "未读聊天", color: "#9bf396"},
  thinking: {label: "思考或运行中", color: "#9cd5fe"},
  attention: {label: "需要用户批准或答复", color: "#ffd0b8"},
  error: {label: "执行错误", color: "#ff7373"},
};

function agentSeenStorageKey() {
  return `harness_agent_seen:${Auth.username() || "anonymous"}`;
}

function agentSeenState() {
  if (state.agentSeen !== null) return state.agentSeen;
  try {
    const value = JSON.parse(localStorage.getItem(agentSeenStorageKey()) || "{}");
    state.agentSeen = value && typeof value === "object" ? value : {};
  } catch (_) {
    state.agentSeen = {};
  }
  return state.agentSeen;
}

function persistAgentSeenState() {
  localStorage.setItem(agentSeenStorageKey(), JSON.stringify(agentSeenState()));
}

function executionLight(status) {
  const value = String(status || "").toLowerCase();
  let key = "idle";
  if (["awaiting_approval", "waiting_approval"].includes(value)) key = "attention";
  else if (["queued", "pending", "planning", "running", "executing", "finalizing"].includes(value)) key = "thinking";
  return {key, ...AGENT_LIGHTS[key]};
}

function conversationExecutionStatus(sessionId) {
  if (!sessionId) return "";
  const active = state.activeJobs
    .filter(job => job.session_id === sessionId)
    .sort((a, b) => {
      const priority = value => value === "awaiting_approval" ? 3
        : ["running"].includes(value) ? 2 : 1;
      return priority(b.status) - priority(a.status)
        || new Date(b.created_at || 0) - new Date(a.created_at || 0);
    })[0];
  if (active) return active.status || "pending";
  return "";
}

function agentLight(agentId) {
  const row = state.agentStatuses.get(Number(agentId)) || {};
  if (row.active_status === "awaiting_approval") {
    return {key: "attention", ...AGENT_LIGHTS.attention};
  }
  if (["pending", "running"].includes(row.active_status)) {
    return {key: "thinking", ...AGENT_LIGHTS.thinking};
  }
  const terminalUnread = Boolean(
    row.terminal_turn_id
    && agentSeenState()[String(agentId)] !== row.terminal_turn_id
  );
  if (terminalUnread && row.terminal_status === "failed") {
    return {key: "error", ...AGENT_LIGHTS.error};
  }
  if (
    terminalUnread
    && ["completed", "completed_with_issues"].includes(row.terminal_status)
  ) {
    return {key: "unread", ...AGENT_LIGHTS.unread};
  }
  return {key: "idle", ...AGENT_LIGHTS.idle};
}

function activeConversationLight() {
  // “新对话”不是历史会话，也没有 Turn；不得继承该智能体其它会话的状态。
  if (!state.sessionId) return {key: "idle", ...AGENT_LIGHTS.idle};
  return executionLight(conversationExecutionStatus(state.sessionId));
}

function markAgentStatusSeen(agentId, turnId = "") {
  if (!agentId) return;
  const terminalId = turnId
    || state.agentStatuses.get(Number(agentId))?.terminal_turn_id
    || "";
  if (!terminalId || agentSeenState()[String(agentId)] === terminalId) return;
  agentSeenState()[String(agentId)] = terminalId;
  persistAgentSeenState();
  renderAgentMenu();
}

function applyActiveAgentLight() {
  const selector = $("agent-selector");
  const light = activeConversationLight();
  selector.dataset.agentState = light.key;
  selector.style.setProperty("--agent-state-color", light.color);
  selector.title = `${activeAgent()?.name || "智能体"} · ${light.label}`;
  selector.setAttribute("aria-label", `选择智能体，当前状态：${light.label}`);
}

function exportLinks(files) {
  return (files || []).map(name => {
    const safeName = String(name || "");
    const url = `/api/v1/exports/${encodeURIComponent(safeName)}`;
    return `<a href="${url}" data-export-download="${escapeHtml(safeName)}">下载 ${escapeHtml(safeName)}</a>`;
  }).join(" · ");
}

async function handleExportDownload(link) {
  if (link.getAttribute("aria-busy") === "true") return;
  link.setAttribute("aria-busy", "true");
  const original = link.textContent;
  link.textContent = "正在下载…";
  try {
    await downloadAuthenticated(
      link.getAttribute("href"),
      link.dataset.exportDownload || "download",
    );
  } catch (error) {
    showToast(`下载失败：${error.message}`);
  } finally {
    link.removeAttribute("aria-busy");
    link.textContent = original;
  }
}

async function loadPreferences() {
  // Refresh account defaults without overwriting explicit choices in this draft.
  return applyPreferences(await api("/api/v1/users/me/preferences"));
}

function applyPreferences(value) {
  const preferences = ChatWorkspace.normalizePreferences(value, Auth.role());
  if (Number.isInteger(value.revision) && value.revision >= 0) preferences.revision = value.revision;
  // A slower refresh must not replace settings saved by a newer request.
  if (Number.isInteger(state.preferences?.revision) && preferences.revision < state.preferences.revision) return state.preferences;
  state.preferences = preferences;
  state.approvalPolicy = ChatWorkspace.effectiveApproval(preferences, state.composerSelection, Auth.role());
  state.recentSortMode = preferences.recent_sort;
  localStorage.setItem("gca_theme", preferences.theme);
  Theme.setAccent(value.theme_color ?? "default", value.custom_color ?? "#8b5cf6");
  Theme.apply(preferences.theme);
  renderAccountTheme();
  renderComposerControls();
  return preferences;
}

function renderAccountTheme() {
  const group = $("account-theme-options");
  const theme = state.preferences?.theme || Theme.current();
  group.setAttribute("aria-busy", String(state.themeSaving));
  group.querySelectorAll("[data-account-theme]").forEach(button => {
    button.setAttribute("aria-checked", String(button.dataset.accountTheme === theme));
    button.setAttribute("aria-disabled", String(state.themeSaving || !state.sessionReady));
  });
}

async function setAccountTheme(theme) {
  if (!["light", "dark", "system"].includes(theme) || state.themeSaving || !state.sessionReady) return;
  if (theme === state.preferences?.theme) return;
  state.themeSaving = true;
  renderAccountTheme();
  try {
    const latest = await loadPreferences();
    if (!Number.isInteger(latest.revision)) throw new Error("无法读取设置版本，请刷新后重试");
    const updated = await api("/api/v1/users/me/preferences", {
      method: "PATCH", json: {theme, revision: latest.revision},
    });
    applyPreferences(updated);
  } catch (error) {
    if (error.status === 409) {
      try { await loadPreferences(); } catch (_) { /* Keep the last confirmed preference. */ }
    }
    showToast(`外观切换失败：${error.message}`);
  } finally {
    state.themeSaving = false;
    renderAccountTheme();
  }
}

function hasComposerOverrides(selection = state.composerSelection) {
  return Boolean(selection?.approval_policy || selection?.provider_id != null || selection?.reasoning_effort);
}

function composerModelLabel(model) {
  if (model && !ChatWorkspace.modelAvailable(model)) return "暂无可用模型";
  const modelName = String(model?.model_name || "").trim();
  if (modelName) return modelName;
  const legacyName = String(model?.name || "").trim();
  if (legacyName && !legacyName.startsWith("__personal_model_")
    && !["智能体默认", "智能体自动选择"].includes(legacyName)) return legacyName;
  return String(model?.model || "").trim() || "智能体自动";
}

function positionComposerPopover() {
  const menu = $("reasoning-menu");
  if (menu.hidden) return;
  const anchor = $("model-btn").getBoundingClientRect();
  const viewport = window.visualViewport;
  const margin = 12, gap = 10;
  const leftEdge = (viewport?.offsetLeft || 0) + margin;
  let topEdge = (viewport?.offsetTop || 0) + margin;
  const rightEdge = leftEdge + (viewport?.width || window.innerWidth) - margin * 2;
  const bottomEdge = topEdge + (viewport?.height || window.innerHeight) - margin * 2;
  menu.style.maxWidth = `${Math.max(0, rightEdge - leftEdge)}px`;
  menu.style.maxHeight = `${Math.max(0, Math.min(menu.classList.contains("model-mode") ? 500 : 310, bottomEdge - topEdge))}px`;
  const size = menu.getBoundingClientRect();
  const centeredLeft = (anchor.left + anchor.right - size.width) / 2;
  const left = Math.max(leftEdge, Math.min(centeredLeft, rightEdge - size.width));
  const header = document.querySelector(".conversation-header")?.getBoundingClientRect();
  if (header?.height && left < header.right && left + size.width > header.left && header.bottom > topEdge && header.top < bottomEdge) {
    topEdge = Math.min(bottomEdge, header.bottom + 8);
  }
  const above = Math.max(0, anchor.top - gap - topEdge);
  const below = Math.max(0, bottomEdge - anchor.bottom - gap);
  const placeAbove = size.height <= above || above >= below;
  const height = Math.min(size.height, placeAbove ? above : below, bottomEdge - topEdge);
  menu.style.maxHeight = `${Math.max(0, height)}px`;
  menu.style.left = `${left}px`;
  menu.style.top = `${Math.max(topEdge, Math.min(placeAbove ? anchor.top - gap - height : anchor.bottom + gap, bottomEdge - height))}px`;
}

let composerPopoverFrame = 0;
function scheduleComposerPopoverPosition() {
  if ($("reasoning-menu").hidden || composerPopoverFrame) return;
  composerPopoverFrame = requestAnimationFrame(() => {
    composerPopoverFrame = 0;
    positionComposerPopover();
  });
}

function closeComposerMenus(restoreFocus = false) {
  for (const kind of ["approval-policy", "reasoning"]) {
    const menu = $(`${kind}-menu`);
    const button = $(kind === "reasoning" ? "model-btn" : `${kind}-btn`);
    const wasOpen = !menu.hidden;
    menu.hidden = true;
    menu.classList.remove("open");
    button.classList.remove("active");
    button.setAttribute("aria-expanded", "false");
    if (restoreFocus && wasOpen) button.focus({preventScroll: true});
  }
  $("model-menu").hidden = true;
  $("model-menu").classList.remove("open");
  $("reasoning-menu").classList.remove("model-mode");
  $("reasoning-model-btn").setAttribute("aria-expanded", "false");
}

function toggleComposerMenu(kind, focus = false) {
  if (state.submitting) return;
  const menu = $(`${kind}-menu`);
  const open = menu.hidden;
  closeComposerMenus();
  closeAddMenu();
  closePalette();
  if (!open) return;
  menu.hidden = false;
  menu.classList.add("open");
  const trigger = $(kind === "reasoning" ? "model-btn" : `${kind}-btn`);
  trigger.classList.add("active");
  trigger.setAttribute("aria-expanded", "true");
  if (kind === "reasoning") {
    renderReasoningControl();
    positionComposerPopover();
  }
  if (focus) {
    const target = kind === "reasoning" && !$("reasoning-slider").disabled
      ? $("reasoning-slider") : menu.querySelector('button:not([disabled]), a[href]');
    target?.focus({preventScroll: true});
  }
}

function setComposerModelMode(showModels, focus = false) {
  if (state.submitting || state.modelsLoading) return;
  $("model-menu").hidden = !showModels;
  $("model-menu").classList.toggle("open", showModels);
  $("reasoning-menu").classList.toggle("model-mode", showModels);
  $("reasoning-model-btn").setAttribute("aria-expanded", String(showModels));
  renderReasoningControl();
  positionComposerPopover();
  if (focus) {
    const target = showModels ? $("model-menu").querySelector('button:not([disabled]), a[href]')
      : !$("reasoning-slider").disabled ? $("reasoning-slider") : $("reasoning-model-btn");
    target?.focus({preventScroll: true});
  }
}

function closeComposerMenuOnFocusOut(event, menu, trigger) {
  // Non-focusable label clicks and replaced rows can blur to <body> before click.
  // Only a known external focus destination dismisses here; outside clicks and
  // Escape already have their own handlers and do not depend on transient focus.
  if (!event.relatedTarget || menu.contains(event.relatedTarget) || event.relatedTarget === trigger) return;
  queueMicrotask(() => {
    if (!menu.hidden && !menu.contains(document.activeElement) && document.activeElement !== trigger) closeComposerMenus();
  });
}

function renderComposerControls() {
  const selection = state.composerSelection || ChatWorkspace.newComposerSelection();
  const policy = ChatWorkspace.effectiveApproval(state.preferences, selection, Auth.role());
  state.approvalPolicy = policy;
  $("approval-policy-label").textContent = policy === "full_access" ? "完全访问" : APPROVAL_POLICIES[policy].label;
  $("approval-policy-btn").dataset.policy = policy;
  $("approval-policy-btn").title = `批准策略：${APPROVAL_POLICIES[policy].label}${selection.approval_policy ? "（本轮选择）" : "（使用默认）"}`;
  const policies = [
    ["", Auth.isGuest() ? "使用默认" : "跟随后台", `当前：${APPROVAL_POLICIES[state.preferences?.approval_policy || "ask"].label}`],
    ["ask", "请求批准", "有副作用的操作先征求批准"],
    ["auto", "帮我批准", "低风险工作区操作自动执行"],
    ["full_access", "完全访问", Auth.role() === "root" ? "保留平台安全边界，不再逐次询问" : "完全访问仅限 root"],
  ];
  $("approval-policy-menu").innerHTML = policies.map(([value, label, detail]) => `<button type="button" data-quick-policy="${value}" role="menuitemradio" aria-checked="${(selection.approval_policy || "") === value}" class="${(selection.approval_policy || "") === value ? "active" : ""}" ${value === "full_access" && Auth.role() !== "root" ? "disabled" : ""}>
    <span class="approval-policy-icon ${value === "full_access" ? "danger" : ""}">${icon("shield", 17)}</span><span><strong>${label}</strong><small>${detail}</small></span><span class="approval-policy-check">${icon("check", 16)}</span></button>`).join("");
  $("approval-policy-menu").querySelectorAll("[data-quick-policy]").forEach(button => {
    button.onclick = () => {
      if (state.submitting || button.disabled) return;
      state.composerSelection.approval_policy = button.dataset.quickPolicy || null;
      renderComposerControls();
      closeComposerMenus(true);
      setSendState();
    };
  });
  const current = ChatWorkspace.selectedComposerModel(state.models, selection);
  $("model-btn-label").textContent = state.modelsLoading ? "加载模型…" : composerModelLabel(current);
  $("model-btn").title = `${composerModelLabel(current)}${selection.provider_id == null ? "（智能体自动选择）" : ""} · 选择模型`;
  renderReasoningControl();
  $("model-menu").innerHTML = `<div class="model-menu-head"><strong>模型</strong><span>选择用于下一轮任务的模型</span></div>`
    + (state.modelError ? `<p class="composer-model-error" role="alert">${escapeHtml(state.modelError)}</p><button type="button" data-model-retry role="menuitem">重新加载模型</button>` : "")
    + state.models.filter(ChatWorkspace.modelAvailable).map(model => `<button type="button" data-quick-provider="${model.provider_id ?? ""}" role="menuitemradio" aria-checked="${model.provider_id === selection.provider_id}" class="${model.provider_id === selection.provider_id ? "active" : ""}">
      <span><strong>${escapeHtml(model.provider_id == null ? "智能体自动选择" : composerModelLabel(model))}</strong><small>${escapeHtml(model.provider_id == null ? composerModelLabel(model) : model.name)}</small></span>${model.provider_id === selection.provider_id ? icon("check", 16) : ""}</button>`).join("")
    + (!state.modelsLoading && !state.modelError && !state.models.some(ChatWorkspace.modelAvailable) ? '<p class="composer-model-note">暂无可用模型，请联系管理员配置。</p>' : "");
  $("model-menu").querySelectorAll("[data-quick-provider]").forEach(button => {
    button.onclick = event => {
      event.stopPropagation();
      if (state.submitting) return;
      const providerId = button.dataset.quickProvider ? Number(button.dataset.quickProvider) : null;
      const changed = ChatWorkspace.reconcileComposerSelection({...selection, provider_id: providerId}, state.models);
      state.composerSelection = changed.selection;
      if (changed.reason) showToast("已恢复该模型的默认推理强度");
      renderComposerControls();
      setComposerModelMode(false, true);
      setSendState();
    };
  });
  $("model-menu").querySelector("[data-model-retry]")?.addEventListener("click", () => loadModels().catch(error => showToast(error.message)));
}

function renderReasoningControl() {
  const current = ChatWorkspace.selectedComposerModel(state.models, state.composerSelection);
  const slider = ChatWorkspace.reasoningSlider(current, state.composerSelection);
  const adjustable = Boolean(slider.efforts.length);
  const inheritedLabel = slider.inherited ? "，沿用模型默认" : "，本轮选择";
  const modelMode = !$("model-menu").hidden;
  $("reasoning-effort-label").textContent = slider.label;
  $("reasoning-effort-label").hidden = !adjustable;
  $("model-btn").title = `${composerModelLabel(current)}${adjustable ? ` · ${slider.label}${inheritedLabel}` : ""} · 模型与推理强度`;
  $("model-btn").setAttribute("aria-label", `选择模型与推理强度：${composerModelLabel(current)}${adjustable ? `，${slider.label}` : ""}`);
  $("reasoning-current-label").textContent = adjustable ? slider.label : "默认";
  $("reasoning-current-model").textContent = composerModelLabel(current);
  $("reasoning-model-btn").title = modelMode ? "返回推理强度" : `${composerModelLabel(current)} · 更换模型`;
  $("reasoning-model-btn").setAttribute("aria-label", modelMode ? "返回推理强度" : "更换模型");
  $("reasoning-slider-wrap").hidden = modelMode || !adjustable;
  $("reasoning-unavailable").hidden = modelMode || adjustable;
  $("reasoning-reset").hidden = modelMode;
  $("reasoning-unavailable").textContent = state.modelsLoading ? "正在读取模型支持的强度…"
    : current?.reasoning_unavailable_reason || (ChatWorkspace.modelAvailable(current)
      ? "当前模型未提供可调推理强度，将使用模型默认设置。" : "请先选择或添加可用模型。");
  const input = $("reasoning-slider");
  input.max = String(Math.max(0, slider.efforts.length - 1));
  input.value = String(slider.index);
  input.setAttribute("aria-valuetext", `${slider.label}${inheritedLabel}`);
  const progress = slider.value && slider.efforts.length > 1 ? slider.index / (slider.efforts.length - 1) * 100 : 0;
  $("reasoning-slider-fill").style.width = `${progress}%`;
  const visual = ChatWorkspace.reasoningVisual(adjustable ? slider.value : "");
  const wrap = $("reasoning-slider-wrap");
  wrap.setAttribute("data-energy", visual.tier);
  wrap.style.setProperty("--reasoning-power", visual.power);
  wrap.style.setProperty("--reasoning-glow", visual.glow);
  wrap.style.setProperty("--reasoning-flow-duration", `${visual.duration}s`);
  wrap.style.setProperty("--reasoning-trail", `${visual.trail}px`);
  const energy = $("reasoning-slider-energy");
  // Keep the same nodes/animation phase across unrelated composer rerenders.
  const signature = `${visual.tier}:${visual.count}`;
  if (energy.motionSignature !== signature) {
    energy.motionSignature = signature;
    energy.innerHTML = Array.from({length: visual.count}, (_, index) => {
      const duration = (visual.duration * (.78 + index % 5 * .095)).toFixed(2);
      return `<span class="reasoning-particle" style="--particle-x:${2 + (index * 37 + 11) % 95}%;--particle-y:${17 + index * 29 % 67}%;--particle-size:${1.4 + index % 4 * .45}px;--particle-delay:-${(index * 1.37 % visual.duration).toFixed(2)}s;--particle-duration:${duration}s;--particle-drift:${visual.drift}px;--particle-opacity:${.3 + index % 4 * .14}"></span>`;
    }).join("");
  }
  $("reasoning-slider-ticks").innerHTML = slider.efforts.map(value =>
    `<i title="${escapeHtml(ChatWorkspace.reasoningLabel(value, current))}"></i>`).join("");
  setReasoningState();
}

function selectReasoningStep(index) {
  if (state.submitting || state.modelsLoading) return;
  const current = ChatWorkspace.selectedComposerModel(state.models, state.composerSelection);
  const slider = ChatWorkspace.reasoningSlider(current, state.composerSelection);
  if (!Number.isInteger(index) || index < 0 || index >= slider.efforts.length) return;
  state.composerSelection.reasoning_effort = slider.efforts[index];
  renderReasoningControl();
  setSendState();
}

async function fetchComposerModels(agentId) {
  const result = await api(`/api/v1/chat/models?agent_id=${agentId}`);
  return ChatWorkspace.normalizeModelCatalog(result);
}

async function loadModels({reset = false} = {}) {
  const agentId = state.agentId;
  const request = ++state.modelRequest;
  if (reset) state.composerSelection = {...state.composerSelection, provider_id: null, reasoning_effort: ""};
  state.models = [];
  state.modelsLoading = Boolean(agentId);
  state.modelError = "";
  renderComposerControls();
  setSendState();
  if (!agentId) return;
  try {
    const models = await fetchComposerModels(agentId);
    if (request !== state.modelRequest || agentId !== state.agentId) return;
    state.models = models;
    const reconciled = ChatWorkspace.reconcileComposerSelection(state.composerSelection, models);
    state.composerSelection = reconciled.selection;
    if (reconciled.reason) showToast(reconciled.reason);
  } catch (error) {
    if (request !== state.modelRequest || agentId !== state.agentId) return;
    state.modelError = `模型加载失败：${error.message}`;
    throw error;
  } finally {
    if (request === state.modelRequest && agentId === state.agentId) {
      state.modelsLoading = false;
      renderComposerControls();
      setSendState();
    }
  }
}

async function prepareComposerSubmission(context) {
  if (context.runtimeOptions) return context.runtimeOptions;
  const agentId = context.agentId || state.agentId;
  const selection = {...(context.selection || state.composerSelection || ChatWorkspace.newComposerSelection())};
  const preferences = await loadPreferences();
  const models = await fetchComposerModels(agentId);
  const checked = ChatWorkspace.reconcileComposerSelection(selection, models);
  if (agentId === state.agentId) {
    state.models = models;
    state.modelError = "";
    if (checked.reason) state.composerSelection = checked.selection;
    renderComposerControls();
  }
  if (checked.reason) throw new Error(checked.reason);
  const selectedModel = ChatWorkspace.selectedComposerModel(models, selection);
  if (!ChatWorkspace.modelAvailable(selectedModel)) {
    throw new Error("暂无可用模型，请联系管理员配置");
  }
  context.runtimeOptions = Object.freeze({
    agent_id: agentId,
    provider_id: selection.provider_id ?? selectedModel.automatic_provider_id ?? null,
    reasoning_effort: selection.reasoning_effort || "",
    approval_policy: ChatWorkspace.effectiveApproval(preferences, selection, Auth.role()),
  });
  return context.runtimeOptions;
}

function renderAgentMenu() {
  $("agent-menu").innerHTML = state.agents.map(agent => `
    <button class="${agent.id === state.agentId ? "active" : ""}" data-agent="${agent.id}"
      data-agent-state="${agentLight(agent.id).key}" role="menuitem"
      style="--agent-state-color:${agentLight(agent.id).color}"
      aria-label="${escapeHtml(agent.name)}，${agentLight(agent.id).label}"
      title="状态：${agentLight(agent.id).label}">
      <span class="agent-dot">${icon("sparkles", 15)}</span>
      <span><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml(agent.description || "未填写说明")}<em class="agent-state-text"> · ${agentLight(agent.id).label}</em></small></span>
      ${agent.id === state.agentId ? `<b>${icon("check", 16)}</b>` : ""}
    </button>`).join("");
  applyActiveAgentLight();
  $("agent-menu").querySelectorAll("[data-agent]").forEach(button => {
    button.onclick = async () => {
      if (state.submitting) return;
      state.agentId = Number(button.dataset.agent);
      state.selectedSkills.clear();
      state.selectedMcp.clear();
      state.selectedAgentCalls.clear();
      $("agent-name").textContent = activeAgent()?.name || "选择智能体";

      $("agent-menu").classList.remove("open");
      $("agent-selector").setAttribute("aria-expanded", "false");
      renderAgentMenu();
      try { await Promise.all([loadCatalog(), loadModels({reset: true})]); }
      catch (error) { showToast(error.message); }
      setSendState();
      renderResources();
      if (!state.sessionId) renderWelcome();
    };
  });
}

function renderWelcome() {
  const suggestions = [
    ["分析文件", "提取附件中的关键信息，并给出可核验的结论"],
    ["制定计划", "把我的目标拆解成清晰、可执行的行动计划"],
    ["总结资料", "结合知识库，总结主题并标明重要依据"],
    ["检查结果", "审查一份现有结果，找出风险和改进空间"],
  ];
  log.innerHTML = `
    <div class="welcome" id="welcome">
      <h1 id="welcome-title">${escapeHtml(activeAgent()?.opening_statement || "今天想完成什么？")}</h1>
      <p id="welcome-copy">${escapeHtml(activeAgent()?.description || "描述你的目标，我会规划、执行并核验结果。")}</p>
      <div class="starter-grid">
        ${suggestions.map(([title, prompt]) => `<button class="starter-card" data-prompt="${escapeHtml(prompt)}">
          <strong>${escapeHtml(title)}</strong>
        </button>`).join("")}
      </div>
    </div>`;
  log.querySelectorAll("[data-prompt]").forEach(button => {
    button.onclick = () => {
      query.value = button.dataset.prompt;
      resizeComposer();
      query.focus();
    };
  });
}

async function loadAgents() {
  state.agents = await api("/api/v1/agents/enabled");
  const preferred = state.preferences?.default_agent_id;
  const selected = state.agents.find(a => a.id === preferred)
    || state.agents.find(a => a.is_default)
    || state.agents[0];
  state.agentId = selected?.id || null;
  $("agent-name").textContent = selected?.name || "暂无可用智能体";

  renderAgentMenu();
  await loadModels({reset: true});
  renderWelcome();
  setSendState();
}

async function loadAgentStatuses({bootstrap = false} = {}) {
  const hadSeenState = localStorage.getItem(agentSeenStorageKey()) !== null;
  const result = await api("/api/v1/chat/agents/status");
  state.agentStatuses = new Map(
    (result.items || []).map(item => [Number(item.agent_id), item])
  );
  if (bootstrap && !hadSeenState) {
    for (const item of (result.items || [])) {
      if (item.terminal_turn_id) {
        agentSeenState()[String(item.agent_id)] = item.terminal_turn_id;
      }
    }
    persistAgentSeenState();
  }
  renderAgentMenu();
  return result.items || [];
}

function stopAgentStatusPolling() {
  clearInterval(state.agentStatusPollTimer);
  state.agentStatusPollTimer = null;
}

async function pollAgentStatuses() {
  if (document.hidden || state.pageLeaving || state.agentStatusPollInFlight) return;
  state.agentStatusPollInFlight = true;
  try {
    await loadAgentStatuses();
  } catch (_) {
    // 保留最近一次可信状态，短暂断线后下一轮自动恢复。
  } finally {
    state.agentStatusPollInFlight = false;
  }
}

function startAgentStatusPolling() {
  stopAgentStatusPolling();
  if (document.hidden || state.pageLeaving) return;
  state.agentStatusPollTimer = setInterval(pollAgentStatuses, 3000);
}

function threadTitle(item) {
  return item.title || item.query || "新对话";
}

function activeProject() {
  return state.projects.find(project => project.id === Number(state.activeProjectId)) || null;
}

function renderComposerProjectContext() {
  const project = activeProject();
  const showProjectSelector = Boolean(project && !state.sessionId);
  const context = $("composer-project-context");
  context.hidden = !showProjectSelector;
  $("composer-project-name").textContent = project?.name || "";
  context.title = project ? `当前项目：${project.name}` : "";
  context.closest(".composer").classList.toggle("has-project-context", showProjectSelector);
  updateComposerPlaceholder();
}

function updateComposerPlaceholder() {
  if ($("mention-pills").childElementCount) {
    query.placeholder = "";
    return;
  }
  const project = activeProject();
  query.placeholder = project && !state.sessionId
    ? `在“${project.name}”中开始对话…`
    : state.sessionId ? "继续提问或补充要求…" : "随心输入";
}

function collapsedProjectIds() {
  try {
    return new Set(JSON.parse(localStorage.getItem("chat_collapsed_projects") || "[]").map(Number));
  } catch (_) {
    return new Set();
  }
}

function isProjectCollapsed(projectId) {
  return collapsedProjectIds().has(Number(projectId));
}

function setProjectCollapsed(projectId, collapsed) {
  const ids = collapsedProjectIds();
  if (collapsed) ids.add(Number(projectId));
  else ids.delete(Number(projectId));
  localStorage.setItem("chat_collapsed_projects", JSON.stringify([...ids]));
}

function belongsToProject(value, projectId) {
  return projectId == null ? value == null : Number(value) === Number(projectId);
}

function collectThreads(projectId, sortMode = "priority") {
  const filter = $("history-search").value.trim().toLowerCase();
  const active = state.activeJobs.filter(job =>
    job.source !== "cron"
    && belongsToProject(job.project_id, projectId)
    && (!filter || threadTitle(job).toLowerCase().includes(filter))
  );
  const cronSessions = new Set(
    state.activeJobs.filter(job => job.source === "cron")
      .map(job => job.session_id).filter(Boolean)
  );
  const activeSessions = new Set(active.map(job => job.session_id).filter(Boolean));
  const threadRows = new Map();
  for (const row of state.conversations) {
    if (!row.session_id || !belongsToProject(row.project_id, projectId)) continue;
    if (activeSessions.has(row.session_id)) continue;
    const current = threadRows.get(row.session_id);
    if (!current) threadRows.set(row.session_id, {...row});
    else if (!current.title && row.title) current.title = row.title;
  }
  const threads = [...threadRows.values()]
    .filter(row => !filter || threadTitle(row).toLowerCase().includes(filter))
    .sort((a, b) => sortMode === "updated"
      ? new Date(b.created_at) - new Date(a.created_at)
      : Number(Boolean(b.pinned)) - Number(Boolean(a.pinned))
        || new Date(b.created_at) - new Date(a.created_at));
  return {active, threads, cronSessions};
}

function collectPinnedThreads() {
  const filter = $("history-search").value.trim().toLowerCase();
  const cronSessions = new Set(
    state.activeJobs.filter(job => job.source === "cron")
      .map(job => job.session_id).filter(Boolean)
  );
  const threadRows = new Map();
  for (const row of state.conversations) {
    if (!row.session_id || !row.pinned) continue;
    const current = threadRows.get(row.session_id);
    if (!current) threadRows.set(row.session_id, {...row});
    else if (!current.title && row.title) current.title = row.title;
  }
  const threads = [...threadRows.values()]
    .filter(row => !filter || threadTitle(row).toLowerCase().includes(filter))
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
  return {active: [], threads, cronSessions};
}

function threadCollectionHtml(collection, nested = false) {
  const {active, threads, cronSessions} = collection;
  return `
    ${active.map(job => `
      <button class="thread-item running ${nested ? "project-thread" : ""} ${job.job_id === state.runningJob ? "active" : ""}"
        data-active-job="${escapeHtml(job.job_id)}">
        <span>${escapeHtml(threadTitle(job).slice(0, 34))}</span>
        <span class="thread-running-indicator" title="任务运行中" aria-label="任务运行中"><i></i></span>
      </button>`).join("")}
    ${threads.map(row => `
      <div class="thread-item ${nested ? "project-thread" : ""} ${row.session_id === state.sessionId ? "active" : ""} ${cronSessions.has(row.session_id) ? "running" : ""}"
        data-thread-session="${escapeHtml(row.session_id)}"
        data-thread-title="${escapeHtml(threadTitle(row))}"
        data-thread-pinned="${row.pinned ? "1" : "0"}">
        <button class="thread-open" data-session="${escapeHtml(row.session_id)}"
          title="${escapeHtml(threadTitle(row))}">${escapeHtml(threadTitle(row).slice(0, 34))}</button>
        ${cronSessions.has(row.session_id)
          ? '<span class="thread-running-indicator" title="定时提醒执行中" aria-label="定时提醒执行中"><i></i></span>'
          : `<span class="thread-actions">
              <button class="thread-action ${row.pinned ? "active" : ""}" data-pin="${escapeHtml(row.session_id)}"
                data-pinned="${row.pinned ? "1" : "0"}" title="${row.pinned ? "取消置顶" : "置顶"}" aria-label="${row.pinned ? "取消置顶" : "置顶"}">${icon("pin", 14)}</button>
              <button class="thread-action" data-archive="${escapeHtml(row.session_id)}"
                title="归档" aria-label="归档">${icon("archive", 14)}</button>
            </span>`}
      </div>`).join("")}`;
}

function bindThreadInteractions(root) {
  root.querySelectorAll("[data-active-job]").forEach(button => {
    button.onclick = () => resumeActiveJob(button.dataset.activeJob);
  });
  root.querySelectorAll("[data-session]").forEach(button => {
    button.onclick = () => openThread(button.dataset.session);
  });
  root.querySelectorAll("[data-pin]").forEach(button => {
    button.onclick = () => setThreadState(button.dataset.pin, {
      pinned: button.dataset.pinned !== "1",
    });
  });
  root.querySelectorAll("[data-archive]").forEach(button => {
    button.onclick = () => archiveThread(button.dataset.archive);
  });
  root.querySelectorAll("[data-thread-session]").forEach(item => {
    item.oncontextmenu = event => openThreadContextMenu(event, item);
  });
}

function projectUpdatedAt(project) {
  const timestamps = [project.created_at];
  state.conversations.forEach(row => {
    if (Number(row.project_id) === Number(project.id)) timestamps.push(row.created_at);
  });
  state.activeJobs.forEach(job => {
    if (Number(job.project_id) === Number(project.id)) timestamps.push(job.created_at);
  });
  return Math.max(...timestamps.map(value => new Date(value || 0).getTime()));
}

function projectGroupHtml(project) {
    const collection = collectThreads(project.id);
    const count = collection.active.length + collection.threads.length;
    const collapsed = isProjectCollapsed(project.id);
    const active = Number(project.id) === Number(state.activeProjectId);
    return `
      <div class="project-group ${collapsed ? "collapsed" : ""}" data-project-group="${project.id}">
        <div class="project-item ${active ? "active" : ""}">
          <button class="project-label" data-project-toggle="${project.id}" aria-expanded="${!collapsed}" aria-current="${active ? "page" : "false"}"
            title="单击${collapsed ? "展开" : "折叠"}，双击切换到项目：${escapeHtml(project.name)}">
            <span data-icon="folder" data-icon-size="14"></span>
            <span>${escapeHtml(project.name)}</span>
          </button>
          <span class="project-item-actions section-head-actions">
            <button class="sidebar-action-button section-more-button project-action" data-project-menu="${project.id}" data-pinned="${project.pinned ? "1" : "0"}"
              title="项目操作" aria-label="${escapeHtml(project.name)}项目操作">•••</button>
            <button class="sidebar-action-button project-action project-new-chat" data-project-start="${project.id}" title="在此项目中新建对话" aria-label="在${escapeHtml(project.name)}中新建对话">
              ${icon("plus", 13)}
            </button>
          </span>
        </div>
        <div class="project-thread-list">
          ${count ? threadCollectionHtml(collection, true) : '<div class="project-empty">暂无对话</div>'}
        </div>
      </div>`;
}

function bindProjectInteractions(root) {
  root.querySelectorAll("[data-project-toggle]").forEach(button => {
    button.onclick = () => {
      const id = Number(button.dataset.projectToggle);
      setProjectCollapsed(id, !isProjectCollapsed(id));
      renderProjects();
    };
    button.ondblclick = event => {
      event.preventDefault();
      openProject(Number(button.dataset.projectToggle));
    };
  });
  root.querySelectorAll("[data-project-start]").forEach(button => {
    button.onclick = event => {
      event.stopPropagation();
      const id = Number(button.dataset.projectStart);
      setProjectCollapsed(id, false);
      newChat({projectId: id});
    };
  });
  root.querySelectorAll("[data-project-menu]").forEach(button => {
    button.onclick = event => {
      const project = state.projects.find(row => Number(row.id) === Number(button.dataset.projectMenu));
      if (project) openProjectContextMenu(event, project);
    };
  });
  bindThreadInteractions(root);
  hydrateIcons(root);
}

function renderProjects() {
  const projects = [...state.projects].sort((a, b) => projectUpdatedAt(b) - projectUpdatedAt(a));
  const pinnedProjects = projects.filter(project => project.pinned);
  const pinnedThreads = collectPinnedThreads();
  const pinnedSection = $("pinned-section");
  const pinnedList = $("pinned-project-list");
  const projectList = $("project-list");

  pinnedSection.hidden = !pinnedProjects.length && !pinnedThreads.threads.length;
  pinnedList.innerHTML = [
    threadCollectionHtml(pinnedThreads),
    pinnedProjects.map(projectGroupHtml).join(""),
  ].join("");
  projectList.innerHTML = projects.length
    ? projects.map(projectGroupHtml).join("")
    : '<div class="project-empty root">还没有项目</div>';

  bindProjectInteractions(pinnedList);
  bindProjectInteractions(projectList);
  $("history-label").textContent = "最近";
  renderComposerProjectContext();
}

function runningJobStorageKey() {
  return `harness_active_chat_job:${Auth.username() || "anonymous"}`;
}

function chatViewStorageKey() {
  return `harness_chat_view:${Auth.username() || "anonymous"}`;
}

function composerDraftStorageKey(sessionId = state.sessionId, projectId = state.activeProjectId) {
  const context = sessionId || (projectId ? `project:${projectId}` : "new");
  return `harness_chat_draft:${Auth.username() || "anonymous"}:${context}`;
}

function saveComposerDraft() {
  const key = composerDraftStorageKey();
  if (query.value) sessionStorage.setItem(key, query.value);
  else sessionStorage.removeItem(key);
}

function restoreComposerDraft() {
  query.value = sessionStorage.getItem(composerDraftStorageKey()) || "";
  query.style.height = "auto";
  query.style.height = Math.min(query.scrollHeight, 220) + "px";
  setSendState();
}

function rememberChatView() {
  sessionStorage.setItem(chatViewStorageKey(), JSON.stringify({
    session_id: state.sessionId,
    project_id: state.activeProjectId,
  }));
}

function rememberedChatView() {
  try {
    return JSON.parse(sessionStorage.getItem(chatViewStorageKey()) || "null");
  } catch (_) {
    return null;
  }
}

function restoreRememberedView() {
  const remembered = rememberedChatView();
  if (
    remembered?.session_id
    && state.conversations.some(row => row.session_id === remembered.session_id)
  ) {
    openThread(remembered.session_id, {refreshSchedules: false, saveCurrentDraft: false});
    return;
  }
  if (
    remembered?.project_id
    && state.projects.some(project => Number(project.id) === Number(remembered.project_id))
  ) {
    newChat({projectId: Number(remembered.project_id), saveCurrentDraft: false});
    return;
  }
  sessionStorage.removeItem(chatViewStorageKey());
  restoreComposerDraft();
}

function rememberRunningJob(job) {
  localStorage.setItem(runningJobStorageKey(), JSON.stringify(job));
}

function rememberedRunningJob() {
  try {
    return JSON.parse(localStorage.getItem(runningJobStorageKey()) || "null");
  } catch (_) {
    return null;
  }
}

function forgetRunningJob(jobId = "") {
  const remembered = rememberedRunningJob();
  if (!jobId || !remembered || remembered.job_id === jobId) {
    localStorage.removeItem(runningJobStorageKey());
  }
}

function renderHistory() {
  const collection = collectThreads(null, state.recentSortMode);
  const total = collection.active.length + collection.threads.length;
  $("history-panel").innerHTML = total
    ? threadCollectionHtml(collection)
    : '<div class="empty-state">还没有对话</div>';
  bindThreadInteractions($("history-panel"));
}

async function loadHistory() {
  state.conversations = await api("/api/v1/chat/turns");
  renderProjects();
  renderHistory();
  renderAgentMenu();
}

async function loadProjects() {
  if (!state.projects.length) {
    try { await api("/api/v1/projects/default", {method: "POST"}); } catch (_) {}
  }
  state.projects = await api("/api/v1/projects");
  if (state.activeProjectId && !activeProject()) state.activeProjectId = null;
  renderProjects();
  renderHistory();
}

async function setProjectState(projectId, patch) {
  try {
    await api(`/api/v1/projects/${projectId}`, {method: "PATCH", json: patch});
    await loadProjects();
    return true;
  } catch (error) {
    showToast(`操作失败：${error.message}`);
    return false;
  }
}

async function archiveProject(projectId) {
  const wasActive = Number(state.activeProjectId) === Number(projectId);
  if (!await setProjectState(projectId, {archived: true})) return;
  if (wasActive) newChat({projectId: null});
  showToast("项目已归档");
}

async function setThreadState(sessionId, patch) {
  try {
    await api(`/api/v1/chat/threads/${encodeURIComponent(sessionId)}`, {
      method: "PATCH", json: patch,
    });
    await Promise.all([loadHistory(), loadProjects()]);
  } catch (error) {
    showToast(`操作失败：${error.message}`);
  }
}

async function archiveThread(sessionId) {
  await setThreadState(sessionId, {archived: true});
  if (state.sessionId === sessionId) newChat({projectId: state.activeProjectId});
  showToast("对话已归档");
}

function openThreadContextMenu(event, item) {
  event.preventDefault();
  event.stopPropagation();
  state.contextSessionId = item.dataset.threadSession;
  state.contextThreadTitle = item.dataset.threadTitle || "新对话";
  state.contextThreadPinned = item.dataset.threadPinned === "1";
  $("context-pin-label").textContent = state.contextThreadPinned ? "取消置顶" : "置顶";
  const menu = $("thread-context-menu");
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(8, Math.min(event.clientX, window.innerWidth - width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(event.clientY, window.innerHeight - height - 8))}px`;
}

function closeThreadContextMenu() {
  $("thread-context-menu").hidden = true;
}

function openProjectContextMenu(event, project) {
  event.preventDefault();
  event.stopPropagation();
  closeThreadContextMenu();

  state.contextProjectId = Number(project.id);
  state.contextProjectPinned = Boolean(project.pinned);
  document.querySelectorAll("[data-project-menu]").forEach(button => {
    button.classList.toggle("active", button === event.currentTarget);
  });
  $("project-context-pin-label").textContent = project.pinned ? "取消置顶" : "置顶";
  const menu = $("project-context-menu");
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(8, Math.min(event.clientX, window.innerWidth - width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(event.clientY, window.innerHeight - height - 8))}px`;
}

function closeProjectContextMenu() {
  $("project-context-menu").hidden = true;
  document.querySelectorAll("[data-project-menu]").forEach(button => button.classList.remove("active"));
}

function openRenameThreadDialog() {
  state.renameSessionId = state.contextSessionId;
  $("rename-thread-name").value = state.contextThreadTitle;
  $("rename-thread-error").textContent = "";
  closeThreadContextMenu();
  $("rename-thread-dialog").showModal();
  setTimeout(() => {
    $("rename-thread-name").focus();
    $("rename-thread-name").select();
  }, 0);
}

async function renameThread() {
  const title = $("rename-thread-name").value.trim();
  $("rename-thread-error").textContent = "";
  if (!title) {
    $("rename-thread-error").textContent = "请输入对话名称";
    return;
  }
  try {
    await api(`/api/v1/chat/threads/${encodeURIComponent(state.renameSessionId)}`, {
      method: "PATCH", json: {title},
    });
    $("rename-thread-dialog").close();
    await loadHistory();
    showToast("对话已重命名");
  } catch (error) {
    $("rename-thread-error").textContent = error.message;
  }
}

function setSectionCollapsed(section, collapsed, persist = true) {
  const container = $(`${section}-section`);
  const toggle = $(`${section}-section-toggle`);
  container.classList.toggle("collapsed", collapsed);
  toggle.setAttribute("aria-expanded", String(!collapsed));
  if (persist) localStorage.setItem(`chat_${section}_collapsed`, collapsed ? "1" : "0");
}

function toggleSection(section) {
  setSectionCollapsed(section, !$(`${section}-section`).classList.contains("collapsed"));
}

function openProject(projectId) {
  if (state.runningJob || state.submitting) {
    showToast("任务运行中，暂时不能切换项目");
    return;
  }
  state.activeProjectId = projectId;
  const project = state.projects.find(item => Number(item.id) === Number(projectId));
  if (project?.default_agent_id && state.agents.some(item => item.id === project.default_agent_id)) {
    state.agentId = project.default_agent_id;
    loadModels({reset: true}).catch(error => showToast(error.message));
    loadCatalog();
  }
  if (projectId) setProjectCollapsed(projectId, false);
  const latest = state.conversations.find(row => Number(row.project_id) === Number(projectId));
  if (projectId && latest) openThread(latest.session_id);
  else newChat({projectId});
  renderProjects();
  renderHistory();
}

async function loadActiveJobs() {
  const result = await api("/api/v1/chat/turns/active");
  state.activeJobs = (result.items || []).map(item => ({
    ...item, job_id: item.turn_id,
  }));
  renderStagedMessages();
  renderProjects();
  renderHistory();
  renderAgentMenu();
  return state.activeJobs;
}

function scheduledMessageMeta(item) {
  if (item?.source !== "cron") return "";
  const created = item.created_at ? new Date(item.created_at) : null;
  const time = created && !Number.isNaN(created.getTime())
    ? created.toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false})
    : "";
  return `<span class="scheduled-message-label">${icon("history", 11)} 定时提醒${time ? ` · ${escapeHtml(time)}` : ""}</span>`;
}

function scheduleStatusLabel(task) {
  if (!task.enabled) return "已暂停";
  return {
    pending: "等待执行",
    running: "正在执行",
    done: "上次成功",
    failed: "上次失败",
    cancelled: "已取消",
    scheduled: "等待提醒",
  }[task.status] || "等待提醒";
}

function scheduleTimeLabel(task) {
  if (["pending", "running"].includes(task.status)) return "现在";
  if (!task.enabled || !task.next_run_at) return "—";
  const target = new Date(task.next_run_at);
  if (Number.isNaN(target.getTime())) return String(task.next_run_at);
  const diff = target.getTime() - Date.now();
  const absolute = target.toLocaleString("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  if (diff <= 0) return `${absolute} · 即将执行`;
  const minutes = Math.ceil(diff / 60000);
  if (minutes < 60) return `${absolute} · ${minutes}分钟后`;
  const hours = Math.ceil(minutes / 60);
  if (hours < 24) return `${absolute} · ${hours}小时后`;
  return absolute;
}

function renderSchedules() {
  const panel = $("schedule-reminders");
  const rows = state.sessionId
    ? state.schedules.filter(task => task.session_id === state.sessionId)
    : [];
  panel.hidden = !rows.length;
  $("schedule-reminder-list").innerHTML = rows.map(task => `
    <div class="schedule-reminder ${escapeHtml(task.status || "scheduled")}">
      <strong title="${escapeHtml(task.name)}">${escapeHtml(task.name)}</strong>
      <time>${escapeHtml(scheduleTimeLabel(task))}</time>
      <span class="schedule-reminder-status"><i></i>${escapeHtml(scheduleStatusLabel(task))}</span>
      <p title="${escapeHtml(task.query)}">${escapeHtml(task.query)}</p>
    </div>`).join("");
}

async function loadSchedules({refreshThread = true} = {}) {
  if (!state.sessionId || !Auth.canModule("schedules")) {
    state.schedules = [];
    renderSchedules();
    return [];
  }
  const sessionId = state.sessionId;
  const rows = await api(`/api/v1/schedules?session_id=${encodeURIComponent(sessionId)}`);
  let completed = false;
  for (const task of rows) {
    const previous = state.scheduleSeen.get(task.id);
    const current = `${task.last_job_id || ""}:${task.status || ""}`;
    if (
      previous !== undefined
      && previous !== current
      && task.last_job_id
      && task.status === "done"
    ) completed = true;
    state.scheduleSeen.set(task.id, current);
  }
  if (state.sessionId !== sessionId) return [];
  state.schedules = rows;
  renderSchedules();
  await loadActiveJobs();
  if (completed && refreshThread && !state.runningJob && state.sessionId === sessionId) {
    await loadHistory();
    openThread(sessionId, {refreshSchedules: false});
    showToast("定时提醒已完成，结果已追加到当前对话");
  }
  return rows;
}

function stopSchedulePolling() {
  clearInterval(state.schedulePollTimer);
  state.schedulePollTimer = null;
}

async function pollSchedules() {
  if (document.hidden || state.pageLeaving || state.schedulePollInFlight) return;
  state.schedulePollInFlight = true;
  try {
    await loadSchedules();
  } catch (_) {
    // 保留当前展示；下一轮恢复后自动重试。
  } finally {
    state.schedulePollInFlight = false;
  }
}

function startSchedulePolling() {
  stopSchedulePolling();
  if (document.hidden || state.pageLeaving || !Auth.canModule("schedules")) return;
  state.schedulePollTimer = setInterval(pollSchedules, 10000);
}

async function refreshRestoredPage() {
  if (!state.sessionReady) return;
  const sessionId = state.sessionId;
  try {
    if (!await Auth.verifyCurrentSession()) return;
    await loadPreferences();
    await loadModels();
    await Promise.all([loadHistory(), loadProjects(), loadActiveJobs(), loadAgentStatuses()]);
    if (sessionId && state.conversations.some(row => row.session_id === sessionId)) {
      openThread(sessionId, {refreshSchedules: false, saveCurrentDraft: false});
    } else if (!state.runningJob) {
      state.sessionId = null;
      restoreRememberedView();
    }
    await loadSchedules({refreshThread: false});
  } catch (error) {
    showToast(`页面刷新失败：${error.message}`);
  } finally {
    startSchedulePolling();
    startAgentStatusPolling();
  }
}

function openThread(sessionId, {refreshSchedules = true, saveCurrentDraft = true} = {}) {
  if (state.runningJob || state.submitting) {
    showToast("当前任务正在运行，可在对话中向上查看历史内容；结束后可切换对话");
    return;
  }
  if (saveCurrentDraft) saveComposerDraft();
  state.sessionId = sessionId;
  resetRunDetails();
  state.followLatest = true;
  log.innerHTML = "";
  const rows = state.conversations
    .filter(row => row.session_id === sessionId)
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
  if (rows.length) {
    state.activeProjectId = rows[0].project_id || null;
    if (state.activeProjectId) setProjectCollapsed(state.activeProjectId, false);
  }
  for (const row of rows) {
    const article = addHistoryTurn(row);
    if (article && row.process) {
      resetRunDetails();
      startAgentWork(article, row.query, "正在恢复处理过程");
      replayAgentWork(article, row.process);
    }
  }
  const latest = rows.at(-1);
  if (latest && isTerminalConversationStatus(latest.status)) {
    markAgentStatusSeen(latest.agent_id, latest.turn_id || latest.id);
  }
  renderAgentMenu();
  renderHistory();
  renderProjects();
  rememberChatView();
  restoreComposerDraft();
  if (refreshSchedules) loadSchedules().catch(() => {});
  scrollToLatest(true);
}

function isDisplayableAnswer(text) {
  const value = String(text || "").trim();
  return Boolean(value)
    && !value.startsWith("工具已返回结果，但模型未能生成最终答复：");
}

function historyMessageText(row) {
  const status = String(row.status || "completed").toLowerCase();
  if (["queued", "pending", "running", "awaiting_approval"].includes(status)) return "";
  if (status === "cancelled") return isDisplayableAnswer(row.answer)
    ? row.answer : "已停止生成。";
  if (["failed", "dead_letter"].includes(status)) {
    if (isDisplayableAnswer(row.answer)) return row.answer;
    const error = String(row.error || "").trim();
    return error ? `执行失败：${error}` : "执行失败。";
  }
  return isDisplayableAnswer(row.answer)
    ? row.answer : "该任务未返回可展示结果，请重新生成。";
}

function addHistoryTurn(row) {
  addMessage("user", row.query, {
    meta: scheduledMessageMeta(row),
    context: {attachments: row.attachments || []},
  });
  const answer = historyMessageText(row);
  if (!answer) return null;
  return addMessage("assistant", answer, {
    meta: exportLinks(row.export_files),
  });
}

function newChat({projectId = null, saveCurrentDraft = true} = {}) {
  if (state.runningJob || state.submitting) return;
  if (saveCurrentDraft) saveComposerDraft();
  state.sessionId = null;
  resetRunDetails();
  state.followLatest = true;
  state.activeProjectId = projectId || null;
  releaseAttachmentPreviews(state.attachments);
  state.attachments = [];
  state.selectedDatasets.clear();
  state.selectedTemplates.clear();
  state.selectedSkills.clear();
  state.selectedMcp.clear();
  state.selectedAgentCalls.clear();
  state.schedules = [];
  renderAttachments();
  renderResources();
  renderWelcome();
  renderAgentMenu();
  renderProjects();
  renderHistory();
  renderSchedules();
  rememberChatView();
  restoreComposerDraft();
  setSendState();
  query.focus();
  scrollToLatest(true);
}

function renderAttachments() {
  $("attach-chips").innerHTML = state.attachments.map((file, index) => {
    const name = file.name || "未命名附件";
    const remove = `<button type="button" class="attachment-remove" data-file="${index}"
      title="移除附件" aria-label="移除附件 ${escapeHtml(name)}">×</button>`;
    if (isImageAttachment(file)) {
      return `<div class="attachment-card attachment-image" title="${escapeHtml(name)}">
        <img data-attachment-preview="${index}" alt="${escapeHtml(name)}" />${remove}</div>`;
    }
    const extension = name.includes(".") ? name.split(".").pop().toUpperCase() : "文件";
    return `<div class="attachment-card attachment-file" title="${escapeHtml(name)}">
      <span class="attachment-file-icon">${icon("fileText", 20)}</span>
      <span class="attachment-file-copy"><strong>${escapeHtml(name)}</strong>
        <small>${escapeHtml(extension)} · ${formatAttachmentSize(file.size)}</small></span>${remove}</div>`;
  }).join("");
  $("attach-chips").querySelectorAll("[data-attachment-preview]").forEach(image => {
    const file = state.attachments[Number(image.dataset.attachmentPreview)];
    if (file) image.src = attachmentPreviewUrl(file);
  });
  $("attach-chips").querySelectorAll("[data-file]").forEach(button => {
    button.onclick = () => {
      if (state.submitting) return;
      const [removed] = state.attachments.splice(Number(button.dataset.file), 1);
      releaseAttachmentPreview(removed);
      renderAttachments();
    };
  });
  setSendState();
}

function isImageAttachment(file) {
  return String(file?.type || "").startsWith("image/")
    || /\.(?:png|jpe?g|gif|webp)$/i.test(file?.name || "");
}

function attachmentPreviewUrl(file) {
  let url = attachmentPreviewUrls.get(file);
  if (!url) {
    url = URL.createObjectURL(file);
    attachmentPreviewUrls.set(file, url);
  }
  return url;
}

function releaseAttachmentPreview(file) {
  const url = file && attachmentPreviewUrls.get(file);
  if (!url) return;
  URL.revokeObjectURL(url);
  attachmentPreviewUrls.delete(file);
}

function releaseAttachmentPreviews(files) {
  for (const file of files || []) releaseAttachmentPreview(file);
}

function formatAttachmentSize(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.max(0.1, size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function addComposerAttachments(files, source = "选择", announce = true) {
  if (state.submitting) return 0;
  const added = Array.from(files || []).filter(file => file instanceof File);
  if (!added.length) return 0;
  state.attachments.push(...added);
  renderAttachments();
  if (announce) showToast(`${source}添加了 ${added.length} 个附件`);
  return added.length;
}

function pastedTextFilename(now = new Date()) {
  const parts = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
    "-",
    String(now.getHours()).padStart(2, "0"),
    String(now.getMinutes()).padStart(2, "0"),
    String(now.getSeconds()).padStart(2, "0"),
  ];
  return `粘贴文本-${parts.join("")}.txt`;
}

function handleComposerPaste(event) {
  if (state.submitting) { event.preventDefault(); return; }
  const clipboard = event.clipboardData;
  if (!clipboard) return;
  const files = Array.from(clipboard.files || []);
  if (files.length) {
    event.preventDefault();
    addComposerAttachments(files, "粘贴");
    return;
  }
  const text = clipboard.getData("text/plain");
  if (text.length < PASTED_TEXT_ATTACHMENT_THRESHOLD) return;
  event.preventDefault();
  const attachment = new File([text], pastedTextFilename(), {
    type: "text/plain;charset=utf-8",
    lastModified: Date.now(),
  });
  addComposerAttachments([attachment], "粘贴", false);
  showToast(`粘贴内容较长，已转为附件（${text.length.toLocaleString()} 字符）`);
}

function isFileDrag(event) {
  return Array.from(event.dataTransfer?.types || []).includes("Files");
}

function setAttachmentDropActive(active) {
  $("attachment-drop-overlay").classList.toggle("active", active);
  $("attachment-drop-overlay").setAttribute("aria-hidden", String(!active));
}

function clearAttachmentDragState() {
  attachmentDragDepth = 0;
  setAttachmentDropActive(false);
}

function installAttachmentDropTarget() {
  conversationPane.addEventListener("dragenter", event => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    attachmentDragDepth += 1;
    setAttachmentDropActive(true);
  });
  conversationPane.addEventListener("dragover", event => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  conversationPane.addEventListener("dragleave", event => {
    if (!isFileDrag(event)) return;
    attachmentDragDepth = Math.max(0, attachmentDragDepth - 1);
    if (!attachmentDragDepth) setAttachmentDropActive(false);
  });
  conversationPane.addEventListener("drop", event => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    const files = Array.from(event.dataTransfer?.files || []);
    clearAttachmentDragState();
    addComposerAttachments(files, "拖放");
  });
  document.addEventListener("dragend", clearAttachmentDragState);
}

function renderResources() {
  const pills = [];
  for (const key of state.selectedDatasets) {
    const item = state.datasets.find(x => (x.key || x.id) == key);
    pills.push(resourceTokenHtml("dataset", key, item?.name || key, "知识库", "database", "mention-pill"));
  }
  for (const id of state.selectedTemplates) {
    const item = state.templates.find(x => x.id == id);
    pills.push(resourceTokenHtml("template", id, item?.name || String(id), "模板", "fileText", "mention-pill"));
  }
  for (const id of state.selectedSkills) {
    const item = state.skills.find(x => x.id == id);
    pills.push(resourceTokenHtml("skill", id, capabilityLabel(item, id), "Skill", "package", "command-pill"));
  }
  for (const id of state.selectedMcp) {
    const item = state.mcpServers.find(x => x.id == id);
    pills.push(resourceTokenHtml("mcp", id, item?.name || String(id), "MCP", "plug", "command-pill"));
  }
  for (const id of state.selectedAgentCalls) {
    const item = state.callableAgents.find(x => x.id == id);
    pills.push(resourceTokenHtml("agent", id, item?.name || String(id), "智能体", "bot", "command-pill"));
  }
  $("mention-pills").innerHTML = pills.join("");
  syncResourceTokenLayout();
}

function resourceTokenHtml(kind, id, label, typeLabel, iconName, className) {
  const text = String(label || id);
  return `<span class="context-pill ${className}" data-resource-kind="${kind}"
    data-resource-id="${escapeHtml(String(id))}"
    title="${escapeHtml(typeLabel)} · ${escapeHtml(text)}（Backspace 移除）">
    ${icon(iconName, 13)}<span>${escapeHtml(text)}</span></span>`;
}

function removeLastResourceToken() {
  const token = $("mention-pills").querySelector(".context-pill:last-child");
  if (!token) return false;
  const kind = token.dataset.resourceKind;
  const raw = token.dataset.resourceId;
  if (kind === "dataset") state.selectedDatasets.delete(raw);
  else if (kind === "template") state.selectedTemplates.delete(Number(raw));
  else if (kind === "skill") state.selectedSkills.delete(Number(raw));
  else if (kind === "mcp") state.selectedMcp.delete(Number(raw));
  else if (kind === "agent") state.selectedAgentCalls.delete(Number(raw));
  else return false;
  renderResources();
  setSendState();
  return true;
}

function syncResourceTokenLayout() {
  const editor = $("composer-editor");
  const pills = $("mention-pills");
  editor.classList.remove("tokens-stacked");
  editor.style.removeProperty("--composer-token-indent");
  updateComposerPlaceholder();
  if (!pills.childElementCount) return;
  const tokensWidth = pills.scrollWidth;
  const stacked = tokensWidth > editor.clientWidth * 0.68;
  editor.classList.toggle("tokens-stacked", stacked);
  if (!stacked) editor.style.setProperty("--composer-token-indent", `${tokensWidth + 7}px`);
}

function captureComposerContext() {
  return {
    agentId: state.agentId,
    selection: {...state.composerSelection},
    attachments: [...state.attachments],
    datasets: [...state.selectedDatasets],
    templates: [...state.selectedTemplates],
    skills: [...state.selectedSkills],
    mcpServers: [...state.selectedMcp],
    agentCalls: [...state.selectedAgentCalls],
  };
}

function clearComposerContext() {
  releaseAttachmentPreviews(state.attachments);
  state.attachments = [];
  state.selectedDatasets.clear();
  state.selectedTemplates.clear();
  state.selectedSkills.clear();
  state.selectedMcp.clear();
  state.selectedAgentCalls.clear();
  renderAttachments();
  renderResources();
}

function restoreComposerContext(context) {
  releaseAttachmentPreviews(state.attachments);
  state.attachments = [...context.attachments];
  for (const id of context.datasets) state.selectedDatasets.add(id);
  for (const id of context.templates) state.selectedTemplates.add(id);
  for (const id of context.skills) state.selectedSkills.add(id);
  for (const id of context.mcpServers) state.selectedMcp.add(id);
  for (const id of context.agentCalls) state.selectedAgentCalls.add(id);
  renderAttachments();
  renderResources();
}

async function loadCatalog() {
  if (!state.agentId) return;
  const catalog = await api(`/api/v1/chat/catalog?agent_id=${state.agentId}`);
  const mentions = catalog.mentions || [];
  const commands = catalog.commands || [];
  state.datasets = mentions.filter(item => item.kind === "dataset");
  state.templates = mentions.filter(item => item.kind === "template");
  state.skills = commands.filter(item => item.kind === "skill");
  state.mcpServers = commands.filter(item => item.kind === "mcp");
  state.callableAgents = commands.filter(item => item.kind === "agent");
  state.catalogLoadedAt = Date.now();
}

function paletteItems() {
  const source = state.paletteMode === "mention"
    ? [...state.datasets, ...state.templates]
    : [...state.skills, ...state.mcpServers, ...state.callableAgents];
  const term = (state.activeTrigger?.term || "").toLowerCase();
  if (!term) return source;
  return source.filter(item =>
    `${item.name} ${capabilityLabel(item)} ${item.description || ""} ${item.kind}`.toLowerCase().includes(term)
  );
}

function paletteMeta(item) {
  return {
    dataset: ["知识库", "search"],
    template: ["模板", "panel"],
    skill: ["Skill", "shield"],
    mcp: ["MCP", "external"],
    agent: ["智能体", "sparkles"],
  }[item.kind] || ["能力", "sparkles"];
}

function renderPalette() {
  const picker = $("resource-picker");
  const items = paletteItems();
  state.paletteIndex = Math.max(0, Math.min(state.paletteIndex, items.length - 1));
  const symbol = state.paletteMode === "mention" ? "@" : "/";
  picker.innerHTML = `
    <div class="palette-head">
      <div><kbd>${symbol}</kbd><strong>${state.paletteMode === "mention" ? "引用上下文" : "调用能力"}</strong></div>
      <span>↑↓ 选择 · Enter 确认 · Esc 关闭</span>
    </div>
    <div class="palette-list">
      ${items.map((item, index) => {
        const [label, iconName] = paletteMeta(item);
        return `<button type="button" class="palette-item ${index === state.paletteIndex ? "active" : ""}"
          data-palette-index="${index}" data-kind="${item.kind}" data-id="${escapeHtml(String(item.id))}" role="option">
          <span class="palette-icon">${icon(iconName, 16)}</span>
          <span><strong>${escapeHtml(capabilityLabel(item))}</strong><small>${escapeHtml(item.description || label)}</small></span>
          <em>${label}${item.bound ? " · 已绑定" : ""}</em>
        </button>`;
      }).join("") || `<div class="palette-empty">没有匹配的${state.paletteMode === "mention" ? "知识库或模板" : "能力"}</div>`}
    </div>`;
  picker.querySelectorAll("[data-palette-index]").forEach(button => {
    button.onmouseenter = () => {
      state.paletteIndex = Number(button.dataset.paletteIndex);
      picker.querySelectorAll(".palette-item").forEach((item, index) =>
        item.classList.toggle("active", index === state.paletteIndex)
      );
    };
    button.onclick = () => selectPaletteItem(items[Number(button.dataset.paletteIndex)]);
  });
}

function closePalette() {
  state.paletteMode = null;
  state.activeTrigger = null;
  $("resource-picker").classList.remove("open");
}

function openPalette(mode, trigger = null) {
  state.paletteMode = mode;
  state.activeTrigger = trigger;
  state.paletteIndex = 0;
  renderPalette();
  $("resource-picker").classList.add("open");
  if (Date.now() - state.catalogLoadedAt > 30000 && !state.catalogLoading) {
    state.catalogLoading = loadCatalog()
      .then(() => {
        if (state.paletteMode) renderPalette();
      })
      .catch(() => showToast("能力目录刷新失败，已保留当前列表"))
      .finally(() => { state.catalogLoading = null; });
  }
}

function findComposerTrigger() {
  const cursor = query.selectionStart;
  const before = query.value.slice(0, cursor);
  const match = before.match(/(^|\s)([@/])([^\s@/]*)$/);
  if (!match) return null;
  const symbolIndex = cursor - match[2].length - match[3].length;
  return {
    mode: match[2] === "@" ? "mention" : "command",
    term: match[3],
    start: symbolIndex,
    end: cursor,
  };
}

function removeTriggerText() {
  const trigger = state.activeTrigger;
  if (!trigger) return;
  query.value = query.value.slice(0, trigger.start) + query.value.slice(trigger.end);
  query.selectionStart = query.selectionEnd = trigger.start;
  resizeComposer();
}

function selectPaletteItem(item) {
  if (!item) return;
  if (item.kind === "dataset") state.selectedDatasets.add(String(item.id));
  else if (item.kind === "template") state.selectedTemplates.add(Number(item.id));
  else if (item.kind === "skill") state.selectedSkills.add(Number(item.id));
  else if (item.kind === "mcp") state.selectedMcp.add(Number(item.id));
  else if (item.kind === "agent") state.selectedAgentCalls.add(Number(item.id));
  removeTriggerText();
  renderResources();
  closePalette();
  query.focus();
}

function insertTrigger(symbol) {
  const start = query.selectionStart;
  const needsSpace = start > 0 && !/\s/.test(query.value[start - 1]);
  query.setRangeText(`${needsSpace ? " " : ""}${symbol}`, start, query.selectionEnd, "end");
  resizeComposer();
  openPalette(symbol === "@" ? "mention" : "command", findComposerTrigger());
  query.focus();
}

function closeAddMenu() {
  $("composer-add-menu").classList.remove("open");
  $("add-menu-btn").classList.remove("active");
  $("add-menu-btn").setAttribute("aria-expanded", "false");
}

function toggleAddMenu() {
  if (state.submitting) return;
  closeComposerMenus();
  const open = !$("composer-add-menu").classList.contains("open");
  closeAddMenu();
  closePalette();
  if (open) {
    $("composer-add-menu").classList.add("open");
    $("add-menu-btn").classList.add("active");
    $("add-menu-btn").setAttribute("aria-expanded", "true");
  }
}

function toggleVoiceInput() {
  if (state.voiceListening) {
    state.voiceRecognition?.stop();
    return;
  }
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    showToast("当前浏览器不支持语音输入，请直接键入消息");
    query.focus();
    return;
  }
  const recognition = new Recognition();
  state.voiceRecognition = recognition;
  state.voiceBase = query.value.trimEnd();
  recognition.lang = "zh-CN";
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.onstart = () => {
    state.voiceListening = true;
    $("mic-btn").classList.add("listening");
    $("mic-btn").setAttribute("aria-label", "停止语音输入");
    showToast("正在聆听…");
  };
  recognition.onresult = event => {
    let transcript = "";
    for (let index = 0; index < event.results.length; index++) {
      transcript += event.results[index][0]?.transcript || "";
    }
    const spacer = state.voiceBase && transcript ? " " : "";
    query.value = `${state.voiceBase}${spacer}${transcript}`;
    query.selectionStart = query.selectionEnd = query.value.length;
    resizeComposer();
  };
  recognition.onerror = event => {
    if (!["aborted", "no-speech"].includes(event.error)) showToast(`语音输入不可用：${event.error}`);
  };
  recognition.onend = () => {
    state.voiceListening = false;
    state.voiceRecognition = null;
    $("mic-btn").classList.remove("listening");
    $("mic-btn").setAttribute("aria-label", "开始语音输入");
    setSendState();
  };
  recognition.start();
}

function resetRunDetails() {
  $("details-toggle").hidden = true;
  setRunDrawer(false);
  $("run-events").innerHTML = "";
  $("run-phase-summary").hidden = true;
  $("run-phase-summary").innerHTML = "";
  $("run-summary").innerHTML = "";
  $("run-status").textContent = "尚未运行";
  $("run-status").className = "";
  resetEvidenceCenter();
  filterRunEvents();
}

function updateRunPhase(article, eventType, payload = {}, detail = "") {
  const projection = article?._runWorkspace;
  if (!projection || !RunWorkspace.observe(projection, {event_type: eventType, payload}, detail)) return;
  const label = RunWorkspace.PHASES[projection.phase];
  $("run-status").textContent = label;
  $("run-status").className = {completed: "done", completed_with_issues: "warning",
    failed: "failed", cancelled: "cancelled", approval: "warning"}[projection.phase] || "running";
  const inline = article.querySelector(".run-phase-inline");
  if (inline) {
    inline.hidden = false;
    inline.className = `run-phase-inline ${projection.phase}`;
    inline.innerHTML = `<i aria-hidden="true"></i><span>${escapeHtml(label)}</span>`;
  }
  const panel = $("run-phase-summary");
  panel.hidden = false;
  panel.innerHTML = `<div class="run-phase-heading"><span>当前阶段</span><strong>${escapeHtml(label)}</strong></div>
    <p>${escapeHtml(projection.detail)}</p>
    <div class="run-phase-history" aria-label="本次已记录的阶段">${projection.visited.map(phase =>
      `<span ${phase === projection.phase ? 'aria-current="step"' : ""}>${escapeHtml(RunWorkspace.PHASES[phase])}</span>`
    ).join("")}</div>`;
}

function filterRunEvents() {
  let visible = 0;
  const events = $("run-events").querySelectorAll(".run-event");
  for (const row of events) {
    row.hidden = !RunWorkspace.matchesFilter(row.dataset.category, state.runEventFilter);
    if (!row.hidden) visible += 1;
  }
  $("run-events-empty").hidden = visible > 0;
  $("run-events-empty").textContent = !events.length
    ? "暂无执行记录，发送任务后可在这里查看。"
    : {tools: "本次任务尚无工具执行记录。", approval: "本次任务尚无审批记录。",
      verification: "本次任务尚无验证记录。"}[state.runEventFilter] || "暂无执行记录。";
  $("run-event-filters").querySelectorAll("[data-event-filter]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.eventFilter === state.runEventFilter));
  });
}

function recordWorkspaceDrawerEvent(eventType, payload, presentation, timestamp = "") {
  const fallback = {
    "verification.failed": {text: "验证发现缺口，等待修正", kind: "warning"},
    "verification.completed": {text: payload.provisional ? "基础验证完成，仍需核验产物"
      : payload.passed === false ? "验证完成，存在未满足条件" : "完成条件验证通过",
      kind: payload.provisional || payload.passed === false ? "warning" : "done"},
    "approval.requested": {text: `请求确认：${payload.description || payload.scope || "当前操作"}`, kind: "warning"},
    "approval.granted": {text: "用户已批准本次操作", kind: "done"},
    "delegation.started": {text: "专家任务已开始", kind: "stage-running"},
    "delegation.completed": {text: "专家任务已返回结果", kind: "done"},
    "delegation.failed": {text: "专家任务执行失败", kind: "failed"},
    "delegation.awaiting_approval": {text: "专家任务需要确认操作", kind: "warning"},
  }[eventType];
  const item = presentation || fallback;
  if (!item) return;
  const nested = payload.execution_scope === "inline_subagent";
  const eventKey = !nested && runtimeActivityIdentity(eventType, payload) === "terminal-outcome"
    ? "terminal-outcome" : "";
  addTurnEvent(`${nested ? "专家任务 · " : ""}${item.text}`, item.kind, eventKey, RunWorkspace.eventCategory(eventType), timestamp);
}

function showRunApproval(jobId, article, details) {
  const slot = article?.querySelector(".run-approval-slot");
  if (!slot) return;
  const previous = article._approvalRequest;
  if (previous?.resolved && details.event_id && previous.event_id === details.event_id) return;
  if (previous && !previous.resolved && previous.scope === details.scope) return;
  const request = RunWorkspace.createApprovalRequest(details);
  article._approvalRequest = request;
  slot.innerHTML = `<section class="run-approval-card" aria-label="确认本次操作">
    <div class="run-approval-title">${icon("shield", 17)}<strong>这一步需要你的确认</strong></div>
    <p>${escapeHtml(details.description || details.scope || "当前操作")}</p>
    <small>批准仅适用于本次操作；拒绝会停止当前任务。</small>
    <p class="run-approval-error" role="alert" hidden></p>
    <div class="run-approval-actions">
      <button type="button" class="btn" data-approval-action="approve">批准本次</button>
      <button type="button" class="btn ghost" data-approval-action="cancel">拒绝并停止</button>
    </div>
  </section>`;
  setRunStatus("等待你的确认", "warning");
  updateRunPhase(article, "approval.requested", details, details.description);
  addAgentWorkActivity(article, `等待批准：${details.description || details.scope}`, "running", "approval");
  slot.querySelectorAll("[data-approval-action]").forEach(button => {
    button.onclick = async () => {
      const action = button.dataset.approvalAction;
      const result = RunWorkspace.decideApproval(request, action,
        next => api(`/api/v1/chat/turns/${jobId}/${next}`, {method: "POST"}));
      slot.querySelectorAll("button").forEach(item => { item.disabled = request.pending; });
      slot.querySelector(".run-approval-card").setAttribute("aria-busy", String(request.pending));
      const succeeded = await result;
      if (article._approvalRequest !== request) return;
      if (succeeded) {
        const hadFocus = slot.contains(document.activeElement);
        slot.innerHTML = "";
        addTurnEvent(action === "approve" ? "已批准本次操作" : "用户已拒绝并请求停止", action === "approve" ? "done" : "cancelled", "", "approval");
        if (action === "approve") {
          setRunStatus("已批准，正在继续", "running");
          updateRunPhase(article, "approval.granted", {}, "已批准本次操作，等待任务继续");
        } else setRunStatus("正在停止", "warning");
        if (hadFocus) query.focus({preventScroll: true});
      } else {
        slot.querySelectorAll("button").forEach(item => { item.disabled = false; });
        slot.querySelector(".run-approval-card").setAttribute("aria-busy", "false");
        const error = slot.querySelector(".run-approval-error");
        error.hidden = false;
        error.textContent = `未能提交决定：${request.error}。请重试。`;
      }
    };
  });
}

function restoreRunApproval(jobId, article, snapshot) {
  const details = RunWorkspace.approvalFromSnapshot(snapshot);
  if (details) showRunApproval(jobId, article, details);
  else if (snapshot.status) settleRunApproval(article);
}

function settleRunApproval(article) {
  if (article._approvalRequest) article._approvalRequest.resolved = true;
  article._approvalRequest = null;
  const slot = article.querySelector(".run-approval-slot");
  if (slot) slot.innerHTML = "";
}

function setRunStatus(text, kind = "") {
  $("run-status").textContent = text;
  $("run-status").className = kind;
  $("run-summary").innerHTML = state.runningJob
    ? `<div><span>Turn ID</span><code>${escapeHtml(state.runningJob)}</code></div>
       <div><span>Harness</span><b>v${activeAgent()?.active_version || 1}</b></div>` : "";
}

function resetEvidenceCenter() {
  const panel = $("evidence-center");
  if (!panel) return;
  panel.hidden = true;
  panel.innerHTML = "";
}

function renderEvaluationReport(report = {}) {
  const panel = $("evidence-center");
  if (!panel || !report || typeof report !== "object") return;
  const tree = report.evidence_tree && typeof report.evidence_tree === "object"
    ? report.evidence_tree : {};
  const nodes = Array.isArray(tree.children) ? tree.children : [];
  const decision = String(report.decision || "needs_attention");
  const decisionLabel = {
    passed: "通过",
    needs_attention: "需关注",
    failed: "失败",
  }[decision] || "待复核";
  const score = Math.max(0, Number(report.score || 0));
  const coverage = Math.round(Math.max(0, Number(report.coverage || 0)) * 100);
  const skills = nodes.map(node => {
    const checks = Array.isArray(node.checks) ? node.checks : [];
    const statusLabel = {
      passed: "通过", failed: "失败", unknown: "待补证",
    }[String(node.status || "unknown")] || "待补证";
    return `<details class="evidence-skill" ${node.status === "failed" ? "open" : ""}>
      <summary><strong>${escapeHtml(node.label || node.skill || "评测能力")}</strong><span>${escapeHtml(statusLabel)}</span></summary>
      <div class="evidence-checks">
        ${checks.map(check => `<div class="evidence-check ${escapeHtml(check.status || "unknown")}">
          <i aria-hidden="true"></i>
          <span><strong>${escapeHtml(check.label || "原子检查")}</strong><br>${escapeHtml(check.observed || "暂无证据")}</span>
        </div>`).join("")}
      </div>
    </details>`;
  }).join("");
  const gaps = Array.isArray(report.skill_gaps) ? report.skill_gaps.filter(Boolean) : [];
  panel.innerHTML = `
    <div class="evidence-head">
      <strong>Evidence Tree · ${escapeHtml(decisionLabel)}</strong>
      <span class="evidence-score ${escapeHtml(decision)}">${score} 分 · 覆盖 ${coverage}%</span>
    </div>
    <div class="evidence-skills">${skills || "<small>暂无可展示的原子检查</small>"}</div>
    ${gaps.length ? `<p class="evidence-gaps">评测能力缺口：${escapeHtml(gaps.join("、"))}</p>` : ""}`;
  panel.hidden = false;
}

function addTurnEvent(text, kind = "progress", key = "", category = "activity", timestamp = "") {
  const observedAt = timestamp ? new Date(timestamp) : new Date();
  const timeValue = Number.isNaN(observedAt.getTime()) ? "" : observedAt.toISOString();
  const timeLabel = Number.isNaN(observedAt.getTime()) ? "—"
    : observedAt.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  const cleanKey = String(key || "");
  const existing = cleanKey
    ? Array.from($("run-events").querySelectorAll(".run-event[data-event-key]"))
      .find(item => item.dataset.eventKey === cleanKey)
    : null;
  if (existing) {
    existing.className = `run-event ${kind}`;
    existing.dataset.category = category;
    existing.querySelector("span").textContent = text;
    existing.querySelector("time").textContent = timeLabel;
    existing.querySelector("time").dateTime = timeValue;
    filterRunEvents();
    return existing;
  }
  const el = document.createElement("div");
  el.className = `run-event ${kind}`;
  el.dataset.category = category;
  if (cleanKey) el.dataset.eventKey = cleanKey;
  el.innerHTML = `<i></i><span>${escapeHtml(text)}</span><time datetime="${escapeHtml(timeValue)}">${escapeHtml(timeLabel)}</time>`;
  $("run-events").appendChild(el);
  filterRunEvents();
  return el;
}

const TOOL_ACTIONS = {
  read: "读取文件", read_many: "读取文件", write: "创建文件", edit: "编辑文件",
  multi_edit: "批量编辑文件", apply_patch: "应用文件补丁", shell: "执行命令",
  ls: "查看目录", glob: "查找文件", grep: "搜索文件内容",
  git_status: "检查 Git 状态", git_diff: "检查代码差异",
  web_search: "搜索网络", web_fetch: "读取网页", browser: "操作浏览器",
  document_inspect: "检查 Word 文档", document_create: "创建 Word 文档",
  document_format: "规范 Word 格式",
};

function runtimeEventPresentation(eventType, payload = {}, work = null) {
  const recovery = RunWorkspace.recoveryPresentation(eventType, payload);
  if (recovery) return recovery;
  if (["model.role.selected", "model.role.fallback", "tools.selection"].includes(eventType)) {
    return ChatWorkspace.modelRoutingPresentation(eventType, payload);
  }
  if (eventType === "guardrail.awaiting_review") {
    return {text: "已通知有护栏权限的管理员审批，最多等待5分钟", kind: "running"};
  }
  if (eventType === "guardrail.reviewed") {
    return {text: payload.status === "approved" ? "管理员已批准本次护栏检查" : "管理员未批准，调用已阻止", kind: payload.status === "approved" ? "done" : "error"};
  }
  if (eventType === "guardrail.content_evaluated") {
    return RunWorkspace.contentGuardrailPresentation(payload);
  }
  if (eventType === "approval.policy") {
    const label = APPROVAL_POLICIES[payload.policy]?.label || "请求批准";
    return {text: `批准策略：${label}`, kind: "done"};
  }
  if (eventType === "approval.auto_approved") {
    const scope = String(payload.scope || "有副作用操作");
    const action = TOOL_ACTIONS[scope] || scope;
    return {text: `已按批准策略自动放行：${action}`, kind: "done"};
  }
  if (eventType.startsWith("task.")) {
    let statusValue = String(
      payload.completion_status
      || payload.status
      || {
        "task.completed": "completed",
        "task.completed_with_issues": "completed_with_issues",
        "task.failed": "failed",
        "task.cancelled": "cancelled",
      }[eventType]
      || ""
    );
    if (
      statusValue === "completed"
      && agentWorkCompletionState(work || {}) === "completed_with_issues"
    ) {
      statusValue = "completed_with_issues";
    }
    const text = {
      queued: "任务已排队",
      planning: "正在制定任务计划",
      executing: "正在执行任务计划",
      waiting_approval: "任务正在等待批准",
      finalizing: "正在核验并整理最终结果",
      completed_with_issues: "任务已受限完成，部分计划步骤未完成",
      completed: "任务执行完成",
      failed: "任务执行失败",
      cancelled: "任务已取消",
    }[statusValue];
    if (text) return {
      text,
      kind: statusValue === "completed"
        ? "done"
        : statusValue === "completed_with_issues"
          ? "warning"
          : statusValue === "failed" ? "failed" : "stage-running",
    };
  }
  if (eventType.startsWith("step.")) {
    const title = String(payload.step || "当前任务步骤");
    return {
      text: {
        "step.started": `开始：${title}`,
        "step.completed": `完成：${title}`,
        "step.failed": `失败：${title}`,
        "step.blocked": `阻塞：${title}`,
        "step.skipped": `跳过：${title}`,
      }[eventType] || title,
      kind: eventType === "step.failed" ? "failed"
        : eventType === "step.blocked" ? "warning"
          : eventType === "step.started" ? "stage-running" : "done",
    };
  }
  if (eventType === "turn.started") {
    return {text: "Turn 已开始", kind: "stage-running"};
  }
  if (eventType === "loop.iteration.started") {
    return {text: `动态循环第 ${Number(payload.iteration || 1)} 轮`, kind: "stage-running"};
  }
  if (eventType === "loop.stopped") {
    return {text: runtimeStopReasonText(payload.reason), kind: "warning"};
  }
  if (eventType === "plan.closeout.started") {
    const reason = runtimeStopReasonText(payload.reason);
    return {text: `${reason}，正在依据已有证据收尾未完成计划`, kind: "stage-running"};
  }
  if (eventType === "plan.closeout.completed") {
    const plan = agentWorkPlanSnapshot(work?.steps || []);
    const allCompleted = payload.all_completed === true || plan.allCompleted;
    return {
      text: allCompleted
        ? "计划收尾完成：全部步骤均已完成"
        : `${plan.label}；最终结果将明确保留未完成限制`,
      kind: allCompleted ? "done" : "warning",
    };
  }
  if (eventType === "verification.started") {
    return {text: "正在核验完成条件", kind: "stage-running"};
  }
  if (eventType === "evaluation.started") {
    const count = Array.isArray(payload.selected_skills) ? payload.selected_skills.length : 0;
    return {text: `已动态选择 ${count} 项评测能力`, kind: "stage-running"};
  }
  if (eventType === "evaluation.completed") {
    const failed = payload.decision === "failed";
    const limited = payload.decision === "needs_attention";
    return {
      text: failed ? "Evidence Tree 判定未通过"
        : limited ? "Evidence Tree 已形成，仍有待补证项"
          : "Evidence Tree 已形成并通过交叉检查",
      kind: failed ? "failed" : limited ? "warning" : "done",
    };
  }
  if (eventType === "guidance.applied") {
    return {text: "已吸收运行中的补充引导", kind: "done"};
  }
  if (eventType === "interaction.interrupt.received") {
    return {text: "已接收打断请求，正在停止旧目标", kind: "warning"};
  }
  if (eventType === "interaction.redirect.queued") {
    return {text: "替代目标已进入同一对话队列", kind: "stage-running"};
  }
  if (["interaction.redirect.created", "interaction.redirect.applied"].includes(eventType)) {
    return {text: "已切换到用户重定向的新目标", kind: "done"};
  }
  if (eventType === "attachments.resolved") {
    const count = Number(payload.count || 0);
    return {
      text: payload.inherited
        ? `已延续前序任务的 ${count} 个附件`
        : `已解析本轮 ${count} 个附件`,
      kind: "done",
    };
  }
  if (eventType === "attachments.materialized") {
    return {text: "附件已安全载入本轮工作区", kind: "done"};
  }
  if (eventType === "attachments.visual_source") {
    const page = Number(payload.page || 0);
    const total = Number(payload.source_page_count || 0);
    return {
      text: `${payload.ok ? "已核对" : "未能识读"}第 ${page}/${total} 页图像资料`,
      kind: payload.ok ? "done" : "failed",
    };
  }
  if (eventType === "loop.completed" || eventType === "turn.completed") {
    const version = work?.loopVersion ? ` v${work.loopVersion}` : "";
    const subject = eventType === "turn.completed" ? "Turn" : `Agent Loop${version}`;
    const limited = payload.completion_status === "completed_with_issues"
      || payload.checkpoint?.status === "completed_with_issues"
      || agentWorkCompletionState(work || {}) === "completed_with_issues";
    return limited
      ? {text: `${subject} 已结束：部分计划步骤未完成`, kind: "warning"}
      : {text: `${subject} 执行完成`, kind: "done"};
  }
  if (!eventType.startsWith("tool.")) return null;
  const tool = String(payload.tool || "工具");
  const action = TOOL_ACTIONS[tool] || `执行工具 ${tool}`;
  const targets = Array.isArray(payload.targets) ? payload.targets.filter(Boolean) : [];
  const target = targets.length ? `：${targets.map(value => String(value)).join("、")}` : "";
  if (eventType === "tool.called") {
    return {text: `正在${action}${target}`, kind: "tool-running"};
  }
  if (eventType === "tool.completed") {
    return payload.ok === false
      ? {text: `${action}失败${target}`, kind: "failed"}
      : {text: `${action}完成${target}`, kind: "done"};
  }
  if (eventType === "tool.deferred") {
    return {text: `${action}已延后`, kind: "progress"};
  }
  return null;
}

function recordRuntimeActivity(article, eventType, payload, presentation) {
  if (payload.execution_scope === "inline_subagent") return;
  const reportedTerminal = payload.status === "completed"
    || ["task.completed", "task.completed_with_issues", "turn.completed", "loop.completed"].includes(eventType);
  const phasePayload = reportedTerminal
    && agentWorkCompletionState(article._agentWork) === "completed_with_issues"
    ? {...payload, completion_status: "completed_with_issues"} : payload;
  updateRunPhase(article, eventType, phasePayload, presentation?.text);
  if (eventType === "evaluation.completed") {
    article._agentWork.evaluation = payload;
    renderEvaluationReport(payload);
  }
  if (["plan.created", "plan.updated"].includes(eventType)) {
    const revision = article._agentWork?.planRevision || 1;
    const explanation = String(payload.explanation || "").trim();
    addAgentWorkActivity(
      article,
      explanation || `${revision === 1 ? "已建立" : "已更新"} ${
        Array.isArray(payload.steps) ? payload.steps.length : 0
      } 步任务计划`,
      "done",
      "plan.lifecycle",
    );
    return;
  }
  if (presentation) {
    if (eventType === "tool.called" && !article._agentWork?.planAware) {
      activateProgressStep(article._agentWork, "agent_loop");
    }
    const kind = {
      "tool-running": "running",
      "stage-running": "running",
      done: "done",
      failed: "failed",
      warning: "warning",
    }[presentation.kind] || "progress";
    const stepId = eventType.startsWith("step.") ? String(payload.step_id || "") : "";
    const key = runtimeActivityIdentity(eventType, payload);
    addAgentWorkActivity(
      article,
      presentation.text,
      kind,
      key,
      eventType === "tool.called",
      stepId,
    );
    return;
  }
  if (eventType === "memory.resolved") {
    activateProgressStep(article._agentWork, "resolve_memory");
    const selected = Number(payload.selected_count || 0);
    addAgentWorkActivity(
      article,
      selected ? `已采用 ${selected} 条相关历史上下文` : "已完成上下文筛选",
      "done",
      eventType,
    );
  } else if (eventType === "tools.routed") {
    activateProgressStep(article._agentWork, "route");
    const count = Array.isArray(payload.offered) ? payload.offered.length : 0;
    addAgentWorkActivity(article, `已匹配 ${count} 项可用能力`, "done", eventType);
  } else if (eventType === "evidence.required") {
    activateProgressStep(article._agentWork, "gather_evidence");
    addAgentWorkActivity(article, "已确定任务所需的外部证据", "done", eventType);
  } else if (eventType === "verification.failed") {
    activateProgressStep(article._agentWork, "revise");
    addAgentWorkActivity(article, "发现结果缺口，正在定向修正", "running", eventType);
  } else if (eventType === "verification.completed") {
    const provisional = payload.provisional === true;
    addAgentWorkActivity(
      article,
      provisional
        ? "基础检查通过，等待产物生成与逐页校验"
        : payload.passed === false
          ? "完成校验并记录剩余限制"
          : "结果已通过完成条件校验",
      provisional || payload.passed === false ? "progress" : "done",
      "verification",
    );
  } else if (eventType === "context.compacted") {
    addAgentWorkActivity(article, "已整理本轮对话上下文", "done", eventType);
  }
}

function markAnswerStreaming(article) {
  const work = article?._agentWork;
  if (!work) return;
  work.answerStreaming = true;
  if (!["completed_with_issues", "completed", "failed", "cancelled"].includes(work.taskStatus)) {
    work.taskStatus = "finalizing";
  }
  work.statusText = "正在生成结果（尚未最终确认）";
  setAnswerDeliveryState(article, "streaming");
  renderAgentWork(article);
}

async function streamRun(jobId, article) {
  const content = article.querySelector(".message-content");
  let streamed = "";
  try {
    const afterRevision = article._agentWork?.lastEventRevision || 0;
    const response = await fetch(
      `/api/v1/chat/turns/${jobId}/stream?after_revision=${afterRevision}`,
      {
      credentials: "same-origin",
      headers: { Accept: "application/x-ndjson" },
      },
    );
    if (!response.ok || !response.body) throw new Error("流式连接失败");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        // Approval controls are restored independently of audit-event deduplication.
        if (event.type === "approval") showRunApproval(jobId, article, event);
        if (
          ["progress", "runtime", "approval"].includes(event.type)
          && !rememberAgentWorkEvent(article._agentWork, event)
        ) continue;
        if (event.type === "progress") {
          setRunStatus(event.text, "running");
          if (!/^调用(?:内置)?工具[：:]/.test(event.text || "")) {
            addTurnEvent(event.text, "progress", "", "activity", event.timestamp);
            updateAgentWork(article, event.text);
            addAgentWorkActivity(
              article, event.text, "progress", `progress:${event.text}`
            );
          }
        } else if (event.type === "runtime") {
          const eventType = event.event_type || "";
          const payload = event.payload || {};
          if (eventType === "approval.granted" && payload.execution_scope !== "inline_subagent") {
            settleRunApproval(article);
          }
          // claimed 只表示 Worker 已租领；真正注入当前模型上下文后才从可编辑区移除。
          if (eventType === "guidance.applied" && payload.guidance_id) {
            const active = state.activeJobs.find(job => job.job_id === jobId);
            if (active) {
              active.guidance = (active.guidance || []).filter(
                item => item.id !== payload.guidance_id
              );
            }
            renderStagedMessages();
          }
          if (payload.execution_scope !== "inline_subagent") {
            applyPlanRuntimeEvent(article, eventType, payload);
            applyTaskRuntimeEvent(article, eventType, payload);
            applyLoopRuntimeEvent(article, eventType, payload);
          }
          const presentation = runtimeEventPresentation(eventType, payload, article._agentWork);
          recordRuntimeActivity(article, eventType, payload, presentation);
          recordWorkspaceDrawerEvent(eventType, payload, presentation, event.timestamp);
        } else if (event.type === "approval") {
          recordWorkspaceDrawerEvent("approval.requested", event, null, event.timestamp);
        } else if (event.type === "delta") {
          if (!article._answerStarted) {
            article._answerStarted = true;
            markAnswerStreaming(article);
            addAgentWorkActivity(
              article, "正在生成待核验结果", "running", "terminal-outcome"
            );
          }
          streamed += event.text || "";
          article._messageText = streamed;
          content.innerHTML = renderMarkdown(streamed);
          scrollToLatest();
        } else if (event.type === "end") {
          settleRunApproval(article);
          if (event.status === "done") {
            if (event.task_status) {
              applyTaskRuntimeEvent(article, "task.status", {status: event.task_status});
            }
            article._messageText = event.answer || streamed;
            if (!article._messageText.trim()) {
              article._messageText = "任务完成但未返回可展示结果，请重新生成。";
              content.textContent = article._messageText;
              setAnswerDeliveryState(article, "failed");
              setRunStatus("结果为空", "failed");
              addTurnEvent(article._messageText, "failed");
              finishAgentWork(article, "failed", article._messageText);
              return "failed";
            }
            content.innerHTML = renderMarkdown(article._messageText);
            const files = event.export_files || [];
            article.querySelector(".message-meta").innerHTML = exportLinks(files);
            state.sessionId = event.session_id || state.sessionId;
            const limited = agentWorkCompletionState(article._agentWork) === "completed_with_issues";
            updateRunPhase(article, "task.status", {status: limited ? "completed_with_issues" : "completed"});
            setAnswerDeliveryState(article, "");
            setRunStatus(limited ? "受限完成" : "已完成", limited ? "warning" : "done");
            addTurnEvent(
              limited ? "受限结果已验证并保存，部分计划步骤未完成" : "结果已验证并保存",
              limited ? "warning" : "done",
              "terminal-outcome",
            );
            addAgentWorkActivity(
              article,
              limited ? "受限结果已完成并保存" : "最终结果已完成并保存",
              limited ? "warning" : "done",
              "terminal-outcome",
            );
            finishAgentWork(
              article,
              "done",
              limited ? "受限结果已验证并保存" : "结果已验证并保存",
            );
          } else if (event.status === "cancelled") {
            updateRunPhase(article, "task.cancelled");
            content.textContent = "已停止生成。";
            setAnswerDeliveryState(article, "cancelled");
            setRunStatus("已取消", "cancelled");
            finishAgentWork(article, "cancelled", "生成已停止");
          } else {
            throw new Error(event.error || "执行失败");
          }
          return event.status;
        }
      }
      if (done) break;
    }
    throw new Error("执行连接已中断，正在确认任务状态");
  } catch (error) {
    if (state.pageLeaving) return "detached";
    try {
      const snapshot = await api(`/api/v1/chat/turns/${jobId}`);
      if (["pending", "running", "awaiting_approval"].includes(snapshot.status)) {
        replayAgentWork(article, snapshot.process);
        updateAgentWork(article, snapshot.progress || "页面连接已中断，任务仍在后台运行");
        setRunStatus("正在重新连接", "running");
        restoreRunApproval(jobId, article, snapshot);
        return "reconnect";
      }
      if (snapshot.status === "done") {
        settleRunApproval(article);
        replayAgentWork(article, snapshot.process);
        if (snapshot.task_status) {
          applyTaskRuntimeEvent(article, "task.status", {status: snapshot.task_status});
        }
        article._messageText = snapshot.answer || streamed;
        if (!article._messageText.trim()) {
          article._messageText = "任务完成但未返回可展示结果，请重新生成。";
          content.textContent = article._messageText;
          setAnswerDeliveryState(article, "failed");
          setRunStatus("结果为空", "failed");
          finishAgentWork(article, "failed", article._messageText);
          return "failed";
        }
        content.innerHTML = renderMarkdown(article._messageText);
        article.querySelector(".message-meta").innerHTML = exportLinks(snapshot.export_files);
        state.sessionId = snapshot.session_id || state.sessionId;
        const limited = agentWorkCompletionState(article._agentWork) === "completed_with_issues";
        updateRunPhase(article, "task.status", {status: limited ? "completed_with_issues" : "completed"});
        setAnswerDeliveryState(article, "");
        setRunStatus(limited ? "受限完成" : "已完成", limited ? "warning" : "done");
        addTurnEvent(
          limited ? "受限结果已验证并保存，部分计划步骤未完成" : "结果已验证并保存",
          limited ? "warning" : "done",
          "terminal-outcome",
        );
        addAgentWorkActivity(
          article,
          limited ? "受限结果已完成并保存" : "最终结果已完成并保存",
          limited ? "warning" : "done",
          "terminal-outcome",
        );
        finishAgentWork(
          article,
          "done",
          limited ? "受限结果已验证并保存" : "结果已验证并保存",
        );
        return "done";
      }
      if (snapshot.status === "cancelled") {
        settleRunApproval(article);
        updateRunPhase(article, "task.cancelled");
        content.textContent = "已停止生成。";
        setAnswerDeliveryState(article, "cancelled");
        finishAgentWork(article, "cancelled", "生成已停止");
        return "cancelled";
      }
      if (snapshot.status === "failed") error = new Error(snapshot.error || error.message);
    } catch (_) {
      updateAgentWork(article, "连接暂时中断，任务状态将在返回页面后恢复");
      setRunStatus("等待重新连接", "running");
      return "reconnect";
    }
    article._messageText = `执行失败：${error.message}`;
    settleRunApproval(article);
    updateRunPhase(article, "task.failed", {}, error.message);
    content.textContent = article._messageText;
    setAnswerDeliveryState(article, "failed");
    setRunStatus("执行失败", "failed");
    addTurnEvent(error.message, "failed");
    finishAgentWork(article, "failed", error.message);
    return "failed";
  }
}

async function connectRun(jobId, article) {
  let outcome = "reconnect";
  while (!state.pageLeaving && outcome === "reconnect") {
    outcome = await streamRun(jobId, article);
    if (outcome === "reconnect") {
      article._agentWork.reconnectAttempts += 1;
      const delay = Math.min(5000, 600 * article._agentWork.reconnectAttempts);
      await new Promise(resolve => setTimeout(resolve, delay));
    } else article._agentWork.reconnectAttempts = 0;
  }
  return state.pageLeaving ? "detached" : outcome;
}

async function resumeActiveJob(jobId) {
  if (state.submitting) return;
  const job = state.activeJobs.find(item => item.job_id === jobId);
  if (!job) return;
  if (state.runningJob && state.runningJob !== jobId) {
    showToast("当前页面已连接另一个运行中的任务");
    return;
  }
  const existing = log.querySelector(`[data-running-job="${jobId}"]`);
  if (existing) {
    existing.scrollIntoView({block: "center", behavior: "smooth"});
    return;
  }

  state.runningJob = jobId;
  state.sessionId = job.session_id || state.sessionId;
  state.activeProjectId = job.project_id || null;
  rememberChatView();
  if (state.activeProjectId) setProjectCollapsed(state.activeProjectId, false);
  if (job.agent_id && state.agents.some(agent => agent.id === Number(job.agent_id))) {
    const changedAgent = state.agentId !== Number(job.agent_id);
    state.agentId = Number(job.agent_id);
    $("agent-name").textContent = activeAgent()?.name || "智能体";

    renderAgentMenu();
    await loadModels({reset: changedAgent});
    await loadCatalog();
    renderResources();
  }

  log.innerHTML = "";
  state.followLatest = true;
  const activeTurnIds = new Set(state.activeJobs.map(item => String(item.job_id)));
  const historyRows = state.conversations
    .filter(row => row.session_id === state.sessionId
      && !activeTurnIds.has(String(row.turn_id || row.id)))
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
  for (const row of historyRows) {
    addHistoryTurn(row);
  }
  addMessage("user", job.query || "正在进行的任务", {
    meta: scheduledMessageMeta(job),
    context: {attachments: job.attachments || []},
  });
  const article = addMessage("assistant", "");
  article.dataset.runningJob = jobId;
  startAgentWork(article, job.query || "正在进行的任务", job.progress || "正在恢复任务连接…");
  resetRunDetails();
  replayAgentWork(article, job.process);
  setRunStatus(job.progress || "后台运行中", "running");
  restoreRunApproval(jobId, article, job);
  addTurnEvent("已恢复运行中的任务");
  setSendState();
  renderStagedMessages();
  renderProjects();
  renderHistory();
  scrollToLatest(true);

  const outcome = await connectRun(jobId, article);
  if (!state.pageLeaving && outcome !== "detached") {
    if (["done", "failed"].includes(outcome)) markAgentStatusSeen(job.agent_id, jobId);
    forgetRunningJob(jobId);
    state.activeJobs = state.activeJobs.filter(item => item.job_id !== jobId);
    if (state.runningJob === jobId) state.runningJob = null;
    setSendState();
    await Promise.all([loadHistory(), loadActiveJobs(), loadSchedules().catch(() => []), loadAgentStatuses()]);
    await resumeNextConversationJob(job.session_id);
  }
}

async function restoreActiveJob() {
  const remembered = rememberedRunningJob();
  const rememberedTarget = state.activeJobs.find(job =>
    job.source !== "cron" && job.job_id === remembered?.job_id
  );
  const target = (
    rememberedTarget && ["running", "awaiting_approval"].includes(rememberedTarget.status)
      ? rememberedTarget : null
  ) || state.activeJobs.find(job =>
    job.source !== "cron" && ["running", "awaiting_approval"].includes(job.status)
  ) || state.activeJobs
    .filter(job => job.source !== "cron" && job.status === "pending")
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at))[0];
  if (target) {
    rememberRunningJob(target);
    await resumeActiveJob(target.job_id);
  } else if (remembered) {
    forgetRunningJob(remembered.job_id);
    if (remembered.session_id && state.conversations.some(
      row => row.session_id === remembered.session_id
    )) {
      openThread(remembered.session_id);
    }
  }
}

function beginComposerSubmission() {
  const token = Symbol("composer-submission");
  state.submissionToken = token;
  state.submitting = true;
  return token;
}

function finishComposerSubmission(token) {
  if (state.submissionToken !== token) return;
  state.submissionToken = null;
  state.submitting = false;
}

async function resumeNextConversationJob(sessionId = state.sessionId) {
  if (state.submitting || state.runningJob || state.sessionId !== sessionId || state.pageLeaving) return;
  const next = state.activeJobs
    .filter(item => item.source !== "cron" && item.session_id === sessionId)
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at))[0];
  if (next) await resumeActiveJob(next.job_id);
}

async function enqueueMessage(
  text,
  context,
  sessionId = state.sessionId,
  projectId = state.activeProjectId,
) {
  const options = await prepareComposerSubmission(context);
  const form = new FormData();
  form.append("agent_id", String(options.agent_id));
  form.append("query", text);
  if (sessionId) form.append("session_id", sessionId);
  if (projectId) form.append("project_id", String(projectId));
  form.append("dataset_ids", JSON.stringify(context.datasets));
  form.append("template_ids", JSON.stringify(context.templates));
  form.append("skill_ids", JSON.stringify(context.skills));
  form.append("mcp_ids", JSON.stringify(context.mcpServers));
  form.append("invoked_agent_ids", JSON.stringify(context.agentCalls));
  form.append("approval_policy", options.approval_policy);
  form.append("provider_id", options.provider_id == null ? "" : String(options.provider_id));
  if (options.reasoning_effort) form.append("reasoning_effort", options.reasoning_effort);
  context.attachments.forEach(file => form.append("attachments", file));
  return api("/api/v1/chat", {method: "POST", body: form});
}

async function enqueueQueuedMessage(
  text, context, sessionId = state.sessionId, projectId = state.activeProjectId,
) {
  const result = await enqueueMessage(
    text,
    context,
    sessionId,
    projectId,
  );
  const activeJob = {
    ...context.runtimeOptions,
    job_id: result.turn_id, session_id: result.session_id,
    project_id: projectId, query: text || "请分析附件", source: "web",
    status: result.status || "pending", guidance: [], progress: "已排队",
    approval_policy: result.approval_policy || context.runtimeOptions.approval_policy,
    created_at: new Date().toISOString(),
  };
  state.activeJobs = [activeJob, ...state.activeJobs.filter(job => job.job_id !== result.turn_id)];
  renderStagedMessages();
  renderProjects();
  renderHistory();
  renderAgentMenu();
  showToast("消息已加入队列，将在当前任务后执行");
  return result;
}

async function submitDuringRun() {
  if (state.submitting) return;
  const text = query.value.trim();
  const hasContent = Boolean(text || state.attachments.length);
  if (!hasContent) {
    await api(`/api/v1/chat/turns/${state.runningJob}/cancel`, {method: "POST"});
    return;
  }
  const context = captureComposerContext();
  const target = Object.freeze({
    jobId: state.runningJob, sessionId: state.sessionId, projectId: state.activeProjectId,
  });
  const activeTurn = {...state.activeJobs.find(job => job.job_id === target.jobId)};
  const submissionToken = beginComposerSubmission();
  closeComposerMenus();
  setSendState();
  try {
    const options = await prepareComposerSubmission(context);
    const sameOptions = ChatWorkspace.sameTurnOptions(activeTurn, options);
    const targetStillRunning = state.runningJob === target.jobId && state.activeJobs.some(
      job => job.job_id === target.jobId && ["pending", "running", "awaiting_approval"].includes(job.status)
    );
    if (text && !hasStructuredComposerContext(context) && sameOptions && targetStillRunning) {
      const guidance = await api(`/api/v1/chat/turns/${target.jobId}/guidance`, {
        method: "POST",
        json: {content: text},
      });
      const active = state.activeJobs.find(job => job.job_id === target.jobId);
      if (active) active.guidance = [...(active.guidance || []), guidance];
      showToast("已加入对话引导；可在上方切换队列或设为当前目标");
    } else {
      await enqueueQueuedMessage(text, context, target.sessionId, target.projectId);
      if (!sameOptions) showToast("设置已用于下一轮任务");
    }
    query.value = "";
    clearComposerContext();
    resizeComposer();
    renderStagedMessages();
    scrollToLatest(true);
  } catch (error) {
    showToast(`消息未发送：${error.message}`);
  } finally {
    finishComposerSubmission(submissionToken);
    setSendState();
    resumeNextConversationJob(target.sessionId).catch(error => showToast(`恢复任务失败：${error.message}`));
  }
}

async function submit() {
  if (state.submitting) return;
  if (state.runningJob) {
    return submitDuringRun();
  }
  if (state.voiceListening) state.voiceRecognition?.stop();
  const text = query.value.trim();
  if ((!text && !state.attachments.length) || !state.agentId) return;
  const submissionToken = beginComposerSubmission();
  closeComposerMenus();
  const submittedContext = captureComposerContext();
  const submittedAgentId = state.agentId;
  state.followLatest = true;
  resetRunDetails();
  const userArticle = addMessage("user", text || "请分析附件", {context: submittedContext});
  query.value = "";
  resizeComposer();
  const responseArticle = addMessage("assistant", "");
  startAgentWork(responseArticle, text || "请分析附件");
  clearComposerContext();
  setSendState();
  let runOutcome = "failed";
  let jobCreated = false;
  let submittedJobId = null;
  try {
    const result = await enqueueMessage(
      text,
      submittedContext,
      state.sessionId,
      state.activeProjectId,
    );
    if (!submittedContext.attachments.length) {
      setMessageAttachmentContext(userArticle, result.attachments || []);
    }
    jobCreated = true;
    submittedJobId = result.turn_id;
    finishComposerSubmission(submissionToken);
    state.runningJob = result.turn_id;
    responseArticle.dataset.runningJob = result.turn_id;
    state.sessionId = result.session_id;
    rememberChatView();
    const activeJob = {
      ...submittedContext.runtimeOptions,
      job_id: result.turn_id, session_id: result.session_id,
      project_id: state.activeProjectId,
      query: text || "请分析附件", status: result.status || "pending",
      source: "web", guidance: [], progress: "Turn 已创建",
      approval_policy: result.approval_policy || submittedContext.runtimeOptions.approval_policy,
      created_at: new Date().toISOString(),
    };
    rememberRunningJob(activeJob);
    state.activeJobs = [activeJob, ...state.activeJobs.filter(job => job.job_id !== result.turn_id)];
    renderStagedMessages();
    renderProjects();
    renderHistory();
    renderAgentMenu();
    setSendState();
    setRunStatus("已排队", "running");
    updateRunPhase(responseArticle, "task.queued", {}, "任务已提交，等待执行");
    addTurnEvent("Turn 已创建");
    runOutcome = await connectRun(result.turn_id, responseArticle);
  } catch (error) {
    const networkFailure = error instanceof TypeError
      || /failed to fetch|networkerror|network request failed/i.test(error.message || "");
    const message = networkFailure
      ? "无法连接服务，任务尚未确认创建。请检查网络后重试。"
      : `提交失败：${error.message}`;
    responseArticle.querySelector(".message-content").textContent = message;
    setRunStatus(jobCreated ? "执行失败" : "提交失败", "failed");
    updateRunPhase(responseArticle, "task.failed", {}, message);
    addAgentWorkActivity(
      responseArticle,
      jobCreated ? message : "任务未成功提交",
      "failed",
      "submit-failed",
    );
    responseArticle._agentWork.statusText = jobCreated ? "执行失败" : "提交失败";
    finishAgentWork(responseArticle, "failed");
    if (!jobCreated) {
      query.value = text;
      resizeComposer();
      restoreComposerContext(submittedContext);
      userArticle.querySelector(".message-meta").textContent = "未确认提交";
    }
  } finally {
    finishComposerSubmission(submissionToken);
    if (!state.pageLeaving && runOutcome !== "detached") {
      if (submittedJobId && ["done", "failed"].includes(runOutcome)) {
        markAgentStatusSeen(submittedAgentId, submittedJobId);
      }
      if (submittedJobId) forgetRunningJob(submittedJobId);
      state.activeJobs = state.activeJobs.filter(job => job.job_id !== submittedJobId);
      if (state.runningJob === submittedJobId) state.runningJob = null;
      setSendState();
      await Promise.all([loadHistory(), loadActiveJobs(), loadSchedules().catch(() => []), loadAgentStatuses()]);
      await resumeNextConversationJob();
    } else if (!state.pageLeaving) {
      await Promise.all([loadActiveJobs(), loadSchedules().catch(() => [])]);
      setSendState();
    }
  }
}

function setSidebarCollapsed(collapsed, persist = true) {
  const sidebar = $("sidebar");
  const shell = document.querySelector(".chat-shell");
  sidebar.classList.toggle("collapsed", collapsed);
  shell.classList.toggle("sidebar-collapsed", collapsed);
  $("sidebar-expand").hidden = !collapsed;
  $("sidebar-toggle").setAttribute("aria-expanded", String(!collapsed));
  $("sidebar-toggle").title = collapsed ? "展开侧栏" : "收起侧栏";
  $("sidebar-toggle").setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
  setAccountMenu(false);
  if (persist) localStorage.setItem("chat_sidebar_collapsed", collapsed ? "1" : "0");
}

function accountMenuItems() {
  return [...$("account-menu").querySelectorAll('[role="menuitem"], [role="menuitemradio"]')]
    .filter(item => !item.hidden && !item.disabled);
}

function setAccountMenu(open, {focus = false, restoreFocus = false} = {}) {
  const menu = $("account-menu");
  const trigger = $("account-menu-btn");
  const wasOpen = !menu.hidden;
  menu.hidden = !open;
  menu.classList.toggle("open", open);
  trigger.setAttribute("aria-expanded", String(open));
  if (open) renderAccountTheme();
  if (open && focus) accountMenuItems()[0]?.focus();
  if (!open && wasOpen && restoreFocus) trigger.focus();
}

$("agent-selector").onclick = () => {
  const open = $("agent-menu").classList.toggle("open");
  $("agent-selector").setAttribute("aria-expanded", String(open));
};
$("new-chat-btn").onclick = () => newChat({projectId: null});
$("new-project-btn").onclick = () => { location.href = "/admin#projects"; };
$("recent-new-chat-btn").onclick = () => {

  newChat({projectId: null});
};
$("project-section-toggle").onclick = () => {

  toggleSection("project");
};
$("pinned-section-toggle").onclick = () => {

  toggleSection("pinned");
};
$("recent-section-toggle").onclick = () => {

  toggleSection("recent");
};
$("context-pin-btn").onclick = async () => {
  const sessionId = state.contextSessionId;
  const pinned = state.contextThreadPinned;
  closeThreadContextMenu();
  await setThreadState(sessionId, {pinned: !pinned});
};
$("context-archive-btn").onclick = () => {
  const sessionId = state.contextSessionId;
  closeThreadContextMenu();
  archiveThread(sessionId);
};
$("context-memory-btn").onclick = () => {
  location.href = `/admin#conversation-memory/${encodeURIComponent(state.contextSessionId)}`;
};
$("project-context-pin-btn").onclick = async () => {
  const projectId = state.contextProjectId;
  const pinned = state.contextProjectPinned;
  closeProjectContextMenu();
  await setProjectState(projectId, {pinned: !pinned});
};
$("project-context-edit-btn").onclick = () => {
  const projectId = state.contextProjectId;
  closeProjectContextMenu();
  location.href = `/admin#projects/${encodeURIComponent(projectId)}`;
};
$("project-context-archive-btn").onclick = () => {
  const projectId = state.contextProjectId;
  closeProjectContextMenu();
  archiveProject(projectId);
};
$("context-rename-btn").onclick = openRenameThreadDialog;
$("rename-thread-cancel").onclick = () => $("rename-thread-dialog").close();
$("rename-thread-save").onclick = renameThread;
$("rename-thread-name").onkeydown = event => {
  if (event.key === "Enter") renameThread();
};
$("history-search").oninput = () => {
  renderProjects();
  renderHistory();
};
$("attach-input").onchange = event => {
  addComposerAttachments(event.target.files);
  event.target.value = "";
  closeAddMenu();
};
$("mention-menu-btn").onclick = () => {
  closeAddMenu();
  insertTrigger("@");
};
$("command-btn").onclick = () => {
  closeAddMenu();
  insertTrigger("/");
};
$("add-menu-btn").onclick = toggleAddMenu;
$("mic-btn").onclick = () => { closeAddMenu(); toggleVoiceInput(); };
for (const kind of ["approval-policy", "model"]) {
  const trigger = $(`${kind}-btn`);
  const menu = $(`${kind}-menu`);
  if (kind === "approval-policy") {
    trigger.onclick = () => toggleComposerMenu(kind);
    trigger.onkeydown = event => {
      if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
      event.preventDefault();
      if (menu.hidden) toggleComposerMenu(kind, true);
      const buttons = [...menu.querySelectorAll('button:not([disabled]), a[href]')];
      (event.key === "ArrowUp" ? buttons.at(-1) : buttons[0])?.focus();
    };
  }
  menu.onkeydown = event => {
    const buttons = [...menu.querySelectorAll('button:not([disabled]), a[href]')];
    const current = buttons.indexOf(document.activeElement);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) && buttons.length) {
      event.preventDefault();
      const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[index]?.focus();
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeComposerMenus(true);
    }
  };
  menu.addEventListener("focusout", event => closeComposerMenuOnFocusOut(event, kind === "model" ? $("reasoning-menu") : menu, trigger));
}
$("model-btn").onclick = () => toggleComposerMenu("reasoning", true);
$("model-btn").onkeydown = event => {
  if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
  event.preventDefault();
  if ($("reasoning-menu").hidden) toggleComposerMenu("reasoning", true);
};
$("reasoning-slider").oninput = event => selectReasoningStep(Number(event.target.value));
$("reasoning-slider").onclick = event => selectReasoningStep(Number(event.currentTarget.value));
$("reasoning-slider").onpointerup = event => {
  if (event.button === 0) selectReasoningStep(Number(event.currentTarget.value));
};
$("reasoning-slider").onkeyup = event => {
  // An inherited default can occupy the first stop without being an override.
  // Commit an explicit range operation even when its native value stays equal.
  if (["Home", "End", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "PageUp", "PageDown"].includes(event.key)) {
    selectReasoningStep(Number(event.currentTarget.value));
  }
};
$("reasoning-reset").onclick = () => {
  if (state.submitting) return;
  state.composerSelection.reasoning_effort = "";
  renderReasoningControl();
  setSendState();
  if (!$("reasoning-slider").disabled) $("reasoning-slider").focus({preventScroll: true});
};
$("reasoning-model-btn").onclick = () => setComposerModelMode($("model-menu").hidden, true);
$("reasoning-menu").onkeydown = event => {
  if (event.key !== "Escape") return;
  event.preventDefault();
  event.stopPropagation();
  closeComposerMenus(true);
};
$("reasoning-menu").addEventListener("focusout", event =>
  closeComposerMenuOnFocusOut(event, $("reasoning-menu"), $("model-btn")));
function setRunDrawer(open) {
  const drawer = $("run-drawer");
  const wasOpen = drawer.classList.contains("open");
  drawer.inert = !open;
  drawer.classList.toggle("open", open);
  document.querySelector(".chat-shell").classList.toggle("run-drawer-open", open);
  drawer.setAttribute("aria-hidden", String(!open));
  $("details-toggle").setAttribute("aria-expanded", String(open));
  if (open) $("drawer-close").focus({preventScroll: true});
  else if (wasOpen) $("details-toggle").focus({preventScroll: true});
}
$("details-toggle").onclick = () => setRunDrawer(!$("run-drawer").classList.contains("open"));
$("drawer-close").onclick = () => setRunDrawer(false);
$("run-event-filters").querySelectorAll("[data-event-filter]").forEach(button => {
  button.onclick = () => {
    state.runEventFilter = button.dataset.eventFilter;
    filterRunEvents();
  };
});
function activeTurnProgressArticle() {
  const owner = $("turn-progress-dock").dataset.owner;
  return [...document.querySelectorAll(".message.assistant")]
    .find(item => item._agentWork?.dockId === owner) || null;
}

function setTurnProgressDockExpanded(expanded) {
  const article = activeTurnProgressArticle();
  if (!article?._agentWork) return;
  article._agentWork.dockExpanded = Boolean(expanded);
  syncTurnProgressDock(article);
}

const turnProgressDock = $("turn-progress-dock");
turnProgressDock.addEventListener("mouseenter", () => setTurnProgressDockExpanded(true));
turnProgressDock.addEventListener("mouseleave", () => setTurnProgressDockExpanded(false));
turnProgressDock.addEventListener("focusin", () => {
  if (!window.matchMedia("(hover: none)").matches) setTurnProgressDockExpanded(true);
});
turnProgressDock.addEventListener("focusout", event => {
  if (!turnProgressDock.contains(event.relatedTarget)) setTurnProgressDockExpanded(false);
});
$("turn-progress-toggle").onclick = () => {
  const article = activeTurnProgressArticle();
  if (!article?._agentWork) return;
  const touchOnly = window.matchMedia("(hover: none)").matches;
  // 桌面端只采用悬停展示，避免点击后计划卡片粘住；无悬停能力的
  // 触屏设备才用点击切换，保证同一功能仍然可达。
  if (touchOnly) setTurnProgressDockExpanded(!article._agentWork.dockExpanded);
};
document.addEventListener("pointerdown", event => {
  if (!window.matchMedia("(hover: none)").matches) return;
  const article = activeTurnProgressArticle();
  if (
    article?._agentWork?.dockExpanded
    && !turnProgressDock.contains(event.target)
  ) setTurnProgressDockExpanded(false);
});
$("sidebar-toggle").onclick = () => {
  if (window.matchMedia("(max-width: 760px)").matches) {
    $("sidebar").classList.remove("mobile-open");
    $("mobile-scrim").classList.remove("open");
    return;
  }
  setSidebarCollapsed(true);
};
$("sidebar-expand").onclick = () => setSidebarCollapsed(false);
$("rail-search").onclick = () => {
  setSidebarCollapsed(false);
  $("history-search").focus();
};
$("rail-projects").onclick = () => {
  setSidebarCollapsed(false);
  setSectionCollapsed("project", false);
  $("project-section-toggle").focus();
};
$("mobile-sidebar").onclick = () => {
  $("sidebar").classList.add("mobile-open");
  $("mobile-scrim").classList.add("open");
};
$("mobile-scrim").onclick = () => {
  $("sidebar").classList.remove("mobile-open");
  $("mobile-scrim").classList.remove("open");
};
$("account-menu-btn").onclick = () => setAccountMenu($("account-menu").hidden);
$("account-menu-btn").onkeydown = event => {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  event.preventDefault();
  setAccountMenu(true, {focus: true});
  if (event.key === "ArrowUp") accountMenuItems().at(-1)?.focus();
};
$("account-menu").onkeydown = event => {
  const items = accountMenuItems();
  const index = items.indexOf(document.activeElement);
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
      : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  }
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    setAccountMenu(false, {restoreFocus: true});
  }
};
$("account-theme-options").querySelectorAll("[data-account-theme]").forEach(button => {
  button.onclick = () => setAccountTheme(button.dataset.accountTheme);
});
$("account-theme-options").onkeydown = event => {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const buttons = [...$("account-theme-options").querySelectorAll("[data-account-theme]")];
  const index = buttons.indexOf(document.activeElement);
  if (index < 0) return;
  event.preventDefault();
  event.stopPropagation();
  buttons[(index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length]?.focus();
};
$("account-menu").addEventListener("focusout", () => {
  queueMicrotask(() => {
    if (!$("account-menu").contains(document.activeElement)
        && document.activeElement !== $("account-menu-btn")) setAccountMenu(false);
  });
});
$("logout-btn").onclick = logout;
send.onclick = () => {
  const hasContent = Boolean(query.value.trim() || state.attachments.length);
  if (!state.runningJob && !hasContent) toggleVoiceInput();
  else submit();
};
query.oninput = () => {
  resizeComposer();
  const trigger = findComposerTrigger();
  if (trigger) openPalette(trigger.mode, trigger);
  else closePalette();
};
query.addEventListener("paste", handleComposerPaste);
installAttachmentDropTarget();
query.onkeydown = event => {
  if (event.isComposing || event.keyCode === 229) return;
  if (
    event.key === "Backspace"
    && !event.ctrlKey && !event.metaKey && !event.altKey
    && query.selectionStart === 0 && query.selectionEnd === 0
    && removeLastResourceToken()
  ) {
    event.preventDefault();
    return;
  }
  if (state.paletteMode) {
    const items = paletteItems();
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      state.paletteIndex = (state.paletteIndex + direction + Math.max(items.length, 1))
        % Math.max(items.length, 1);
      renderPalette();
      return;
    }
    if (event.key === "Enter" && items.length) {
      event.preventDefault();
      selectPaletteItem(items[state.paletteIndex]);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closePalette();
      return;
    }
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
};
document.onclick = event => {
  const exportLink = event.target.closest?.("[data-export-download]");
  if (exportLink) {
    event.preventDefault();
    handleExportDownload(exportLink);
    return;
  }
  if (!$("agent-selector").contains(event.target) && !$("agent-menu").contains(event.target)) {
    $("agent-menu").classList.remove("open");
    $("agent-selector").setAttribute("aria-expanded", "false");
  }
  if (!$("account-menu-btn").contains(event.target) && !$("account-menu").contains(event.target)) setAccountMenu(false);
  if (!["model-btn", "reasoning-menu", "approval-policy-btn", "approval-policy-menu"].some(id => $(id).contains(event.target))) closeComposerMenus();
  if (!$("thread-context-menu").contains(event.target)) closeThreadContextMenu();
  if (!$("project-context-menu").contains(event.target)) closeProjectContextMenu();
  if (!$("resource-picker").contains(event.target)
      && !$("command-btn").contains(event.target)
      && !$("mention-menu-btn").contains(event.target)
      && event.target !== query) closePalette();
  if (!$("composer-add-menu").contains(event.target)
      && !$("add-menu-btn").contains(event.target)) closeAddMenu();
  if (!event.target.closest?.(".staged-message")) {
    document.querySelectorAll(".staged-message-menu").forEach(menu => { menu.hidden = true; });
  }
};
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    newChat({projectId: null});
  }
  if (event.key === "Escape") {
    $("agent-menu").classList.remove("open");
    setAccountMenu(false, {restoreFocus: true});
    setRunDrawer(false);
    $("sidebar").classList.remove("mobile-open");
    $("mobile-scrim").classList.remove("open");
    closePalette();
    closeAddMenu();
    closeComposerMenus(true);
    closeThreadContextMenu();
    closeProjectContextMenu();

  }
});

function renderAccount() {
  const guest = !Auth.user || Auth.isGuest();
  $("account-name").textContent = guest ? "未登录" : Auth.username() || "";
  $("account-role").textContent = guest ? "登录后使用问答" : Auth.role() || "";
  $("user-avatar").textContent = guest ? "登" : initials(Auth.username());
  $("admin-link").hidden = !Auth.canAccessSettings();
  $("login-link").hidden = !guest;
  $("logout-btn").hidden = guest;
  $("new-project-btn").hidden = guest;
  $("context-memory-btn").hidden = guest;
  $("project-context-edit-btn").hidden = guest;
}

window.addEventListener("beforeunload", () => {
  state.pageLeaving = true;
  stopSchedulePolling();
  stopAgentStatusPolling();
});
window.addEventListener("pagehide", () => {
  state.pageLeaving = true;
  stopSchedulePolling();
  stopAgentStatusPolling();
});
window.addEventListener("pageshow", event => {
  state.pageLeaving = false;
  if (event.persisted) refreshRestoredPage();
});
window.addEventListener("resize", syncResourceTokenLayout);
window.addEventListener("resize", () => setAccountMenu(false));
window.addEventListener("resize", scheduleComposerPopoverPosition);
document.addEventListener("scroll", scheduleComposerPopoverPosition, true);
window.visualViewport?.addEventListener("resize", scheduleComposerPopoverPosition);
window.visualViewport?.addEventListener("scroll", scheduleComposerPopoverPosition);
if (typeof ResizeObserver !== "undefined") {
  const composerPopoverObserver = new ResizeObserver(scheduleComposerPopoverPosition);
  for (const element of [$("reasoning-menu"), $("model-btn"), document.querySelector(".composer-wrap"), document.querySelector(".conversation-pane")]) {
    if (element) composerPopoverObserver.observe(element);
  }
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopSchedulePolling();
    stopAgentStatusPolling();
    return;
  }
  if (!state.sessionReady) return;
  Auth.verifyCurrentSession().then(async unchanged => {
    if (!unchanged) return;
    renderAccount();
    await loadPreferences();
    await loadModels();
    renderHistory();
    startSchedulePolling();
    startAgentStatusPolling();
    pollSchedules();
    pollAgentStatuses();
  }).catch(error => showToast(`设置同步失败：${error.message}`));
});
setSidebarCollapsed(localStorage.getItem("chat_sidebar_collapsed") === "1", false);
setSectionCollapsed("pinned", localStorage.getItem("chat_pinned_collapsed") === "1", false);
setSectionCollapsed("project", localStorage.getItem("chat_project_collapsed") === "1", false);
setSectionCollapsed("recent", localStorage.getItem("chat_recent_collapsed") === "1", false);
hydrateIcons();
setSendState();
Auth.ensureSession()
  .then(() => {
    state.sessionReady = true;
    renderAccount();
    return loadPreferences();
  })
  .then(() => loadAgents())
  .then(() => Promise.all([loadHistory(), loadProjects(), loadCatalog(), loadActiveJobs()]))
  .then(() => loadAgentStatuses({bootstrap: true}))
  .then(() => startAgentStatusPolling())
  .then(() => restoreActiveJob())
  .then(() => {
    if (!state.sessionId) restoreRememberedView();
  })
  .then(() => loadSchedules({refreshThread: false}))
  .then(() => startSchedulePolling())
  .catch(error => {
    log.innerHTML = `<div class="fatal-state">加载失败：${escapeHtml(error.message)}</div>`;
  })
  .finally(() => {
    document.body.classList.remove("is-booting");
    document.body.removeAttribute("aria-busy");
  });
