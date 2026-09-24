"""Integrated adaptive execution without live providers, services or databases."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from backend.runtime import builtin_tools, orchestrator


def _call(identity, name, arguments):
    return {"id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}


def _batch(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def _answer(text="已根据实际工具结果完成。"):
    return {"role": "assistant", "content": text, "tool_calls": []}


class _ScriptedModel:
    context_tokens = 32000

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def chat_with_tools(self, messages, tools=None, **_kwargs):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if not self.replies:
            raise AssertionError("Model exceeded its bounded scripted conversation")
        return copy.deepcopy(self.replies.pop(0))

    async def chat(self, system, user, **kwargs):
        return (await self.chat_with_tools([
            {"role": "system", "content": system}, {"role": "user", "content": user},
        ], **kwargs))["content"]


class AdaptiveExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, model, execute, *, names=("read",), events=None,
                   query="读取工作区文件", tool_policy=None, verification_policy=None,
                   guidance=None):
        target = events if events is not None else []
        specs = [builtin_tools.TOOLS[name].spec() for name in names]
        with (
            patch.object(orchestrator, "guard_model_client", side_effect=lambda client, **_kwargs: client),
            patch.object(orchestrator, "enforce_content", new_callable=AsyncMock),
            patch.object(orchestrator, "execution_store", return_value=None),
            patch.object(builtin_tools, "tool_specs", return_value=specs),
            patch.object(builtin_tools, "execute", side_effect=execute),
        ):
            return await asyncio.wait_for(orchestrator.run_harness(
                model, "使用工具证据回答", query,
                model_execution={"planning": {"mode": "off"}},
                builtin_context=builtin_tools.BuiltinToolContext(enabled_tools=set(names)),
                runtime_event=lambda name, payload: target.append((name, copy.deepcopy(payload))),
                tool_policy={"profile": "standard", "max_iterations": 8,
                             "max_parallel_calls": 2, **(tool_policy or {})},
                verification_policy=verification_policy, guidance=guidance,
            ), timeout=5)

    async def test_read_calls_overlap_with_bounded_parallelism_and_ordered_model_results(self):
        active = 0
        peak = 0
        finished = []
        pair_ready = [asyncio.Event(), asyncio.Event()]

        async def execute(name, args, context):
            nonlocal active, peak
            self.assertEqual(name, "read")
            index = int(args["path"].split(".")[0])
            active += 1
            peak = max(peak, active)
            if index % 2:
                pair_ready[index // 2].set()
            else:
                await asyncio.wait_for(pair_ready[index // 2].wait(), 1)
                await asyncio.sleep(.01)
            finished.append(index)
            active -= 1
            return {"ok": True, "text": f"READ_RESULT_{index}"}

        model = _ScriptedModel([
            _batch(*[_call(f"r{i}", "read", {"path": f"{i}.txt"}) for i in range(4)]),
            _answer(),
        ])
        await self._run(model, execute)
        self.assertEqual(peak, 2)
        self.assertEqual(finished, [1, 0, 3, 2])
        observed = [item for item in model.requests[1]["messages"] if item["role"] == "tool"]
        self.assertEqual([item["tool_call_id"] for item in observed], ["r0", "r1", "r2", "r3"])
        for index, item in enumerate(observed):
            self.assertIn(f"READ_RESULT_{index}", item["content"])

    async def test_write_is_an_exclusive_barrier_without_truncating_standard_batch(self):
        active_reads = 0
        timeline = []
        reads_started = asyncio.Event()

        async def execute(name, args, context):
            nonlocal active_reads
            if name == "read":
                active_reads += 1
                timeline.append(f"start:{args['path']}")
                if active_reads == 2:
                    reads_started.set()
                await asyncio.wait_for(reads_started.wait(), 1)
                await asyncio.sleep(0)
                active_reads -= 1
                timeline.append(f"end:{args['path']}")
            else:
                self.assertEqual(name, "write")
                self.assertEqual(active_reads, 0)
                self.assertIn("end:a.txt", timeline)
                self.assertIn("end:b.txt", timeline)
                timeline.append("write")
            return {"ok": True, "text": name}

        model = _ScriptedModel([
            _batch(_call("a", "read", {"path": "a.txt"}),
                   _call("b", "read", {"path": "b.txt"}),
                   _call("w", "write", {"path": "out.txt", "content": "result", "confirm": True})),
            _answer(),
        ])
        events = []
        await self._run(model, execute, names=("read", "write"), events=events)
        self.assertEqual(timeline[-1], "write")
        self.assertFalse(any(name == "tool.deferred" for name, _ in events))
        results = [row for row in model.requests[1]["messages"] if row["role"] == "tool"]
        self.assertEqual([row["tool_call_id"] for row in results], ["a", "b", "w"])

    async def test_missing_evidence_returns_to_real_tools_before_final_verification(self):
        model = _ScriptedModel([
            _answer("未取得证据却声称已经完成"),
            _batch(_call("repair-read", "read", {"path": "evidence.txt"})),
            _answer("已读取实际证据，任务完成。"),
        ])
        execute = AsyncMock(return_value={"ok": True, "text": "ACTUAL_EVIDENCE"})
        events = []
        answer, _ = await self._run(model, execute, events=events, verification_policy={
            "require_successful_tool": True, "max_revisions": 1,
        })
        self.assertEqual(answer, "已读取实际证据，任务完成。")
        execute.assert_awaited_once()
        self.assertIn("read", {item["function"]["name"] for item in model.requests[1]["tools"]})
        self.assertTrue(any("没有任何工具成功证据" in str(row["content"])
                            for row in model.requests[1]["messages"] if row["role"] == "system"))
        started = [payload for name, payload in events if name == "verification.repair.started"]
        completed = [payload for name, payload in events if name == "verification.repair.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(completed[-1]["ok"], True)

    async def test_failed_action_repair_is_bounded_and_cannot_be_reported_as_success(self):
        model = _ScriptedModel([
            _answer(), _batch(_call("failed-read", "read", {"path": "missing.txt"})), _answer(),
        ])
        execute = AsyncMock(return_value={"ok": False, "error": {"code": "missing"}})
        events = []
        with self.assertRaisesRegex(orchestrator.CompletionVerificationError, "没有任何工具成功证据"):
            await self._run(model, execute, events=events, verification_policy={
                "require_successful_tool": True, "max_revisions": 1,
            })
        self.assertEqual(len(model.requests), 3)
        execute.assert_awaited_once()
        self.assertEqual(sum(name == "verification.repair.started" for name, _ in events), 1)
        self.assertFalse(any(name == "loop.completed" for name, _ in events))

    async def test_final_boundary_redirect_reloads_read_evidence_and_routes_new_objective(self):
        redirect = "重新读取 settings.txt，并查询深圳当前天气"
        model = _ScriptedModel([
            _batch(_call("old-read", "read", {"path": "settings.txt"})),
            _answer("旧目标已经完成"),
            _batch(_call("new-read", "read", {"path": "settings.txt"}),
                   _call("weather", "web_search", {"query": "深圳当前天气"})),
            _answer("已重新读取配置，并根据最新检索结果回答深圳天气。"),
        ])
        executed = []

        async def execute(name, args, context):
            executed.append((name, dict(args)))
            return {"ok": True, "text": f"CURRENT_{name}_{len(executed)}"}

        def guidance():
            # It arrives while the model is composing its first final answer.
            return [{"id": "redirect-1", "mode": "redirect", "content": redirect}] if len(model.requests) >= 2 else []

        events = []
        with patch.object(orchestrator, "route_tools", wraps=orchestrator.route_tools) as route:
            await self._run(model, execute, names=("read", "web_search"), guidance=guidance,
                            events=events, verification_policy={"require_successful_tool": True})
        self.assertEqual([name for name, _ in executed], ["read", "read", "web_search"])
        self.assertTrue(any(call.args[1].startswith(redirect) for call in route.call_args_list))
        self.assertEqual(sum(name == "task.contract.updated" for name, _ in events), 1)
        final = next(payload for name, payload in events if name == "loop.completed")
        self.assertEqual(final["checkpoint"]["successful_tools"], 2)
        self.assertIn("web_search", final["checkpoint"]["successful_tool_names"])
        self.assertIn("web_search", {item["function"]["name"] for item in model.requests[2]["tools"]})

    async def test_redirect_cannot_reuse_old_success_to_pass_new_objective(self):
        model = _ScriptedModel([
            _batch(_call("old-read", "read", {"path": "first.txt"})),
            _answer("旧工作完成"), _answer("未读取第二份文件却声称完成"),
        ])
        execute = AsyncMock(return_value={"ok": True, "text": "OLD_EVIDENCE_ONLY"})
        def guidance():
            return ([{"id": "redirect-2", "mode": "redirect", "content": "改为读取 second.txt"}]
                    if len(model.requests) >= 2 else [])
        with self.assertRaisesRegex(orchestrator.CompletionVerificationError, "没有任何工具成功证据"):
            await self._run(model, execute, guidance=guidance, verification_policy={
                "require_successful_tool": True, "max_revisions": 0,
            })
        execute.assert_awaited_once()
        self.assertEqual(len(model.requests), 3)


if __name__ == "__main__":
    unittest.main()
