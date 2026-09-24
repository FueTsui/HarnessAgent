"""Browser-independent recovery and real-tool repair presentations."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RunWorkspaceRecoveryTests(unittest.TestCase):
    def run_js(self, scenario, extra=""):
        result = subprocess.run(["node", "-"], cwd=ROOT, text=True, encoding="utf-8",
            input='const assert = require("node:assert/strict");\n'
                  'const RunWorkspace = require("./frontend/static/run-workspace.js");\n'
                  + extra + "\n" + scenario,
            capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_recovery_and_repair_track_progress_without_inventing_completion_or_approval(self):
        self.run_js(r'''
const view = RunWorkspace.createProjection();
const events = [
  ["task.contract.updated", {revision: 2, mode: "redirect"}, "decide"],
  ["capability.activated", {kind: "skill", name: "pdf"}, "prepare"],
  ["recovery.resumed", {checkpoint_revision: 3, iteration: 4}, "prepare"],
  ["invocation.reused", {tool: "read", call_id: "c1"}, "observe"],
  ["verification.repair.started", {attempt: 1, issues_count: 2}, "act"],
  ["tool.called", {tool: "write"}, "act"],
  ["verification.repair.completed", {attempt: 1, issues_count: 0, ok: true}, "verify"],
  ["recovery.blocked", {tool: "write", call_id: "c2", reason: "unknown_outcome"}, "observe"],
];
events.forEach(([event_type, payload, phase], index) => {
  assert.equal(RunWorkspace.observe(view, {event_type, payload, event_id: `e${index}`}), true);
  assert.equal(view.phase, phase);
});
assert.ok(view.detail.includes("结果未知"));
assert.ok(view.detail.includes("核实外部状态"));
assert.equal(RunWorkspace.recoveryPresentation("recovery.blocked").kind, "warning");
assert.equal(RunWorkspace.approvalFromSnapshot({status: "running"}), null);
assert.equal(RunWorkspace.observe(view, {event_type: "invocation.reused", event_id: "e3"}), false);
RunWorkspace.observe(view, {event_type: "task.failed"});
assert.equal(view.phase, "failed");
for (const [event_type, payload] of events) RunWorkspace.observe(view, {event_type, payload});
assert.equal(view.phase, "failed", "late recovery facts cannot overwrite terminal state");
const parent = RunWorkspace.createProjection();
RunWorkspace.observe(parent, {event_type: "tool.called"});
for (const [event_type, payload] of events) {
  assert.equal(RunWorkspace.observe(parent, {event_type, payload: {...payload, execution_scope: "inline_subagent"}}), false);
}
assert.equal(parent.phase, "act");
''')

    def test_new_presentations_ignore_raw_detail_and_reject_nested_public_fields(self):
        self.run_js(r'''
const payload = {mode: {text: "PRIVATE"}, kind: "__proto__", name: {text: "PRIVATE"},
  tool: "<think>PRIVATE</think>", revision: true, checkpoint_revision: "PRIVATE", iteration: Infinity,
  attempt: {text: "PRIVATE"}, issues_count: -1, ok: "PRIVATE", messages: "PRIVATE",
  arguments: "PRIVATE", result: "PRIVATE", hash: "PRIVATE", reasoning: "PRIVATE"};
for (const event_type of ["task.contract.updated", "capability.activated", "invocation.reused",
  "recovery.resumed", "recovery.blocked", "verification.repair.started", "verification.repair.completed"]) {
  const presentation = RunWorkspace.recoveryPresentation(event_type, payload);
  assert.ok(presentation);
  assert.ok(!JSON.stringify(presentation).includes("PRIVATE"));
  const view = RunWorkspace.createProjection();
  RunWorkspace.observe(view, {event_type, payload}, "PRIVATE");
  assert.ok(!view.detail.includes("PRIVATE"));
}
assert.equal(RunWorkspace.recoveryPresentation("verification.repair.completed", payload).kind, "warning");
assert.equal(RunWorkspace.recoveryPresentation("unknown.event", payload), null);
assert.ok(RunWorkspace.recoveryPresentation("invocation.reused", {tool: "read"}).text.includes("已有结果"));
assert.ok(RunWorkspace.recoveryPresentation("verification.repair.started", {attempt: 1, issues_count: 2}).text.includes("调用工具修复"));
''')

    def test_app_renderer_and_event_filters_use_shared_recovery_semantics(self):
        app = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")
        renderer = app[app.index("function runtimeEventPresentation("):app.index("function recordRuntimeActivity(")]
        self.run_js(r'''
for (const event_type of ["task.contract.updated", "capability.activated", "invocation.reused",
  "recovery.resumed", "recovery.blocked", "verification.repair.started", "verification.repair.completed"]) {
  const payload = {revision: 2, mode: "guide", kind: "skill", name: "pdf", tool: "read", attempt: 1, issues_count: 0, ok: true};
  assert.deepEqual(runtimeEventPresentation(event_type, payload), RunWorkspace.recoveryPresentation(event_type, payload));
}
assert.equal(RunWorkspace.eventCategory("invocation.reused"), "tools");
assert.equal(RunWorkspace.eventCategory("recovery.blocked"), "tools");
assert.equal(RunWorkspace.eventCategory("recovery.resumed"), "activity");
assert.equal(RunWorkspace.eventCategory("verification.repair.started"), "verification");
assert.equal(RunWorkspace.eventCategory("verification.repair.completed"), "verification");
''', renderer)


if __name__ == "__main__":
    unittest.main()
