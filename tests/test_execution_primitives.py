"""Contract and scheduler behavior independent of model/provider availability."""
import asyncio
import json
import unittest

from backend.runtime.scheduler import ScheduledCall, run_ordered_batch
from backend.runtime.skill_resources import SkillResourceIndex
from backend.runtime.task_contract import TaskContract
from backend.runtime.tool_contracts import McpToolBinding, ToolContract


def _spec(name, properties=None):
    return {"type": "function", "function": {
        "name": name, "description": "description",
        "parameters": {"type": "object", "properties": properties or {}},
    }}


class TaskContractTests(unittest.TestCase):
    def test_guidance_is_idempotent_and_redirect_preserves_original(self):
        contract = TaskContract("生成报告")
        self.assertTrue(contract.apply_guidance("使用中文", guidance_id=11))
        self.assertIn("生成报告", contract.objective)
        self.assertIn("使用中文", contract.objective)
        self.assertFalse(contract.apply_guidance("重复投递", guidance_id="11"))
        self.assertEqual(contract.revision, 2)
        self.assertTrue(contract.apply_guidance("先修复导出错误", "redirect", 12))
        self.assertEqual(contract.objective, "先修复导出错误")
        self.assertEqual(contract.original_objective, "生成报告")
        self.assertTrue(contract.apply_guidance("保留源文件", guidance_id=13))
        restored = TaskContract.from_snapshot(json.loads(json.dumps(contract.snapshot())))
        self.assertEqual(restored.snapshot(), contract.snapshot())
        self.assertFalse(restored.apply_guidance("先修复导出错误", "redirect", 12))
        self.assertEqual(restored.revision, 4)

    def test_public_metadata_does_not_include_original_or_steering(self):
        contract = TaskContract("PRIVATE_ORIGINAL")
        contract.apply_guidance("PRIVATE_GUIDANCE", "redirect", "PRIVATE_ID")
        public = contract.public_snapshot()
        self.assertEqual(public, {"version": 1, "revision": 2,
                                  "guidance_count": 1, "redirected": True})
        self.assertNotIn("PRIVATE", json.dumps(public))
        self.assertEqual(TaskContract.from_snapshot(None, "fallback").objective, "fallback")
        with self.assertRaises(ValueError):
            TaskContract.from_snapshot({"version": 99})


class CapabilityMetadataTests(unittest.TestCase):
    def test_revision_is_stable_but_schema_and_target_changes_invalidate_it(self):
        definition = _spec("read", {"path": {"type": "string"}, "limit": {"type": "integer"}})
        reordered = json.loads(json.dumps(definition, sort_keys=True))
        a = ToolContract("read", "builtin", definition)
        b = ToolContract("read", "builtin", reordered)
        self.assertEqual(a.capability_revision, b.capability_revision)
        reordered["function"]["parameters"]["properties"]["limit"]["maximum"] = 10
        self.assertNotEqual(a.capability_revision, b.capability_revision)
        risk = {"mutating": False, "destructive": False, "classification_source": "annotation"}
        first = ToolContract("remote_read", "mcp", _spec("remote_read"),
                             McpToolBinding(object(), "read", risk, {}, 1, "one"))
        reconnected = ToolContract("remote_read", "mcp", _spec("remote_read"),
                                   McpToolBinding(object(), "read", risk, {}, 1, "one"))
        other = ToolContract("remote_read", "mcp", _spec("remote_read"),
                             McpToolBinding(object(), "read", risk, {}, 2, "two"))
        self.assertEqual(first.capability_revision, reconnected.capability_revision)
        self.assertNotEqual(first.capability_revision, other.capability_revision)

    def test_only_explicitly_known_reads_can_overlap_or_replay(self):
        for name in ("read", "read_many", "ls", "glob", "grep", "git_diff", "web_fetch"):
            self.assertTrue(ToolContract(name, "builtin", _spec(name)).read_only)
        for name in ("shell", "browser_open", "browser_snapshot", "lsp", "spawn_agent", "future_read"):
            self.assertFalse(ToolContract(name, "builtin", _spec(name)).read_only)
        self.assertFalse(ToolContract("read", "agent", _spec("read")).read_only)
        self.assertFalse(ToolContract("update_plan", "control", _spec("update_plan")).read_only)
        for risk, expected in (
            ({"mutating": False, "destructive": False, "classification_source": "annotation"}, True),
            ({"mutating": False, "destructive": False, "classification_source": "heuristic"}, False),
            ({"mutating": False, "destructive": False, "classification_source": "server_policy"}, False),
            ({"mutating": False, "destructive": True, "classification_source": "annotation"}, False),
            ({}, False),
        ):
            binding = McpToolBinding(None, "read", risk, {}, 1, "server")
            self.assertEqual(ToolContract("remote_read", "mcp", _spec("remote_read"), binding).read_only,
                             expected)


class SkillDiscoveryTests(unittest.TestCase):
    def test_descriptor_discovery_never_loads_bodies_into_prompt(self):
        index = SkillResourceIndex([{
            "name": "writer", "description": "编写报告", "instructions": "PRIVATE_ENTRY_BODY",
            "resources": [
                {"name": "reference.md", "content": "PRIVATE_REFERENCE_BODY"},
                {"name": "asset.bin", "binary": True, "content": "PRIVATE_BINARY_BODY"},
            ],
        }])
        for discovery in (json.dumps(index.descriptors), index.descriptor_prompt()):
            self.assertNotIn("PRIVATE", discovery)
            self.assertIn("SKILL.md", discovery)
        self.assertIn("read_skill_resource", index.descriptor_prompt())
        self.assertEqual(json.loads(index.read("writer", "SKILL.md"))["content"], "PRIVATE_ENTRY_BODY")


class OrderedSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_really_overlap_with_a_bound_and_return_model_order(self):
        active = 0
        peak = 0
        completed = []
        first_pair_ready = asyncio.Event()

        def operation(index):
            async def run():
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                if index == 1:
                    first_pair_ready.set()
                if index == 0:
                    await asyncio.wait_for(first_pair_ready.wait(), 1)
                    await asyncio.sleep(.01)
                else:
                    await asyncio.sleep(0)
                completed.append(index)
                active -= 1
                return index
            return run

        result = await run_ordered_batch(
            [ScheduledCall(str(i), operation(i), True) for i in range(5)], max_parallel_calls=2,
        )
        self.assertEqual(result, list(range(5)))
        self.assertEqual(peak, 2)
        self.assertEqual(completed[:2], [1, 0])

    async def test_exclusive_calls_form_barriers_between_read_windows(self):
        timeline = []
        active_reads = 0

        def read(name):
            async def run():
                nonlocal active_reads
                active_reads += 1
                timeline.append(f"start:{name}")
                await asyncio.sleep(0)
                timeline.append(f"end:{name}")
                active_reads -= 1
                return name
            return run

        async def write():
            self.assertEqual(active_reads, 0)
            timeline.append("write")
            await asyncio.sleep(0)
            self.assertNotIn("start:c", timeline)
            return "write"

        result = await run_ordered_batch([
            ScheduledCall("a", read("a"), True), ScheduledCall("b", read("b"), True),
            ScheduledCall("write", write), ScheduledCall("c", read("c"), True),
        ], max_parallel_calls=3)
        self.assertEqual(result, ["a", "b", "write", "c"])
        self.assertLess(timeline.index("end:a"), timeline.index("write"))
        self.assertLess(timeline.index("end:b"), timeline.index("write"))
        self.assertLess(timeline.index("write"), timeline.index("start:c"))

    async def test_failure_settles_started_reads_before_propagating_original_exception(self):
        marker = RuntimeError("approval-like stop")
        state = []

        async def failed():
            raise marker

        async def finishing():
            await asyncio.sleep(.01)
            state.append("settled")
            return "ok"

        async def later():
            state.append("must not dispatch")

        with self.assertRaises(RuntimeError) as raised:
            await run_ordered_batch([
                ScheduledCall("failed", failed, True), ScheduledCall("finishing", finishing, True),
                ScheduledCall("later", later),
            ], max_parallel_calls=2)
        self.assertIs(raised.exception, marker)
        self.assertEqual(state, ["settled"])

    async def test_synchronous_closure_failure_cannot_orphan_an_earlier_task(self):
        settled = []

        async def finishing():
            await asyncio.sleep(0)
            settled.append("finished")

        def rejected():
            raise ValueError("callable failed before returning an awaitable")

        with self.assertRaisesRegex(ValueError, "callable failed"):
            await run_ordered_batch([
                ScheduledCall("finishing", finishing, True),
                ScheduledCall("rejected", rejected, True),
            ], max_parallel_calls=2)
        self.assertEqual(settled, ["finished"])

    async def test_parent_cancellation_reaps_all_started_read_tasks(self):
        started = asyncio.Event()
        running = 0
        reaped = []

        def read(index):
            async def run():
                nonlocal running
                running += 1
                if running == 2:
                    started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    reaped.append(index)
            return run

        task = asyncio.create_task(run_ordered_batch([
            ScheduledCall("a", read(0), True), ScheduledCall("b", read(1), True),
        ], max_parallel_calls=2))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertCountEqual(reaped, [0, 1])


if __name__ == "__main__":
    unittest.main()
