"""工具授权、结构化观察和执行证据必须使用同一契约。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.approvals import ApprovalRequired
from backend.llm import mcp_client
from backend.runtime import builtin_tools, orchestrator
from backend.runtime.control import analyze_observation, repair_arguments
from backend.runtime.policies import RuntimePolicies
from backend.runtime.tool_contracts import ToolCatalog


def _spec(name, parameters=None):
    return {
        "type": "function",
        "function": {
            "name": name, "description": "读取数据",
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


class _SequenceLlm:
    context_tokens = 8192

    def __init__(self, calls):
        self.calls = list(calls)
        self.offered = []
        self.messages = []

    async def chat_with_tools(self, messages, tools=None, temperature=1):
        self.messages = list(messages)
        self.offered.append({tool["function"]["name"] for tool in tools or []})
        if self.calls:
            name, args = self.calls.pop(0)
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": f"call-{len(self.offered)}", "type": "function",
                    "function": {
                        "name": name, "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }],
            }
        return {"role": "assistant", "content": "已读取结果", "tool_calls": []}

    async def chat(self, system, user, temperature=.5):
        return "已读取结果"


class _Connection:
    def __init__(self, result=None):
        self.result = result if result is not None else {"isError": False, "structuredContent": {"ok": True}}
        self.call_tool_result = AsyncMock(return_value=self.result)
        self.call_tool = AsyncMock(return_value="旧接口不应丢失结构化数据")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_tools(self):
        return [{
            "name": "read_item", "description": "read item",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True},
        }]


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, llm, *, events=None, policy=None, **kwargs):
        target = events if events is not None else []
        return await orchestrator.run_harness(
            llm, "根据证据回答", "读取结果",
            runtime_event=lambda name, payload: target.append((name, payload)),
            tool_policy={"profile": "standard", "max_iterations": 6, **(policy or {})},
            **kwargs,
        )

    async def test_denied_disabled_and_allowlist_exclude_every_executor(self):
        for kind, name, args in (
            ("builtin", "read", {"path": "sample.txt"}),
            ("mcp", "remote_read_item", {}),
            ("agent", "call_agent__worker", {"query": "读取内容"}),
            ("resource", "read_skill_resource", {"skill": "guide", "file": "sample.txt"}),
            ("skill", "create_skill", {"name": "test", "description": "test", "instructions": "test"}),
        ):
            for policy in (
                {"mode": "disabled"},
                {"denied_tools": [name]},
                {"mode": "allowlist", "allowed_tools": ["other"]},
            ):
                with self.subTest(kind=kind, policy=policy):
                    llm = _SequenceLlm([(name, args)])
                    events = []
                    connection = _Connection()
                    builder = AsyncMock(return_value={"ok": True})
                    with (
                        patch.object(mcp_client, "McpConnection", return_value=connection),
                        patch.object(builtin_tools, "execute", new_callable=AsyncMock) as builtin,
                        patch.object(orchestrator, "_run_descriptor", new_callable=AsyncMock) as delegate,
                    ):
                        await self._run(
                            llm, events=events, policy=policy,
                            builtin_context=builtin_tools.BuiltinToolContext(enabled_tools=["read"]),
                            mcp_servers=[SimpleNamespace(id=7, name="remote", risk_policy="auto")],
                            sub_agents=[{"id": 8, "name": "worker"}],
                            skills=[{"name": "guide", "resources": [{"name": "sample.txt", "content": "evidence"}]}],
                            skill_builder=builder,
                        )
                    builtin.assert_not_awaited()
                    delegate.assert_not_awaited()
                    builder.assert_not_awaited()
                    connection.call_tool_result.assert_not_awaited()
                    connection.call_tool.assert_not_awaited()
                    self.assertNotIn(name, llm.offered[0])
                    rejected = next(payload for event, payload in events if event == "tool.rejected")
                    self.assertEqual(rejected["call_id"], "call-1")
                    completed = next(payload for event, payload in events if event == "tool.completed")
                    self.assertFalse(completed["ok"])
                    self.assertEqual(completed["error_type"], "tool_unavailable")
                    terminal = next(payload for event, payload in events if event == "loop.completed")
                    self.assertEqual(terminal["checkpoint"]["successful_tools"], 0)

    async def test_allowed_calls_continue_after_rejection_and_keep_dispatch_binding(self):
        llm = _SequenceLlm([
            ("write", {"path": "blocked.txt", "content": "blocked"}),
            ("readanalysis", {"path": "sample.txt"}),
            ("remote_read_item", {}),
            ("call_agent__worker", {"query": "读取内容"}),
        ])
        events = []
        connection = _Connection()
        with (
            patch.object(mcp_client, "McpConnection", return_value=connection),
            patch.object(builtin_tools, "execute", new_callable=AsyncMock, return_value={"ok": True}) as builtin,
            patch.object(orchestrator, "_run_descriptor", new_callable=AsyncMock, return_value="已读取委派内容") as delegate,
        ):
            await self._run(
                llm, events=events, policy={"denied_tools": ["write"]},
                builtin_context=builtin_tools.BuiltinToolContext(enabled_tools=["read", "write"]),
                mcp_servers=[SimpleNamespace(id=7, name="remote", risk_policy="auto")],
                sub_agents=[{"id": 8, "name": "worker"}],
            )
        builtin.assert_awaited_once()
        self.assertEqual(builtin.await_args.args[0], "read")
        connection.call_tool_result.assert_awaited_once_with("read_item", {})
        connection.call_tool.assert_not_awaited()
        delegate.assert_awaited_once()
        completed = next(payload for event, payload in events if event == "loop.completed")
        self.assertEqual(completed["checkpoint"]["successful_tools"], 3)

    async def test_cached_structured_failure_never_becomes_successful_evidence(self):
        for result in (
            {"ok": False, "error": "offline"},
            '{"ok": false, "error": "offline"}',
            {"isError": True, "content": [{"type": "text", "text": "offline"}]},
            {"isError": False, "structuredContent": {"ok": False, "error": {"code": "offline"}}},
        ):
            with self.subTest(result=result):
                llm = _SequenceLlm([("read", {"path": "sample.txt"})] * 2)
                events = []
                with patch.object(builtin_tools, "execute", new_callable=AsyncMock, return_value=result) as executor:
                    with self.assertRaisesRegex(orchestrator.CompletionVerificationError, "没有任何工具成功证据"):
                        await self._run(
                            llm, events=events,
                            builtin_context=builtin_tools.BuiltinToolContext(enabled_tools=["read"]),
                            verification_policy={"require_successful_tool": True, "max_revisions": 0},
                        )
                executor.assert_awaited_once()
                completed = [payload for event, payload in events if event == "tool.completed"]
                self.assertEqual([item["ok"] for item in completed], [False, False])
                self.assertEqual([item["repeated"] for item in completed], [False, True])
                self.assertEqual(completed[0]["error_type"], completed[1]["error_type"])

    async def test_invalid_nested_input_and_non_object_input_do_not_execute(self):
        parameters = {
            "type": "object", "required": ["items"],
            "properties": {"items": {
                "type": "array", "minItems": 1,
                "items": {"type": "object", "required": ["count"], "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                }},
            }},
        }
        for args in ([], {"items": []}, {"items": [{}]}, {"items": [{"count": 1.9}]}, {"items": [{"count": 5}]}):
            with self.subTest(args=args):
                events = []
                with (
                    patch.object(builtin_tools, "tool_specs", return_value=[_spec("read", parameters)]),
                    patch.object(builtin_tools, "execute", new_callable=AsyncMock) as executor,
                ):
                    await self._run(
                        _SequenceLlm([("read", args)]), events=events,
                        builtin_context=builtin_tools.BuiltinToolContext(enabled_tools=["read"]),
                    )
                executor.assert_not_awaited()
                completed = next(payload for event, payload in events if event == "tool.completed")
                self.assertEqual(completed["error_type"], "argument_validation")

    async def test_approval_exception_is_propagated_without_retry(self):
        approval = ApprovalRequired("builtin:write", "确认写入")
        with patch.object(builtin_tools, "execute", new_callable=AsyncMock, side_effect=approval) as executor:
            with self.assertRaises(ApprovalRequired) as raised:
                await self._run(
                    _SequenceLlm([("write", {"path": "sample.txt", "content": "data", "confirm": True})]),
                    builtin_context=builtin_tools.BuiltinToolContext(enabled_tools=["write"]),
                )
        self.assertIs(raised.exception, approval)
        executor.assert_awaited_once()

    async def test_preflight_and_model_calls_share_failure_evidence_and_event_identity(self):
        events = []
        result = {"ok": False, "error": {"type": "upstream", "code": "offline"}}
        with patch.object(builtin_tools, "execute", new_callable=AsyncMock, return_value=result) as executor:
            with self.assertRaises(orchestrator.CompletionVerificationError):
                await orchestrator.run_harness(
                    _SequenceLlm([("web_search", {"query": "深圳天气"})]),
                    "根据证据回答", "深圳天气",
                    builtin_context=builtin_tools.BuiltinToolContext(enabled_tools=["web_search"]),
                    runtime_event=lambda name, payload: events.append((name, payload)),
                    verification_policy={"max_revisions": 0},
                )
        self.assertEqual(executor.await_count, 2)
        completed = [payload for event, payload in events if event == "tool.completed"]
        self.assertEqual([item["call_id"] for item in completed], ["preflight_web_search", "call-1"])
        self.assertEqual([item["ok"] for item in completed], [False, False])
        self.assertEqual([item["error_type"] for item in completed], ["upstream", "upstream"])
        self.assertEqual([item["error_code"] for item in completed], ["offline", "offline"])
        for item in completed:
            self.assertTrue({
                "tool", "call_id", "ok", "repeated", "result_chars", "targets",
                "duration_ms", "error_type", "error_code", "timeout", "retry_count",
            }.issubset(item))

    async def test_mcp_structured_failure_cannot_satisfy_completion_verification(self):
        connection = _Connection({
            "isError": False, "content": [{"type": "text", "text": "请求已接收"}],
            "structuredContent": {"ok": False, "error": {"type": "remote", "code": "denied"}},
        })
        events = []
        with patch.object(mcp_client, "McpConnection", return_value=connection):
            with self.assertRaises(orchestrator.CompletionVerificationError):
                await self._run(
                    _SequenceLlm([("remote_read_item", {})]), events=events,
                    mcp_servers=[SimpleNamespace(id=7, name="remote", risk_policy="auto")],
                    verification_policy={"require_successful_tool": True, "max_revisions": 0},
                )
        connection.call_tool_result.assert_awaited_once()
        completed = next(payload for event, payload in events if event == "tool.completed")
        self.assertFalse(completed["ok"])
        self.assertEqual(completed["error_type"], "remote")
        self.assertEqual(completed["error_code"], "denied")


class ResultContractTests(unittest.TestCase):
    def test_explicit_success_keeps_error_words_as_business_content(self):
        for result in (
            {"ok": True, "text": "HTTP 404 是本次读取文档中的示例"},
            {"isError": False, "content": [{"type": "text", "text": "HTTP 404"}]},
        ):
            self.assertTrue(analyze_observation(result).ok)
            self.assertTrue(analyze_observation(json.dumps(result)).ok)

    def test_mcp_text_json_failure_and_structured_only_success_are_preserved(self):
        failed = {"content": [{"type": "text", "text": '{"ok":false,"error":"offline"}'}]}
        self.assertFalse(analyze_observation(failed).ok)
        successful = {"structuredContent": {"ok": True, "value": 42}}
        flattened = mcp_client.flatten_tool_result(successful)
        self.assertEqual(json.loads(flattened), successful)
        self.assertTrue(analyze_observation(flattened).ok)
        mixed = {"content": [{"type": "text", "text": "完成"}], "structuredContent": {"ok": False}}
        self.assertFalse(analyze_observation(mcp_client.flatten_tool_result(mixed)).ok)

    def test_integer_repair_does_not_truncate_fractional_business_value(self):
        schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
        args, _repairs, errors = repair_arguments(schema, {"count": 1.9})
        self.assertEqual(args["count"], 1.9)
        self.assertTrue(errors)

    def test_catalog_policy_preserves_plan_control_and_denies_bound_routes(self):
        catalog = ToolCatalog()
        catalog.register(_spec("update_plan"), "control")
        catalog.register(_spec("read"), "builtin")
        selected = catalog.select(RuntimePolicies.from_dicts({"mode": "disabled"}))
        self.assertEqual(selected.names, {"update_plan"})
        self.assertIsNone(selected.get("read"))
        with self.assertRaises(ValueError):
            selected.require("read")

    def test_mcp_connection_exposes_raw_and_legacy_text_contracts(self):
        async def check():
            result = {"isError": False, "content": [], "structuredContent": {"ok": True, "value": 42}}
            connection = mcp_client.McpConnection(SimpleNamespace(name="remote"))
            connection._sse = True
            connection._session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
            self.assertEqual(await connection.call_tool_result("read_item", {}), result)
            self.assertEqual(json.loads(await connection.call_tool("read_item", {})), result)
        import asyncio
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
