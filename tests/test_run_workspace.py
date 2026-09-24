"""Behavior checks for public phase projection, approval recovery and live updates."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")


def function_source(start, end):
    return APP[APP.index(start):APP.index(end)]


class RunWorkspaceTests(unittest.TestCase):
    def test_content_guardrail_label_exposes_only_decision_and_does_not_end_task(self):
        self.run_js(r'''
const view = RunWorkspace.createProjection();
const event = {event_type: "guardrail.content_evaluated", payload: {
  point: "model_output", decision: "block", content: "PRIVATE_TEXT", matches: [{value: "PRIVATE_TEXT"}],
}};
assert.equal(RunWorkspace.eventCategory(event.event_type), "verification");
RunWorkspace.observe(view, event, "PRIVATE_TEXT");
assert.equal(view.phase, "verify");
assert.equal(view.detail, "模型输出护栏：已阻止内容传递");
assert.ok(!JSON.stringify(RunWorkspace.contentGuardrailPresentation(event.payload)).includes("PRIVATE_TEXT"));
assert.equal(RunWorkspace.contentGuardrailPresentation({decision:"other"}).kind, "warning");
RunWorkspace.observe(view, {event_type: "tool.called"});
assert.equal(view.phase, "act");
RunWorkspace.observe(view, {event_type: "task.failed"});
assert.equal(view.phase, "failed");
RunWorkspace.observe(view, event);
assert.equal(view.phase, "failed");
''')

    def run_js(self, scenario, extra=""):
        result = subprocess.run(
            ["node", "-"], cwd=ROOT, text=True, encoding="utf-8",
            input=(
                'const assert = require("node:assert/strict");\n'
                'const RunWorkspace = require("./frontend/static/run-workspace.js");\n'
                + extra + '\n(async () => {\n' + scenario
                + '\n})().catch(error => { console.error(error); process.exitCode = 1; });'
            ), capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_phase_projection_tracks_observed_events_and_preserves_limited_completion(self):
        self.run_js(r'''
const view = RunWorkspace.createProjection();
assert.deepEqual(view.visited, []);
const events = ["task.queued", "loop.iteration.started", "tool.called", "tool.completed", "evaluation.started"];
const expected = ["prepare", "decide", "act", "observe", "verify"];
events.forEach((event_type, index) => {
  RunWorkspace.observe(view, {event_type, event_id: String(index)});
  assert.equal(view.phase, expected[index]);
});
assert.deepEqual(view.visited, expected);
assert.equal(RunWorkspace.observe(view, {event_type: "tool.called", event_id: "2"}), false);
assert.equal(view.phase, "verify");
RunWorkspace.observe(view, {event_type: "task.status", payload: {status: "completed_with_issues"}});
RunWorkspace.observe(view, {event_type: "turn.completed"});
assert.equal(view.phase, "completed_with_issues");
RunWorkspace.observe(view, {event_type: "loop.iteration.started"});
assert.equal(view.phase, "completed_with_issues");
const nested = RunWorkspace.createProjection();
RunWorkspace.observe(nested, {event_type: "tool.called"});
RunWorkspace.observe(nested, {event_type: "task.completed", payload: {execution_scope: "inline_subagent"}});
assert.equal(nested.phase, "act");
assert.equal(RunWorkspace.phaseForEvent("unknown.event"), null);
assert.equal(RunWorkspace.phaseForEvent("approval.policy"), null);
''')

    def test_approval_is_single_flight_and_failures_remain_retryable(self):
        self.run_js(r'''
const request = RunWorkspace.createApprovalRequest({scope: "write"});
let calls = 0, finish;
const attempt = RunWorkspace.decideApproval(request, "approve", () => {
  calls += 1; return new Promise(resolve => { finish = resolve; });
});
assert.equal(request.pending, true);
assert.equal(request.resolved, false);
assert.equal(await RunWorkspace.decideApproval(request, "cancel", () => { calls += 1; }), false);
assert.equal(calls, 1);
finish();
assert.equal(await attempt, true);
assert.equal(request.resolved, true);
assert.equal(await RunWorkspace.decideApproval(request, "approve", () => { calls += 1; }), false);
const retry = RunWorkspace.createApprovalRequest({scope: "shell"});
assert.equal(await RunWorkspace.decideApproval(retry, "cancel", async () => { throw new Error("offline"); }), false);
assert.equal(retry.pending, false);
assert.equal(retry.resolved, false);
assert.equal(retry.error, "offline");
assert.equal(await RunWorkspace.decideApproval(retry, "cancel", async () => {}), true);
assert.equal(retry.error, "");
''')

    def test_snapshot_restores_approval_after_its_audit_event_was_seen(self):
        extra = function_source("function restoreRunApproval", "function settleRunApproval")
        self.run_js(r'''
const article = {_agentWork: {seenEventIds: new Set(["approval-17"])}};
let restored = null;
global.showRunApproval = (jobId, target, details) => { restored = {jobId, target, details}; };
global.settleRunApproval = () => { restored = null; };
restoreRunApproval("job-1", article, {
  status: "awaiting_approval", approval_scope: "write", approval_description: "创建报告.md",
});
assert.equal(restored.jobId, "job-1");
assert.equal(restored.target, article);
assert.deepEqual(restored.details, {scope: "write", description: "创建报告.md"});
restoreRunApproval("job-1", article, {status: "running"});
assert.equal(restored, null);
assert.equal(RunWorkspace.approvalFromSnapshot({status: "done", approval_scope: "write"}), null);
''', extra)

    def test_stream_continues_past_duplicate_approval_without_waiting_for_user(self):
        extra = function_source("async function streamRun", "async function connectRun")
        self.run_js(r'''
const received = [];
global.state = {pageLeaving: false};
global.showRunApproval = () => received.push("approval-control");
global.rememberAgentWorkEvent = (_, event) => event.type !== "approval";
global.setRunStatus = text => received.push(text);
global.addTurnEvent = () => {};
global.updateAgentWork = () => {};
global.addAgentWorkActivity = () => {};
global.settleRunApproval = () => received.push("settled");
global.updateRunPhase = () => {};
global.setAnswerDeliveryState = () => {};
global.finishAgentWork = () => {};
const events = [
  {type: "approval", event_id: "already-seen", scope: "write"},
  {type: "progress", text: "still receiving"},
  {type: "end", status: "cancelled"},
];
let read = false;
global.fetch = async () => ({ok: true, body: {getReader: () => ({read: async () => {
  assert.equal(read, false); read = true;
  return {value: new TextEncoder().encode(events.map(JSON.stringify).join("\n") + "\n"), done: false};
}})}});
const result = await streamRun("job", {_agentWork: {lastEventRevision: 0}, querySelector: () => ({})});
assert.equal(result, "cancelled");
assert.ok(received.indexOf("still receiving") > received.indexOf("approval-control"));
assert.ok(received.includes("settled"));
''', extra)

    def test_shared_connection_retries_until_real_terminal_status(self):
        extra = function_source("async function connectRun", "async function resumeActiveJob")
        self.run_js(r'''
global.state = {pageLeaving: false};
global.setTimeout = callback => callback();
let calls = 0;
global.streamRun = async () => ++calls < 3 ? "reconnect" : "done";
const article = {_agentWork: {reconnectAttempts: 0}};
assert.equal(await connectRun("job", article), "done");
assert.equal(calls, 3);
assert.equal(article._agentWork.reconnectAttempts, 0);
''', extra)

    def test_scroll_preserves_history_and_explicit_jump_returns_to_latest(self):
        extra = function_source("function scrollToLatest", 'log.addEventListener("scroll"')
        self.run_js(r'''
global.state = {followLatest: false};
global.log = {scrollTop: 100, scrollHeight: 1800, clientHeight: 600};
const button = {hidden: true};
global.$ = () => button;
scrollToLatest();
assert.equal(log.scrollTop, 100);
assert.equal(button.hidden, false);
log.scrollHeight = 2100;
scrollToLatest();
assert.equal(log.scrollTop, 100);
scrollToLatest(true);
assert.equal(log.scrollTop, 2100);
assert.equal(button.hidden, true);
assert.equal(state.followLatest, true);
assert.equal(RunWorkspace.nearLatest({scrollHeight: 1000, scrollTop: 395, clientHeight: 600}), true);
''', extra)

    def test_event_filters_keep_unknown_activity_out_of_tools_and_verification(self):
        self.run_js(r'''
const events = ["tool.called", "delegation.completed", "approval.requested", "verification.failed", "evaluation.completed", "task.started"];
const categories = events.map(RunWorkspace.eventCategory);
assert.deepEqual(categories, ["tools", "tools", "approval", "verification", "verification", "activity"]);
assert.equal(categories.filter(category => RunWorkspace.matchesFilter(category, "all")).length, 6);
assert.equal(categories.filter(category => RunWorkspace.matchesFilter(category, "tools")).length, 2);
assert.equal(categories.filter(category => RunWorkspace.matchesFilter(category, "approval")).length, 1);
''')

    def test_layout_growth_does_not_cancel_explicit_follow_latest(self):
        listener = APP[APP.index('log.addEventListener("scroll"'):]
        listener = listener[:listener.index('}, {passive: true});') + len('}, {passive: true});')]
        setup = r'''
global.state = {followLatest: true, lastScrollTop: 400};
global.log = {scrollHeight: 1200, scrollTop: 400, clientHeight: 600,
  addEventListener: (_, handler) => { global.onScroll = handler; }};
global.$ = () => ({hidden: false});
'''
        self.run_js(r'''
onScroll();
assert.equal(state.followLatest, true, "growing content keeps automatic follow");
log.scrollTop = 250;
onScroll();
assert.equal(state.followLatest, false, "reading upwards stops automatic follow");
log.scrollTop = 600;
onScroll();
assert.equal(state.followLatest, true, "returning to the bottom resumes follow");
''', setup + listener)

    def test_drawer_replay_uses_observed_event_time(self):
        extra = function_source("function addTurnEvent", "const TOOL_ACTIONS")
        extra += function_source("function recordWorkspaceDrawerEvent", "function showRunApproval")
        self.run_js(r'''
const rows = [];
global.document = {createElement: () => ({dataset: {}, innerHTML: ""})};
global.$ = () => ({querySelectorAll: () => [], appendChild: row => rows.push(row)});
global.filterRunEvents = () => {};
global.escapeHtml = value => String(value);
global.runtimeActivityIdentity = () => "";
const timestamp = "2001-02-03T04:05:06+00:00";
recordWorkspaceDrawerEvent("tool.completed", {tool: "read"}, {text: "已读取", kind: "done"}, timestamp);
assert.ok(rows[0].innerHTML.includes('datetime="2001-02-03T04:05:06.000Z"'));
assert.ok(rows[0].innerHTML.includes(new Date(timestamp).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})));
assert.equal(rows[0].dataset.category, "tools");
addTurnEvent("时间不可用", "progress", "", "activity", "invalid-time");
assert.ok(rows[1].innerHTML.includes('datetime="">—</time>'));
''', extra)


if __name__ == "__main__":
    unittest.main()
