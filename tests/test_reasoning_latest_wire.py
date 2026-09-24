"""Latest verified vendor wire semantics; all HTTP requests use local MockTransport."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from backend.llm.client import LLMClient, PROVIDER_PRESETS
from backend.runtime.process_view import PUBLIC_PROCESS_EVENTS, public_process_payload


def connection(model, protocol="responses", **kwargs):
    return LLMClient(base_url="https://fixture.invalid/v1", api_key="fixture",
                     model_id=model, wire_api=protocol, max_retries=0, stream_max_retries=0,
                     **kwargs)


def custom(efforts, control="effort", **kwargs):
    return {"mode": "custom", "control": control, "supported_efforts": efforts, **kwargs}


class LatestReasoningWireTests(unittest.TestCase):
    def request_body(self, c):
        bodies = []

        async def handler(request):
            bodies.append(json.loads(request.content))
            if c.wire_api == "responses":
                result = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}]}
            elif c.wire_api == "messages":
                result = {"content": [{"type": "text", "text": "answer"}]}
            else:
                result = {"choices": [{"message": {"role": "assistant", "content": "answer"}}]}
            return httpx.Response(200, json=result)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with patch("backend.llm.client.get_http_client", return_value=http):
                    return await c.chat_with_tools([{"role": "user", "content": "question"}], temperature=0.37)
        self.assertEqual(asyncio.run(run())["content"], "answer")
        self.assertEqual(len(bodies), 1)
        return bodies[0]

    def test_deepseek_chat_and_responses_send_four_native_choices_without_renaming(self):
        for model in ("deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash",
                      "deepseek-v4-flash-vision-exp", "deepseek-ai/DeepSeek-V4-Pro"):
            for protocol in ("chat_completions", "responses"):
                for effort in ("none", "low", "high", "max"):
                    with self.subTest(model=model, protocol=protocol, effort=effort):
                        body = self.request_body(connection(model, protocol, reasoning_effort=effort))
                        actual = body["reasoning_effort"] if protocol == "chat_completions" else body["reasoning"]["effort"]
                        self.assertEqual(actual, effort)
                        self.assertEqual(body["model"], model)
                        self.assertEqual(body["temperature"], 0.37)

    def test_messages_none_is_native_toggle_for_deepseek_and_hy(self):
        extra = {"thinking": {"type": "enabled", "budget_tokens": 3000, "display": "omitted"},
                 "reasoning_effort": "max", "reasoning": {"effort": "max", "summary": "auto"},
                 "output_config": {"effort": "max", "format": {"type": "json_schema"}}}
        before = copy.deepcopy(extra)
        for model in ("deepseek-flash", "deepseek-v4-pro", "hy3", "hy4-preview", "tencent/hy3"):
            body = self.request_body(connection(model, "messages", reasoning_effort="none", extra_body=extra))
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", body)
            self.assertEqual(body["reasoning"], {"summary": "auto"})
            self.assertEqual(body["output_config"], {"format": {"type": "json_schema"}})
            self.assertEqual(body["model"], model)
        self.assertEqual(extra, before)

    def test_messages_enabled_effort_resolves_inherited_disabled_toggle(self):
        for model, values in (("deepseek-flash", ("low", "high", "max")),
                              ("deepseek-ai/deepseek-v4-pro", ("low", "high", "max")),
                              ("hy3", ("low", "high")), ("hy4-preview", ("high",))):
            for effort in values:
                body = self.request_body(connection(model, "messages", reasoning_effort=effort,
                                                    extra_body={"thinking": {"type": "disabled"}}))
                self.assertEqual(body["thinking"], {"type": "enabled"})
                self.assertEqual(body["output_config"]["effort"], effort)
                self.assertNotIn("effort", body)

    def test_hy_efforts_and_conflicting_extra_toggles_in_openai_protocols(self):
        for model, efforts in (("hy3", ("none", "low", "high")), ("hy4-preview", ("none", "high"))):
            for protocol in ("chat_completions", "responses"):
                for effort in efforts:
                    body = self.request_body(connection(model, protocol, reasoning_effort=effort,
                                                        extra_body={"thinking": {"type": "disabled"}}))
                    if protocol == "responses":
                        self.assertNotIn("thinking", body)
                        self.assertEqual(body["reasoning"]["effort"], effort)
                    else:
                        self.assertEqual(body["reasoning_effort"], effort)
                        self.assertEqual(body["thinking"]["type"], "disabled" if effort == "none" else "enabled")

    def test_minimax_m3_toggle_uses_protocol_specific_shape_without_budget(self):
        for model in ("MiniMax-M3", "minimax/MiniMax-M3"):
            for protocol in ("chat_completions", "messages", "responses"):
                for toggle in ("enabled", "disabled"):
                    with self.subTest(model=model, protocol=protocol, toggle=toggle):
                        body = self.request_body(connection(model, protocol, reasoning_effort=toggle, max_tokens=512,
                                                            extra_body={"thinking": {"type": "enabled", "budget_tokens": 2048},
                                                                        "output_config": {"effort": "high"}}))
                        self.assertEqual(body["model"], model)
                        if protocol == "responses":
                            self.assertNotIn("thinking", body)
                            self.assertEqual(body["reasoning"]["effort"], "minimal" if toggle == "enabled" else "none")
                        else:
                            self.assertEqual(body["thinking"], {"type": "adaptive" if toggle == "enabled" else "disabled"})
                            self.assertNotIn("reasoning_effort", body)
                        self.assertNotIn("output_config", body)

    def test_minimax_m2_fixed_thinking_suppresses_legacy_controls_without_failing(self):
        for model in ("MiniMax-M2", "MiniMax-M2.1", "MiniMax-M2.5-highspeed", "minimax/MiniMax-M2.7"):
            for protocol in ("chat_completions", "messages", "responses"):
                body = self.request_body(connection(model, protocol, reasoning_effort="high", extra_body={
                    "thinking": {"type": "disabled"}, "reasoning_split": True,
                    "reasoning": {"effort": "none"}, "reasoning_effort": "high",
                    "output_config": {"effort": "high", "format": {"type": "json_schema"}},
                }))
                for field in ("thinking", "reasoning_effort", "reasoning"):
                    self.assertNotIn(field, body)
                self.assertEqual(body["output_config"], {"format": {"type": "json_schema"}})
                self.assertIs(body["reasoning_split"], True)
                self.assertEqual(body["model"], model)
        body = self.request_body(connection("MiniMax-M2.7", reasoning_effort="high", reasoning_config=custom(["high"])))
        self.assertEqual(body["reasoning"]["effort"], "high")

    def test_deepseek_new_connection_preset_uses_current_ids(self):
        self.assertEqual(PROVIDER_PRESETS["deepseek"]["model_id"], "deepseek-flash")
        self.assertTrue(PROVIDER_PRESETS["deepseek"]["model_reasoning"])
        self.assertEqual(PROVIDER_PRESETS["deepseek"]["models"], ["deepseek-flash", "deepseek-v4-pro"])

    def test_custom_mapping_bypasses_automatic_vendor_conversion(self):
        body = self.request_body(connection("MiniMax-M3", reasoning_effort="high",
                                            reasoning_config=custom(["high"], effort_param="reasoning_effort")))
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("thinking", body)
        self.assertNotIn("reasoning", body)
        body = self.request_body(connection("deepseek-flash", "messages", reasoning_effort="none",
                                            reasoning_config=custom(["none"])))
        self.assertEqual(body["output_config"]["effort"], "none")
        self.assertNotIn("thinking", body)
        body = self.request_body(connection("MiniMax-M3", "messages", reasoning_effort="enabled",
                                            reasoning_config=custom(["enabled"], "thinking_toggle")))
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 2048})

    def test_off_removes_vendor_and_extra_controls(self):
        for model in ("MiniMax-M3", "deepseek-flash", "hy3"):
            for protocol in ("chat_completions", "messages", "responses"):
                body = self.request_body(connection(model, protocol, reasoning_config={"mode": "off"},
                    extra_body={"thinking": {"type": "enabled"}, "effort": "high", "reasoning_effort": "high",
                                "reasoning": {"effort": "high"}, "output_config": {"effort": "high", "format": {"type": "json_schema"}}}))
                for field in ("thinking", "effort", "reasoning_effort", "reasoning"):
                    self.assertNotIn(field, body)
                self.assertEqual(body["output_config"], {"format": {"type": "json_schema"}})

    def test_unknown_namespace_does_not_acquire_native_mapping(self):
        body = self.request_body(connection("gateway/deepseek-flash", "messages", reasoning_effort="none"))
        self.assertEqual(body["output_config"]["effort"], "none")
        self.assertNotIn("thinking", body)
        self.assertEqual(body["model"], "gateway/deepseek-flash")

    def test_prefixed_kimi_omits_fixed_sampling_without_renaming(self):
        body = self.request_body(connection("moonshotai/kimi-k3", reasoning_effort="high"))
        self.assertNotIn("temperature", body)
        self.assertEqual(body["model"], "moonshotai/kimi-k3")

    def test_empty_recovery_never_invents_unsupported_hy4_low(self):
        for protocol in ("chat_completions", "messages", "responses"):
            body = {}
            connection("hy4-preview", protocol)._apply_recovery_reasoning(body, protocol)
            self.assertEqual(body, {})


class DeepSeekResponsesContinuationTests(unittest.TestCase):
    @staticmethod
    def fixture_output():
        return [
            {"type": "reasoning", "id": "r1", "content": [{"type": "reasoning_text", "text": "private reasoning fixture"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checking a tool."}]},
            {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": '{"q":"test"}'},
        ]

    def test_real_two_request_tool_loop_preserves_exact_output_order(self):
        output = self.fixture_output()
        requests = []
        c = connection("deepseek-ai/deepseek-flash", reasoning_effort="high", provider_id=7)

        async def handler(request):
            requests.append(json.loads(request.content))
            result = {"output": output, "output_text": "Checking a tool."} if len(requests) == 1 else {
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "final answer"}]}]}
            return httpx.Response(200, json=result)

        async def run():
            messages = [{"role": "user", "content": "question"}]
            tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with patch("backend.llm.client.get_http_client", return_value=http):
                    message = await c.chat_with_tools(messages, tools)
                    self.assertEqual(message["content"], "Checking a tool.")
                    messages.extend([message, {"role": "tool", "tool_call_id": "call-1", "content": "tool result"}])
                    return await c.chat_with_tools(messages, tools)
        self.assertEqual(asyncio.run(run())["content"], "final answer")
        self.assertEqual(requests[1]["input"][1:4], output)
        self.assertEqual(requests[1]["input"][4], {"type": "function_call_output", "call_id": "call-1", "output": "tool result"})
        self.assertEqual(requests[1]["model"], "deepseek-ai/deepseek-flash")

    def test_private_reasoning_never_crosses_connection_model_or_protocol(self):
        c = connection("deepseek-flash", provider_id=7)
        message = c._parse_responses({"output": self.fixture_output()})
        self.assertNotIn("private reasoning fixture", message["content"])
        for other in (connection("deepseek-flash", provider_id=8), connection("deepseek-v4-pro", provider_id=7)):
            self.assertNotIn("private reasoning fixture", json.dumps(other._responses_payload([message])))
        other = connection("deepseek-flash", provider_id=7)
        other.base_url = "https://other.invalid/v1"
        self.assertNotIn("private reasoning fixture", json.dumps(other._responses_payload([message])))
        for body in (c._anthropic_payload([message]), c._body({"messages": [message]}, "chat_completions")):
            self.assertNotIn("private reasoning fixture", json.dumps(body))
            self.assertNotIn("_responses_output", json.dumps(body))
        self.assertIn("_responses_output", message)

    def test_private_items_never_enter_public_projection(self):
        message = connection("deepseek-flash")._parse_responses({"output": self.fixture_output()})
        for event in PUBLIC_PROCESS_EVENTS:
            result = json.dumps(public_process_payload(event, message))
            self.assertNotIn("private reasoning fixture", result)
            self.assertNotIn("_responses_output", result)

    def test_non_deepseek_parser_does_not_enable_this_vendor_continuation(self):
        message = connection("other-reasoner")._parse_responses({"output": self.fixture_output()})
        self.assertNotIn("_responses_output", message)
        self.assertEqual(message["content"], "Checking a tool.")


if __name__ == "__main__":
    unittest.main()
