"""现代对话 UI 的结构回归测试。"""
import re
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendUiTests(unittest.TestCase):
    def test_chat_shell_has_accessible_modern_interactions(self):
        html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        script = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")

        for marker in (
            'id="mobile-scrim"',
            'id="sidebar-expand"',
            'id="rail-search"',
            'id="rail-projects"',
            'id="account-menu-btn"',
            'aria-controls="account-menu"',
            'id="run-drawer"',
            'id="schedule-reminders"',
            'id="schedule-reminder-list"',
            'id="add-menu-btn"',
            'id="command-btn"',
            'id="mic-btn"',
            'id="composer-project-context"',
            'id="composer-project-name"',
            'id="message-staging"',
            'id="evidence-center"',
            'id="turn-progress-dock"',
            'id="turn-progress-card"',
            'id="turn-progress-steps"',
            'id="turn-progress-toggle"',
            'id="turn-progress-count"',
            'id="new-project-btn"',
            'id="pinned-section"',
            'id="pinned-section-toggle"',
            'id="pinned-project-list"',
            'id="project-section-toggle"',
            'id="recent-section-toggle"',
            'id="recent-new-chat-btn"',
            'id="thread-context-menu"',
            'id="project-context-menu"',
            'id="rename-thread-dialog"',
            'aria-live="polite"',
            'data-icon="waveform"',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('id="duplex-mode-btn"', html)
        self.assertNotIn('id="duplex-mode-label"', html)
        self.assertIn("starter-grid", script)
        self.assertIn("data-copy-message", script)
        self.assertIn("response.body.getReader()", script)
        self.assertIn("/api/v1/chat/catalog", script)
        self.assertIn('form.append("skill_ids"', script)
        self.assertIn('form.append("mcp_ids"', script)
        self.assertIn('form.append("invoked_agent_ids"', script)
        self.assertIn('form.append("provider_id", options.provider_id == null ? "" : String(options.provider_id))', script)
        self.assertIn('form.append("approval_policy", options.approval_policy)', script)
        self.assertIn('form.append("reasoning_effort", options.reasoning_effort)', script)
        self.assertIn('full_access: {', script)
        self.assertIn("ChatWorkspace.normalizePreferences", script)
        self.assertIn('/api/v1/users/me/preferences', script)
        self.assertIn('await loadPreferences();', script)
        self.assertIn('eventType === "approval.auto_approved"', script)
        self.assertIn("/api/v1/chat/models?agent_id=", script)
        self.assertNotIn("chat_model_provider:", script)
        self.assertNotIn("chat_approval_policy:", script)
        for control in ("model-btn", "approval-policy-btn", "model-menu", "approval-policy-menu",
                        "reasoning-effort-label", "reasoning-menu", "reasoning-slider", "reasoning-reset"):
            self.assertIn(f'id="{control}"', html)
        self.assertNotIn('id="reasoning-btn"', html)
        self.assertIn('aria-controls="reasoning-menu" aria-haspopup="dialog"', html)
        self.assertNotIn('class="model-menu-icon"', script)
        for removed in ("theme-toggle", "change-password-btn", "project-dialog", "memory-dialog",
                        "recent-section-menu"):
            self.assertNotIn(f'id="{removed}"', html)
        self.assertIn('/admin#projects', script)
        self.assertIn('/admin#conversation-memory/', script)
        self.assertIn('href="/admin#preferences"', html)
        self.assertIn('setAccountMenu(false, {restoreFocus: true})', script)
        chat_css = (ROOT / "frontend/static/chat-workspace.css").read_text(encoding="utf-8")
        self.assertIn('grid-template-columns: 64px minmax(0, 1fr)', chat_css)
        self.assertIn('.chat-page .chat-sidebar { padding: 14px 12px 12px; overflow: visible; }', chat_css)
        literal_ids = set(re.findall(r'id="([^"]+)"', html))
        script_ids = set(re.findall(r'\$\("([^"$]+)"\)', script))
        self.assertFalse(script_ids - literal_ids, f"Dangling UI references: {script_ids - literal_ids}")
        self.assertIn("startAgentWork", script)
        self.assertIn("updateAgentWork", script)
        self.assertIn("summarizeTaskGoal", script)
        self.assertIn("syncTurnProgressDock", script)
        self.assertIn('work.status !== "running" || !work.planAware', script)
        self.assertIn("applyLoopRuntimeEvent", script)
        self.assertIn("applyPlanRuntimeEvent", script)
        self.assertIn('"plan.updated"', script)
        self.assertIn('"plan.created"', script)
        self.assertIn("buildTaskProgressSteps", script)
        self.assertIn('title: summarizePlanStep(objective, "完成当前任务")', script)
        self.assertIn("addAgentWorkActivity", script)
        self.assertIn("recordRuntimeActivity", script)
        self.assertIn("settleAgentWorkActivities", script)
        self.assertIn("failAgentWorkActivities", script)
        self.assertIn(".flat()", script)
        self.assertIn(".findLast(", script)
        self.assertIn("展开或收起处理过程", script)
        self.assertIn("计划状态会随执行进展同步", script)
        self.assertNotIn("正在根据最新观察决定下一步；复杂任务会先建立任务计划。", script)
        self.assertNotIn('确认“${goal}”', script)
        self.assertIn('eventType === "loop.iteration.started"', script)
        self.assertNotIn('eventType.startsWith("graph.")', script)
        self.assertIn("agent-work-loop-note", script)
        self.assertNotIn("工作目标", script)
        self.assertNotIn("agent-work-focus", script)
        self.assertIn("agentWorkActivitiesMarkup(work)", script)
        self.assertIn("replayAgentWork", script)
        self.assertIn('process.events || []', script)
        self.assertIn("执行当前行动", script)
        self.assertNotIn("规划、调用工具并生成回答", script)
        self.assertIn('class="agent-work-disclosure"', script)
        self.assertIn("dockExpanded", script)
        self.assertIn("dockExpanded: false", script)
        self.assertIn('turnProgressDock.addEventListener("mouseenter"', script)
        self.assertIn('turnProgressDock.addEventListener("mouseleave"', script)
        self.assertIn("悬停查看任务进度", script)
        self.assertIn("if (touchOnly) setTurnProgressDockExpanded", script)
        self.assertIn("previousActivities = new Map", script)
        self.assertIn("planRevision", script)
        self.assertIn("item.id || sameTitle?.node", script)
        self.assertIn("existingByStepId = new Map", script)
        self.assertIn("updateAgentWorkStepElement", script)
        self.assertIn('failed: "failed"', script)
        self.assertIn('blocked: "blocked"', script)
        self.assertIn('skipped: "skipped"', script)
        self.assertIn("rememberAgentWorkEvent", script)
        self.assertIn("applyTaskRuntimeEvent", script)
        finish_block = script[
            script.index("function finishAgentWork"):
            script.index("function activeAgent")
        ]
        self.assertIn("const completionState = agentWorkCompletionState(work)", finish_block)
        self.assertIn("work.taskStatus = completionState", finish_block)
        self.assertIn('completionState === "completed_with_issues"', finish_block)
        self.assertIn('work.taskStatus = "failed"', finish_block)
        self.assertIn('work.statusText = "执行失败"', finish_block)
        self.assertIn('work.taskStatus = "cancelled"', finish_block)
        self.assertIn('work.statusText = "已停止"', finish_block)
        self.assertIn("after_revision=${afterRevision}", script)
        self.assertIn('return "reconnect"', script)
        self.assertIn("reconnectAttempts", script)
        self.assertIn('document.addEventListener("pointerdown"', script)
        self.assertNotIn("work.steps.push({node", script)
        self.assertIn("items.length > 24", script)
        self.assertIn("计划已收尾：${parts.join", script)
        self.assertIn("recordAgentWorkActivityState", script)
        self.assertIn("runtimeActivityIdentity", script)
        self.assertIn("正在生成，当前内容尚未最终确认", script)
        self.assertIn('work.expanded = false', script)
        self.assertIn("setSidebarCollapsed", script)
        self.assertIn('localStorage.getItem("chat_sidebar_collapsed")', script)
        self.assertIn('api("/api/v1/chat/turns/active")', script)
        self.assertIn('api("/api/v1/chat/agents/status")', script)
        self.assertIn("function agentLight(agentId)", script)
        self.assertIn("function conversationExecutionStatus(sessionId)", script)
        self.assertIn("function executionLight(status)", script)
        self.assertIn("function activeConversationLight()", script)
        self.assertIn("const light = activeConversationLight()", script)
        self.assertIn('if (!state.sessionId) return {key: "idle", ...AGENT_LIGHTS.idle}', script)
        self.assertIn("function markAgentStatusSeen", script)
        self.assertIn('if (submittedJobId && ["done", "failed"].includes(runOutcome))', script)
        self.assertIn('error: {label: "执行错误", color: "#ff7373"}', script)
        self.assertNotIn('completed: {label: "执行完成", color: "#9bf396"}', script)
        self.assertIn('terminalUnread && row.terminal_status === "failed"', script)
        self.assertIn('["completed", "completed_with_issues"].includes(row.terminal_status)', script)
        self.assertIn('attention: {label: "需要用户批准或答复", color: "#ffd0b8"}', script)
        self.assertIn('thinking: {label: "思考或运行中", color: "#9cd5fe"}', script)
        self.assertIn('unread: {label: "未读聊天", color: "#9bf396"}', script)
        self.assertIn('idle: {label: "空闲", color: "#e0e0e0"}', script)
        execution_status_block = script[
            script.index("function conversationExecutionStatus"):
            script.index("function agentLight")
        ]
        self.assertNotIn("state.conversations", execution_status_block)
        self.assertIn('if (active) return active.status || "pending"', execution_status_block)
        self.assertIn('return ""', execution_status_block)
        self.assertIn("rememberRunningJob", script)
        self.assertIn("restoreActiveJob", script)
        self.assertIn("function historyMessageText(row)", script)
        self.assertIn("function addHistoryTurn(row)", script)
        self.assertIn("const activeTurnIds = new Set", script)
        self.assertIn("!activeTurnIds.has(String(row.turn_id || row.id))", script)
        self.assertIn('["queued", "pending", "running", "awaiting_approval"]', script)
        self.assertIn('status === "cancelled"', script)
        self.assertIn("thread-running-indicator", script)
        self.assertNotIn("thread-state-indicator", script)
        self.assertNotIn("function threadStatusMarkup", script)
        self.assertNotIn("最后一次执行：", script)
        self.assertIn("runtimeEventPresentation", script)
        self.assertIn('event.type === "runtime"', script)
        self.assertIn('edit: "编辑文件"', script)
        self.assertIn("const threadRows = new Map()", script)
        self.assertIn('data-pin="', script)
        self.assertIn('data-archive="', script)
        self.assertIn("openThreadContextMenu", script)
        self.assertIn("renameThread", script)
        self.assertIn("setSectionCollapsed", script)
        self.assertIn('function collectThreads(projectId, sortMode = "priority")', script)
        self.assertIn("belongsToProject(row.project_id, projectId)", script)
        self.assertIn('newChat({projectId: null})', script)
        self.assertIn('class="project-thread-list"', script)
        self.assertIn("function collectPinnedThreads()", script)
        self.assertIn("if (!row.session_id || !row.pinned) continue", script)
        self.assertIn("threadCollectionHtml(pinnedThreads)", script)
        self.assertIn("const pinnedProjects = projects.filter(project => project.pinned)", script)
        self.assertIn("projects.map(projectGroupHtml)", script)
        self.assertNotIn("const regularProjects = projects.filter(project => !project.pinned)", script)
        self.assertIn('localStorage.getItem("chat_pinned_collapsed")', script)
        self.assertIn('data-project-start="${project.id}"', script)
        self.assertIn('data-project-menu="${project.id}"', script)
        self.assertIn('data-project-toggle="${project.id}"', script)
        self.assertIn("button.ondblclick = event =>", script)
        self.assertIn("openProject(Number(button.dataset.projectToggle))", script)
        self.assertIn('localStorage.getItem("chat_collapsed_projects")', script)
        self.assertNotIn('id="history-count"', html)
        self.assertIn("openProjectContextMenu", script)
        self.assertIn("function renderComposerProjectContext()", script)
        self.assertIn("const showProjectSelector = Boolean(project && !state.sessionId)", script)
        self.assertIn('context.closest(".composer").classList.toggle("has-project-context"', script)
        self.assertIn('localStorage.getItem("chat_project_collapsed")', script)
        self.assertIn('localStorage.getItem("chat_recent_collapsed")', script)
        self.assertNotIn('localStorage.getItem("chat_recent_sort")', script)
        self.assertIn("state.recentSortMode = preferences.recent_sort", script)
        self.assertIn('newChat({projectId: null})', script)
        self.assertIn('form.append("project_id"', script)
        self.assertIn("else if (!current.title && row.title)", script)
        self.assertIn("/api/v1/schedules?session_id=", script)
        self.assertIn("结果已追加到当前对话", script)
        self.assertIn("scheduledMessageMeta", script)
        self.assertIn("data-export-download", script)
        self.assertIn("downloadAuthenticated", script)
        self.assertIn("exportLinks(snapshot.export_files)", script)
        self.assertIn("任务完成但未返回可展示结果，请重新生成", script)
        self.assertIn("无法连接服务，任务尚未确认创建", script)
        self.assertIn("未确认提交", script)
        self.assertIn("captureComposerContext", script)
        self.assertIn("clearComposerContext", script)
        self.assertIn("restoreComposerContext", script)
        self.assertIn("{context: submittedContext}", script)
        self.assertIn("renderSubmittedReferences", script)
        self.assertIn("renderUserMessageContent", script)
        self.assertIn("isDisplayableAnswer", script)
        self.assertIn("submitDuringRun", script)
        self.assertIn("enqueueQueuedMessage", script)
        self.assertIn("renderStagedMessages", script)
        self.assertIn('data-stage-change="', script)
        self.assertNotIn("data-stage-toggle", script)
        self.assertIn('class="staged-mode" data-stage-change="', script)
        self.assertIn('data-stage-redirect="', script)
        self.assertIn('class="danger" data-stage-delete', script)
        self.assertIn('handleStagedActionFailure', script)
        self.assertIn('"引导已送达当前任务"', script)
        self.assertIn('eventType === "guidance.applied"', script)
        self.assertNotIn('["guidance.claimed", "guidance.applied"].includes', script)
        self.assertIn('"已加入对话引导；可在上方切换队列或设为当前目标"', script)
        self.assertIn('/guidance`', script)
        self.assertIn('["new", "redirect"].includes(target)', script)
        self.assertIn('"已设为当前目标，正在停止旧目标"', script)
        self.assertIn('hasStructuredContext || hasComposerOverrides() ? "加入对话队列" : "添加到对话引导"', script)
        self.assertNotIn("duplexMode", script)
        self.assertIn("renderEvaluationReport", script)
        self.assertIn('eventType === "evaluation.completed"', script)
        self.assertIn("Evidence Tree", script)
        self.assertIn('/chat/staged/${row.kind}/${row.id}', script)
        self.assertIn('["running", "awaiting_approval"].includes(job.status)', script)
        edit_block = script[script.index("async function editStagedMessage"):
                            script.index("async function moveStagedMessage")]
        self.assertNotIn("discardStagedMessage", edit_block)
        self.assertIn("工具已返回结果，但模型未能生成最终答复：", script)
        self.assertNotIn('icon("history", 15)', script)

    def test_plan_state_helpers_execute_mixed_terminal_and_step_scoping(self):
        script = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")
        helper_source = script.split("/* AGENT_WORK_STATE_HELPERS_START */", 1)[1]
        helper_source = helper_source.split("/* AGENT_WORK_STATE_HELPERS_END */", 1)[0]
        task_event_source = script[
            script.index("function applyTaskRuntimeEvent"):
            script.index("function replayAgentWork")
        ]
        presentation_source = script[
            script.index("function runtimeEventPresentation"):
            script.index("function recordRuntimeActivity")
        ]
        scenario = r"""
const equal = (actual, expected, label) => {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${label}: ${JSON.stringify(actual)} !== ${JSON.stringify(expected)}`);
  }
};
const work = {
  taskStatus: "completed",
  steps: [
    {node: "step_1", status: "done"},
    {node: "step_2", status: "blocked"},
    {node: "step_3", status: "blocked"},
    {node: "step_4", status: "blocked"},
  ],
  activities: [[], [], [], []],
};
const snapshot = agentWorkPlanSnapshot(work.steps);
equal(snapshot.counts, {
  total: 4, done: 1, current: 0, pending: 0,
  failed: 0, blocked: 3, skipped: 0,
}, "mixed terminal counts");
equal(snapshot.label, "计划已收尾：1 完成、3 阻塞", "mixed terminal label");
equal(snapshot.terminalized, true, "mixed plan terminalized");
equal(snapshot.hasIssues, true, "mixed plan issues");
if (snapshot.label.includes("第 1 / 4 步")) throw new Error("mixed plan regressed to step 1/4");
equal(agentWorkCompletionState(work), "completed_with_issues", "limited completion");
equal(isTerminalConversationStatus("completed_with_issues"), true,
  "limited completion clears unread when opened");
equal(isTerminalConversationStatus("running"), false,
  "active task does not clear unread");

for (const [stepId, text] of [
  ["step_2", "阻塞：拆解主要资金来源"],
  ["step_3", "阻塞：判定大额流出属性"],
  ["step_4", "阻塞：形成复盘与风险提示"],
]) {
  const payload = {step_id: stepId};
  recordAgentWorkActivityState(work, {
    text,
    kind: "failed",
    key: runtimeActivityIdentity("step.blocked", payload),
    stepId,
  });
}
equal(work.activities.map(items => items.length), [0, 1, 1, 1], "step buckets");
equal(work.activities.flat().map(item => item.key), [
  "step:step_2:step.blocked",
  "step:step_3:step.blocked",
  "step:step_4:step.blocked",
], "stable scoped activity keys");
equal(runtimeStopReasonText("successful_tool_budget"),
  "已达到本轮业务工具调用预算，转入结果整理", "budget explanation");
equal(runtimeActivityIdentity("task.status", {status: "completed_with_issues"}),
  "terminal-outcome", "limited task status shares terminal identity");
equal(runtimeActivityIdentity("loop.completed", {completion_status: "completed_with_issues"}),
  "terminal-outcome", "loop completion shares terminal identity");
equal(runtimeActivityIdentity("task.completed_with_issues", {}),
  "terminal-outcome", "persisted task completion shares terminal identity");
equal(runtimeActivityIdentity("loop.stopped", {reason: "successful_tool_budget"}),
  "loop.stopped", "budget cause remains a separate audit activity");
equal(runtimeEventPresentation("step.blocked", {
  step_id: "step_2", step: "拆解主要资金来源",
}), {
  text: "阻塞：拆解主要资金来源", kind: "warning",
}, "blocked is visually distinct from failure");

const replayed = {
  taskStatus: "executing",
  completionStatus: "",
  answerStreaming: false,
  loopVersion: "2.0",
  steps: work.steps.map(step => ({...step})),
};
applyTaskRuntimeEvent({_agentWork: replayed}, "task.completed", {status: "completed"});
equal(replayed.taskStatus, "completed_with_issues",
  "old completed event preserves inferred limited state");
equal(runtimeEventPresentation("task.completed", {status: "completed"}, replayed), {
  text: "任务已受限完成，部分计划步骤未完成", kind: "warning",
}, "old task completion presentation");
equal(runtimeEventPresentation("loop.completed", {}, replayed), {
  text: "Agent Loop v2.0 已结束：部分计划步骤未完成", kind: "warning",
}, "old loop completion presentation");
equal(runtimeEventPresentation("turn.completed", {}, replayed), {
  text: "Turn 已结束：部分计划步骤未完成", kind: "warning",
}, "old turn completion presentation");
equal(runtimeEventPresentation("loop.completed", {
  checkpoint: {status: "completed_with_issues"},
}, {
  taskStatus: "completed", loopVersion: "2.0",
  steps: [{node: "step_1", status: "done"}],
}), {
  text: "Agent Loop v2.0 已结束：部分计划步骤未完成", kind: "warning",
}, "loop checkpoint limited presentation");

const successful = {
  taskStatus: "completed", loopVersion: "2.0",
  steps: [{node: "step_1", status: "done"}],
};
equal(runtimeEventPresentation("task.completed", {status: "completed"}, successful), {
  text: "任务执行完成", kind: "done",
}, "successful task presentation remains done");
"""
        result = subprocess.run(
            ["node", "-"],
            input=(
                'const RunWorkspace = require("./frontend/static/run-workspace.js");\n'
                + helper_source
                + "\nfunction renderAgentWork() {}\nfunction setAnswerDeliveryState() {}\n"
                + task_event_source
                + "\nconst APPROVAL_POLICIES = {}; const TOOL_ACTIONS = {};\n"
                + presentation_source
                + scenario
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_limited_completion_seen_and_reconnect_paths_are_wired(self):
        script = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")
        open_thread = script[
            script.index("function openThread("):
            script.index("function isDisplayableAnswer")
        ]
        self.assertIn("isTerminalConversationStatus(latest.status)", open_thread)
        self.assertNotIn('["completed", "failed"].includes(latest.status)', open_thread)

        reconnect = script[
            script.index('if (snapshot.status === "done")'):
            script.index('if (snapshot.status === "cancelled")')
        ]
        self.assertIn("const limited = agentWorkCompletionState", reconnect)
        self.assertIn('limited ? "受限完成" : "已完成"', reconnect)
        self.assertIn("受限结果已验证并保存，部分计划步骤未完成", reconnect)
        self.assertIn('limited ? "warning" : "done"', reconnect)

        self.assertIn('function addTurnEvent(text, kind = "progress", key = "", category = "activity", timestamp = "")', script)
        self.assertIn('item.dataset.eventKey === cleanKey', script)
        self.assertIn(
            'runtimeActivityIdentity(eventType, payload) === "terminal-outcome"',
            script,
        )
        self.assertNotIn('"final-output"', script)
        self.assertGreaterEqual(script.count('"terminal-outcome"'), 8)

    def test_markdown_supports_fenced_code_and_copy(self):
        script = (ROOT / "frontend/static/common.js").read_text(encoding="utf-8")

        self.assertIn('class="code-block"', script)
        self.assertIn("data-copy-code", script)
        self.assertIn("navigator.clipboard", script)
        self.assertIn('localStorage.removeItem("gca_token")', script)
        self.assertNotRegex(script, r'(?:localStorage|sessionStorage)\.(?:getItem|setItem)\(\s*[\"\']gca_token[\"\']')
        self.assertNotIn('headers["Authorization"]', script)
        self.assertIn("error.status = resp.status", script)
        self.assertIn('fetch("/api/v1/auth/me", {credentials: "same-origin"})', script)
        self.assertIn("URL.createObjectURL(blob)", script)
        auth_source = script[:script.index("async function api")]
        verification = r'''
const assert = require("node:assert/strict");
const removed = [];
global.localStorage = {
  getItem(){throw new Error("Browser storage must not supply identity or tokens");},
  setItem(){throw new Error("Browser storage must not receive identity or tokens");},
  removeItem(key){removed.push(key);},
};
'''
        verification += auth_source + r'''
Auth.save({id:1,username:"member",role:"user",modules:[]});
assert.equal(Auth.username(),"member");
assert.equal(Auth.role(),"user");
assert.equal(Auth.canModule("providers"),false);
assert.ok(removed.includes("gca_token"));
Auth.clear();
assert.equal(Auth.username(),null);
'''
        result = subprocess.run(["node", "-"], input=verification, text=True, encoding="utf-8", capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_composer_accepts_drag_drop_clipboard_files_and_long_pasted_text(self):
        html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        script = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")
        css = (ROOT / "frontend/static/style.css").read_text(encoding="utf-8")

        self.assertIn('id="attachment-drop-overlay"', html)
        self.assertIn("也可拖放文件或从剪贴板粘贴", html)
        self.assertIn("function addComposerAttachments", script)
        self.assertIn("function handleComposerPaste", script)
        self.assertIn("clipboard.files", script)
        self.assertIn("PASTED_TEXT_ATTACHMENT_THRESHOLD", script)
        self.assertIn('new File([text], pastedTextFilename()', script)
        self.assertIn('query.addEventListener("paste", handleComposerPaste)', script)
        self.assertIn('conversationPane.addEventListener("drop"', script)
        self.assertIn("event.dataTransfer?.files", script)
        self.assertIn(".attachment-drop-overlay.active", css)
        self.assertIn('class="attachment-pills" id="attach-chips"', html)
        self.assertIn('class="composer-toolbar"', html)
        self.assertIn('class="composer-editor" id="composer-editor"', html)
        self.assertIn('placeholder="随心输入"', html)
        self.assertIn("function attachmentPreviewUrl", script)
        self.assertIn("URL.createObjectURL(file)", script)
        self.assertIn('class="attachment-card attachment-image"', script)
        self.assertIn('class="attachment-card attachment-file"', script)
        self.assertIn(".attachment-image img", css)
        self.assertIn(".attachment-file-copy", css)
        self.assertIn("function resourceTokenHtml", script)
        self.assertIn("function removeLastResourceToken", script)
        self.assertIn('event.key === "Backspace"', script)
        self.assertIn("query.selectionStart === 0 && query.selectionEnd === 0", script)
        self.assertNotIn('data-remove-kind="${kind}"', script)
        self.assertIn("function syncResourceTokenLayout", script)
        self.assertIn("function updateComposerPlaceholder", script)
        self.assertIn('query.placeholder = ""', script)
        self.assertIn('`在“${project.name}”中开始对话…`', script)
        self.assertIn('state.sessionId ? "继续提问或补充要求…" : "随心输入"', script)
        self.assertIn('window.addEventListener("resize", syncResourceTokenLayout)', script)
        self.assertIn("--composer-token-indent", css)
        self.assertIn(".composer-editor.tokens-stacked", css)

    def test_design_tokens_include_dark_and_reduced_motion(self):
        css = (ROOT / "frontend/static/style.css").read_text(encoding="utf-8")
        script = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")

        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("--sidebar-width", css)
        self.assertIn("--primary:", css)
        self.assertIn(".command-palette", css)
        self.assertIn(".chat-shell.sidebar-collapsed", css)
        self.assertIn(".agent-work-disclosure", css)
        self.assertIn(".agent-work-head::after", css)
        self.assertIn(".agent-work-step.current", css)
        self.assertIn(".agent-work-step.skipped", css)
        self.assertIn(".agent-work-step.blocked", css)
        self.assertIn(".agent-work-activity.running", css)
        self.assertIn(".agent-work-activity.warning", css)
        self.assertIn(".agent-work-activity-group", css)
        self.assertIn(".agent-work.done .agent-work-head", css)
        self.assertIn('[data-agent-state="thinking"]', css)
        self.assertIn("@keyframes agent-light-pulse", css)
        self.assertIn(".turn-progress-dock", css)
        self.assertIn(".turn-progress-card", css)
        self.assertIn(".turn-progress-toggle", css)
        self.assertIn(".turn-progress-dock.collapsed", css)
        self.assertIn(".turn-progress-dock.terminalized.has-issues", css)
        self.assertIn(".turn-progress-card .agent-work-step.blocked", css)
        self.assertIn(".turn-progress-dock:not(.collapsed)::after", css)
        self.assertIn(".message-answer-state", css)
        self.assertNotIn(".thread-state-indicator", css)
        self.assertIn(".submitted-context", css)
        self.assertIn(".submitted-reference", css)
        self.assertIn(".submitted-reference.inherited", css)
        self.assertIn("setMessageAttachmentContext", script)
        self.assertIn('eventType === "attachments.resolved"', script)
        self.assertIn(".submitted-invocation", css)
        self.assertIn(".run-event.tool-running", css)
        self.assertIn(".model-menu", css)
        self.assertIn(".approval-policy-menu", css)
        self.assertIn(".approval-policy-button", css)
        self.assertIn(".composer-project-context", css)
        self.assertIn(".composer.has-project-context", css)
        self.assertIn(".message-staging.active", css)
        self.assertIn(".staged-message-menu", css)
        self.assertIn(".schedule-reminders", css)
        self.assertIn(".schedule-reminder-status", css)
        self.assertIn(".thread-context-menu", css)
        self.assertIn(".thread-item:hover .thread-actions", css)
        self.assertIn(".sidebar-section.collapsed", css)
        self.assertIn(".pinned-project-list", css)
        self.assertIn(".project-group.collapsed .project-thread-list", css)
        self.assertIn(".section-more-button", css)
        self.assertIn(".sidebar-action-button", css)
        self.assertIn(".section-head-actions", css)
        self.assertIn("visibility: hidden", css)
        self.assertIn(".sidebar-section-head:hover .section-head-actions", css)
        self.assertIn("margin-top: auto", css)
        self.assertIn(".project-item:hover .project-item-actions", css)

    def test_refresh_restores_chat_context_without_background_polling(self):
        html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        script = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")
        css = (ROOT / "frontend/static/style.css").read_text(encoding="utf-8")

        self.assertIn('class="chat-page is-booting"', html)
        self.assertIn('class="sidebar-loading"', html)
        self.assertIn("rememberChatView", script)
        self.assertIn("restoreRememberedView", script)
        self.assertIn("saveComposerDraft", script)
        self.assertIn("restoreComposerDraft", script)
        self.assertIn('sessionStorage.setItem(chatViewStorageKey()', script)
        self.assertIn('document.addEventListener("visibilitychange"', script)
        self.assertIn("state.schedulePollInFlight", script)
        self.assertIn("refreshRestoredPage", script)
        self.assertNotIn("if (event.persisted) location.reload()", script)
        self.assertIn(".sidebar-loading", css)
        self.assertIn("@keyframes loading-sheen", css)
        self.assertIn(".conversation-pane { height: 100dvh; }", css)

    def test_admin_uses_domain_forms_instead_of_json_editor(self):
        html = (ROOT / "frontend/admin.html").read_text(encoding="utf-8")
        script = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")

        self.assertNotIn('id="json-editor"', html)
        self.assertNotIn("编辑 JSON", script)
        for marker in (
            'id="resource-fields"',
            'id="resource-import"',
            'id="resource-export"',
            'id="agent-import"',
            'id="agent-export"',
        ):
            self.assertIn(marker, html)
        for marker in (
            'type: "keyvalue"',
            'type: "resources"',
            'type: "module-choices"',
            'importType: "users-csv"',
            'exportType: "audit-csv"',
        ):
            self.assertIn(marker, script)

    def test_provider_form_supports_generic_protocols_and_model_discovery(self):
        script = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")

        for marker in (
            '["openai", "OpenAI 兼容"]',
            '["anthropic", "Anthropic 兼容"]',
            '["chatgpt", "ChatGPT 订阅（Codex 登录）"]',
            '["responses", "Responses API"]',
            '["chat_completions", "Chat Completions"]',
            '["messages", "Anthropic Messages"]',
            'detectModels: true',
            '"/api/v1/providers/discover-models"',
            'type: "json-keyvalue"',
            'name: "reasoning_config"',
            'ProviderReasoningConfig.mount',
            'Object.assign(payload, state.providerReasoningEditor.read())',
            'name: "stream_idle_timeout_ms"',
            'name: "model_id"',
            'name: "model_input"',
            'name: "model_reasoning"',
            'name: "context_window"',
            'name: "max_tokens"',
            'provider-use-model',
            'collectResourcePayload({allowIncomplete: true})',
            '开放给对话用户选择',
            'providerType.value === "chatgpt" ? ["responses"]',
        ):
            self.assertIn(marker, script)
        for legacy_marker in (
            'name: "text_model"',
            'name: "vision_model"',
            'name: "image_generation_mode"',
            'name: "image_model"',
            'name: "supports_image_edit"',
        ):
            self.assertNotIn(legacy_marker, script)

    def test_settings_are_inline_and_audit_logs_use_list_table(self):
        script = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")
        css = (ROOT / "frontend/static/style.css").read_text(encoding="utf-8")

        self.assertIn("directSettings: true", script)
        self.assertIn('id="settings-inline-save"', script)
        self.assertIn("renderSystemSettings", script)
        self.assertNotIn('importType: "settings-json"', script)
        self.assertNotIn('exportType: "settings-json"', script)
        self.assertIn('class="audit-table"', script)
        self.assertIn("renderAuditList", script)
        self.assertIn("auditPageSize: 10", script)
        self.assertIn("[10, 20, 50, 100]", script)
        self.assertIn('id="audit-prev"', script)
        self.assertIn('id="audit-next"', script)
        self.assertIn("offset=${state.auditPage * state.auditPageSize}", script)
        self.assertIn('id="resource-clear"', (ROOT / "frontend/admin.html").read_text(encoding="utf-8"))
        self.assertIn('clearLabel: "清空日志"', script)
        self.assertIn('await api(config.endpoint, {method: "DELETE"})', script)
        self.assertIn("state.auditPage = 0", script)
        self.assertIn(".settings-panel", css)
        self.assertIn(".audit-table", css)
        self.assertIn(".audit-pagination", css)

    def test_archives_are_managed_from_admin_console(self):
        chat_html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        admin_html = (ROOT / "frontend/admin.html").read_text(encoding="utf-8")
        chat_script = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")
        admin_script = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")

        self.assertNotIn('id="personal-settings-btn"', chat_html)
        self.assertNotIn('id="personal-settings-dialog"', chat_html)
        self.assertNotIn("openPersonalSettings", chat_script)
        self.assertIn('data-tab="archive"', admin_html)
        self.assertIn("archiveManagement: true", admin_script)
        self.assertIn('/api/v1/chat/turns?archived=true', admin_script)
        self.assertIn('/api/v1/projects?archived=true', admin_script)

    def test_admin_console_is_presented_as_settings(self):
        chat_html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        admin_html = (ROOT / "frontend/admin.html").read_text(encoding="utf-8")
        common_script = (ROOT / "frontend/static/common.js").read_text(encoding="utf-8")
        chat_script = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")

        self.assertIn('<span data-icon="settings"></span>后台管理</a>', chat_html)
        self.assertIn('href="/admin#preferences" id="admin-link"', chat_html)
        menu = chat_html.split('id="account-menu"', 1)[1].split("</div>", 1)[0]
        menu_elements = re.findall(r'<(?:a|button)\b[^>]*role="menuitem"[^>]*>', menu)
        menu_items = {
            re.search(r'id="([^"]+)"', tag).group(1): {"hidden": bool(re.search(r'\bhidden(?:\s|>)', tag))}
            for tag in menu_elements
        }
        self.assertEqual(set(menu_items), {"admin-link", "login-link", "logout-btn"})
        self.assertTrue(all(menu_items[key]["hidden"] for key in ("admin-link", "login-link", "logout-btn")))
        render_account = chat_script[chat_script.index("function renderAccount()"):chat_script.index('window.addEventListener("beforeunload"')]
        verification = 'const assert = require("node:assert/strict");\n'
        verification += 'const elements = ' + json.dumps(menu_items) + ';\n'
        verification += r'''
global.$ = id => elements[id] ||= {};
let guest = false;
global.Auth = {user:{id:1},isGuest:()=>guest,username:()=>guest?"visitor_fixture":"member",role:()=>guest?"guest":"user",canAccessSettings:()=>!guest};
global.initials = value => value[0].toUpperCase();
const menuIds = Object.keys(elements);
const visible = () => menuIds.filter(id=>!elements[id].hidden).sort();
'''
        verification += render_account + r'''
renderAccount();
assert.deepEqual(visible(),["admin-link","logout-btn"]);
assert.equal(elements["account-name"].textContent,"member");
guest = true;
renderAccount();
assert.deepEqual(visible(),["login-link"]);
assert.equal(elements["account-name"].textContent,"未登录");
assert.equal(elements["account-role"].textContent,"登录后使用问答");
assert.equal(elements["context-memory-btn"].hidden,true);
assert.equal(elements["new-project-btn"].hidden,true);
'''
        result = subprocess.run(["node", "-"], input=verification, text=True, encoding="utf-8", capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('querySelectorAll(\'[role="menuitem"], [role="menuitemradio"]\')', chat_script)
        self.assertIn('.filter(item => !item.hidden && !item.disabled)', chat_script)
        self.assertIn('<title>__BRAND_DOCUMENT_TITLE__</title>', admin_html)
        self.assertIn('<strong>设置</strong>', admin_html)
        self.assertNotIn("管理控制台", chat_html + admin_html + common_script)
        self.assertNotIn("管理后台", chat_html + admin_html + common_script)

    def test_personal_weixin_channel_uses_qr_binding_ui(self):
        html = (ROOT / "frontend/admin.html").read_text(encoding="utf-8")
        script = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")
        self.assertIn("<span>消息渠道</span>", html)
        self.assertIn('id="weixin-login-dialog"', html)
        self.assertIn('["openclaw_weixin", "微信（扫码连接）"]', script)
        self.assertIn("openWeixinLogin", script)
        self.assertIn("/pair-code", script)
        self.assertIn("独立空间", script)
        self.assertIn('image.removeAttribute("src")', script)
        self.assertIn('Auth.canModule("agents")', script)
        self.assertIn('api("/api/v1/agents/enabled")', script)
        self.assertIn("await ensureResourceDependencies(key)", script)
        self.assertIn("if (state.currentTab === key) openResourceEditor", script)
        self.assertIn("resourceUsesAgentSelect", script)

    def test_admin_navigation_has_distinct_icons_and_task_order(self):
        html = (ROOT / "frontend/admin.html").read_text(encoding="utf-8")
        script = (ROOT / "frontend/static/common.js").read_text(encoding="utf-8")
        admin_script = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")
        css = (ROOT / "frontend/static/style.css").read_text(encoding="utf-8")

        tabs = re.findall(
            r'<button[^>]+class="tab[^"]*"[^>]+data-tab="([^"]+)"[^>]*>'
            r'<span class="ti" data-icon="([^"]+)"',
            html,
        )
        self.assertEqual(len(tabs), 21)
        workspace_tabs = tabs[3:]
        self.assertEqual(len({icon_name for _, icon_name in workspace_tabs}), len(workspace_tabs))
        for _, icon_name in tabs[:3]:
            self.assertIn(f"  {icon_name}:", script)
        self.assertEqual(
            [tab_name for tab_name, _ in tabs],
            [
                "preferences", "projects", "conversation-memory",
                "agents", "providers", "services", "tools", "knowledge",
                "memory", "guardrails", "templates", "improvement", "operations",
                "token-usage", "schedules", "channels", "keys", "archive", "users", "settings", "audit",
            ],
        )
        for icon_name in (
            "bot", "flask", "database", "plug", "package", "fileText",
            "calendarClock", "blocks", "server", "activity", "webhook", "users",
            "chart", "clipboardList",
        ):
            self.assertIn(f"  {icon_name}:", script)
        self.assertIn('class="sidebar admin-sidebar"', html)
        self.assertIn('aria-label="管理类别"', html)
        self.assertIn('aria-label="当前类别功能"', html)
        sidebar = html.split('id="admin-sidebar"', 1)[1].split("</aside>", 1)[0]
        self.assertEqual(re.findall(r'data-category="([^"]+)"', sidebar),
                         ["personal", "create", "optimize", "manage"])
        self.assertNotIn("data-tab=", sidebar)
        taskbar = html.split('id="admin-section-tabs"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(taskbar.count('data-tab="'), 21)
        self.assertEqual(re.findall(r'data-admin-category="([^"]+)"', taskbar),
                         ["personal", "create", "optimize", "manage"])
        self.assertIn('aria-current="page"', html)
        self.assertIn('id="tools-subnav"', html)
        self.assertIn('["mcp", "MCP", "mcp"]', admin_script)
        self.assertNotIn("MCP 工具", html + admin_script)
        for removed_title in ("功能模块", "智能体运行", "能力与资源", "平台治理"):
            self.assertNotIn(removed_title, html)
        self.assertIn('item.setAttribute("aria-current", "page")', admin_script)
        self.assertIn(".tab.active::before", css)
        self.assertIn(".tab.active .ti", css)

    def test_root_token_usage_dashboard_is_wired_to_real_usage_api(self):
        admin_html = (ROOT / "frontend/admin.html").read_text(encoding="utf-8")
        script = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")
        css = (ROOT / "frontend/static/style.css").read_text(encoding="utf-8")
        self.assertIn('data-tab="token-usage"', admin_html)
        self.assertIn('endpoint: "/api/v1/token-usage"', script)
        self.assertIn("function renderTokenUsage", script)
        self.assertIn('data-token-action="configure-limits"', script)
        self.assertIn('data-token-action="reset-week"', script)
        self.assertIn('data-token-action="reset-month"', script)
        self.assertNotIn('data-token-action="delete-record"', script)
        self.assertIn('id="token-limit-dialog"', admin_html)
        self.assertIn('每周上限（万 Token）', admin_html)
        self.assertIn('step="0.0001"', admin_html)
        self.assertIn("const TOKEN_LIMIT_UNIT = 10_000", script)
        self.assertIn("tokenLimitToWan", script)
        self.assertIn("tokenLimitFromWan", script)
        self.assertIn("formatTokenLimitWan", script)
        self.assertIn("Auth.role() === \"root\"", script)
        self.assertIn("handleTokenUsageAction", script)
        self.assertIn("formatAuditTime(item.created_at)", script)
        self.assertNotIn("formatLocalTime(", script)
        self.assertIn(".token-dashboard", css)
        self.assertIn(".token-heatmap", css)


if __name__ == "__main__":
    unittest.main()
