"""Explicit MCP selection survives both routing stages without granting access."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.runtime import builtin_tools, orchestrator
from backend.runtime.control import route_tools
from tests.test_tool_contracts import _Connection, _SequenceLlm, _spec


class ExplicitMcpRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_large_live_catalog_preserves_selected_financial_tools(self):
        tools = [_spec("web_search"), _spec("web_fetch")]
        tools += [_spec(f"other_{i}") for i in range(280)]
        tools += [_spec("Tushare_daily"), _spec("Tushare_index_daily")]
        selected = route_tools(tools, "根据今天市场行情预测一下明天科技趋势",
                               threshold=10, limit=7,
                               preferred_names={"Tushare_daily", "Tushare_index_daily", "forged"})
        names = {item["function"]["name"] for item in selected}
        self.assertTrue({"Tushare_daily", "Tushare_index_daily", "web_search"} <= names)
        self.assertNotIn("forged", names)
        self.assertLessEqual(len(selected), 7)

    def test_control_tools_cannot_fill_all_slots_before_explicit_choice(self):
        tools = [_spec(name) for name in ["update_plan", "read_skill_resource", "create_skill",
                                         "web_search", "web_fetch", "Tushare_daily"]]
        selected = route_tools(tools, "今天市场行情", threshold=2, limit=2,
                               preferred_names={"Tushare_daily"})
        self.assertIn("Tushare_daily", {item["function"]["name"] for item in selected})
        self.assertEqual(len(selected), 2)

    async def test_model_selector_cannot_drop_explicit_mcp_and_dispatch_uses_binding(self):
        connection = _Connection()
        model = _SequenceLlm([("remote_read_item", {})])
        router = _SequenceLlm([("select_tools", {"choices": ["web_search"], "confidence": .99})] * 4)
        events = []
        with (
            patch.object(orchestrator, "guard_model_client", side_effect=lambda client, **kw: client),
            patch.object(orchestrator, "enforce_content", new_callable=AsyncMock),
            patch.object(orchestrator.mcp_client, "McpConnection", return_value=connection),
            patch.object(builtin_tools, "execute", new_callable=AsyncMock,
                         return_value={"ok": True, "text": "actual web evidence"}),
        ):
            await orchestrator.run_harness(
                model, "依据证据回答", "根据今天市场行情预测一下明天科技趋势",
                mcp_servers=[SimpleNamespace(id=5, name="remote", risk_policy="auto")],
                preferred_mcp_ids={5}, role_clients={"router": router},
                model_execution={"planning": {"mode": "off"}, "tool_routing": {"mode": "model"}},
                builtin_context=builtin_tools.BuiltinToolContext(enabled_tools={"web_search"}),
                tool_policy={"profile": "standard", "router": {"activation_threshold": 2, "max_candidates": 7}},
                runtime_event=lambda name, payload: events.append((name, payload)),
            )
        connection.call_tool_result.assert_awaited_once()
        self.assertIn("remote_read_item", model.offered[0])
        self.assertTrue(router.offered)
        selections = [p for n, p in events if n == "tools.selection"]
        self.assertIn("remote_read_item", selections[0]["offered"])
        system = model.messages[0]["content"]
        self.assertIn('"available_count": 1', system)
        self.assertIn("未出现在候选中不等于未绑定", system)

    async def test_preference_never_restores_denied_tools(self):
        connection = _Connection()
        model = _SequenceLlm([])
        with patch.object(orchestrator.mcp_client, "McpConnection", return_value=connection):
            await orchestrator.run_harness(
                model, "回答", "读取结果",
                mcp_servers=[SimpleNamespace(id=5, name="remote", risk_policy="auto")],
                preferred_mcp_ids={5}, builtin_context=builtin_tools.BuiltinToolContext(enabled_tools={"web_search"}),
                tool_policy={"denied_tools": ["remote_read_item"]},
            )
        self.assertNotIn("remote_read_item", set().union(*model.offered))
        connection.call_tool_result.assert_not_awaited()

    async def test_connection_failure_reports_state_without_leaking_endpoint(self):
        model = _SequenceLlm([])
        with patch.object(orchestrator.mcp_client, "McpConnection",
                          side_effect=RuntimeError("private-endpoint-secret")):
            await orchestrator.run_harness(
                model, "回答", "读取结果",
                mcp_servers=[SimpleNamespace(id=5, name="remote", risk_policy="auto")],
                preferred_mcp_ids={5}, builtin_context=builtin_tools.BuiltinToolContext(enabled_tools={"web_search"}),
            )
        context = json.dumps(model.messages, ensure_ascii=False)
        self.assertIn("connection_failed", context)
        self.assertNotIn("private-endpoint-secret", context)


if __name__ == "__main__":
    unittest.main()
