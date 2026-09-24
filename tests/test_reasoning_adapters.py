"""Model declarations, wire payloads and private tool continuity; no network calls."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from backend.llm.client import LLMClient
from backend.reasoning_options import (
    apply_turn_reasoning, common_reasoning_capabilities, normalize_reasoning_config,
    normalize_turn_reasoning, reasoning_capabilities, validate_reasoning_settings,
)
from backend.runtime.process_view import PUBLIC_PROCESS_EVENTS, public_process_payload


def provider(model="glm-5.3", protocol="responses", **kwargs):
    return {"model_id": model, "wire_api": protocol, "model_reasoning": True,
            "max_tokens": 8192, **kwargs}


def client(model="glm-5.3", protocol="responses", **kwargs):
    return LLMClient(base_url="https://fixture.invalid/v1", api_key="fixture",
                     model_id=model, wire_api=protocol, max_retries=0,
                     stream_max_retries=0, **kwargs)


def custom(values, control="effort", **kwargs):
    return {"mode": "custom", "control": control, "supported_efforts": values, **kwargs}


class ReasoningProfileTests(unittest.TestCase):
    def test_glm_and_kimi_efforts_follow_model_not_protocol(self):
        for model in ("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "kimi-k3"):
            for protocol in ("chat_completions", "responses", "messages"):
                with self.subTest(model=model, protocol=protocol):
                    cap = reasoning_capabilities(provider(model, protocol))
                    self.assertEqual(cap["reasoning_efforts"], ["low", "high", "max"])
                    self.assertEqual(cap["reasoning_control"], "effort")
                    self.assertEqual(set(cap["reasoning_effort_labels"]), {"low", "high", "max"})

    def test_claude_models_have_distinct_effort_sets(self):
        expected = {
            "claude-opus-4-5": ["low", "medium", "high"],
            "claude-opus-4-6": ["low", "medium", "high", "max"],
            "claude-sonnet-4-6": ["low", "medium", "high", "max"],
            "claude-opus-4-7": ["low", "medium", "high", "xhigh", "max"],
            "claude-opus-5": ["low", "medium", "high", "xhigh", "max"],
        }
        for model, efforts in expected.items():
            self.assertEqual(reasoning_capabilities(provider(model, "messages"))["reasoning_efforts"], efforts)

    def test_glm52_aliases_are_not_presented_as_distinct_strengths(self):
        self.assertEqual(reasoning_capabilities(provider("glm-5.2"))["reasoning_efforts"], ["none", "high", "max"])
        validate_reasoning_settings(provider("glm-5.2", reasoning_effort="medium"))

    def test_toggle_values_are_not_effort_levels(self):
        for model in ("glm-5", "glm-5.1", "kimi-k2.6"):
            cap = reasoning_capabilities(provider(model, "chat_completions"))
            self.assertEqual(cap["reasoning_efforts"], ["disabled", "enabled"])
            self.assertEqual(cap["reasoning_control"], "thinking_toggle")
            with self.assertRaises(ValueError):
                apply_turn_reasoning(provider(model, "chat_completions"), "high")
        for model in ("kimi-k2.7-code", "kimi-k2.7-code-highspeed"):
            self.assertFalse(reasoning_capabilities(provider(model))["reasoning_supported"])
        self.assertEqual(normalize_turn_reasoning("enabled"), "enabled")

    def test_unknown_requires_explicit_capability_declaration(self):
        raw = provider("my-gateway-alias", reasoning_effort="high")
        self.assertFalse(reasoning_capabilities(raw)["reasoning_supported"])
        validate_reasoning_settings(raw)  # Legacy defaults keep working.
        raw["reasoning_config"] = json.dumps(custom(["low", "high"]))
        snapshot = apply_turn_reasoning(raw, "low")
        self.assertEqual(snapshot["reasoning_effort"], "low")
        self.assertEqual(raw["reasoning_effort"], "high")
        with self.assertRaises(ValueError):
            apply_turn_reasoning(raw, "max")

    def test_invalid_configuration_cannot_smuggle_paths_or_values(self):
        for value in ([], "[]", "broken", {"mode": None}, {"mode": []},
                      {"effort_param": "headers.Authorization"}, custom([]), custom(["ultra"]),
                      custom(["enabled"]), custom(["high"], "thinking_toggle"),
                      custom(["enabled"], "thinking_toggle", budget_tokens=True),
                      custom(["enabled"], "thinking_toggle", budget_tokens=100),
                      {"mode": "auto", "surprise": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_reasoning_config(value)
        self.assertEqual(normalize_reasoning_config("{}"), {})

    def test_custom_default_and_subscription_validation(self):
        for raw in (
            provider("alias", reasoning_config=custom(["high"]), reasoning_effort="low"),
            provider("alias", reasoning_config=custom(["high"]), model_reasoning=False),
            provider("alias", reasoning_config=custom(["high"]), provider_type="chatgpt"),
            provider("glm-5.3", reasoning_effort="medium"),
        ):
            with self.assertRaises(ValueError):
                validate_reasoning_settings(raw)

    def test_messages_budget_validates_effective_max_and_model(self):
        raw = provider("claude-sonnet-4-5", "messages", reasoning_effort="enabled")
        validate_reasoning_settings(raw)
        for updates in ({"max_tokens": 2048}, {"extra_body": '{"max_tokens":1500}'},
                        {"max_tokens_param": "none"}):
            with self.assertRaises(ValueError):
                validate_reasoning_settings({**raw, **updates})
        with self.assertRaises(ValueError):
            validate_reasoning_settings(provider("alias", "responses", reasoning_config=custom(
                ["enabled"], "thinking_toggle", budget_tokens=2048)))
        with self.assertRaises(ValueError):
            validate_reasoning_settings(provider("claude-opus-5", "messages", reasoning_config=custom(
                ["enabled"], "thinking_toggle")))

    def test_common_choices_intersect_real_semantics(self):
        self.assertEqual(common_reasoning_capabilities([
            provider("glm-5.3"), provider("claude-opus-4-6", "messages"),
        ])["reasoning_efforts"], ["low", "high", "max"])
        cap = common_reasoning_capabilities([provider("glm-5.3"), provider("glm-5", "chat_completions")])
        self.assertEqual(cap["reasoning_efforts"], [])
        self.assertEqual(cap["reasoning_control"], "mixed")
        self.assertFalse(reasoning_capabilities(provider(reasoning_config={"mode": "off"}))["reasoning_supported"])


class ReasoningWireTests(unittest.TestCase):
    def test_standard_protocol_fields_in_actual_http_requests(self):
        for protocol, expected in (("chat_completions", {"reasoning_effort": "high"}),
                                   ("responses", {"reasoning": {"effort": "high"}}),
                                   ("messages", {"output_config": {"effort": "high"}})):
            requests = []

            async def handler(request):
                requests.append(json.loads(request.content))
                output = {"choices": [{"message": {"content": "answer"}}]}
                if protocol == "responses":
                    output = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}]}
                elif protocol == "messages":
                    output = {"content": [{"type": "text", "text": "answer"}]}
                return httpx.Response(200, json=output)

            async def run():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    with patch("backend.llm.client.get_http_client", return_value=http):
                        return await client(protocol=protocol, reasoning_effort="high").chat_with_tools([
                            {"role": "user", "content": "test"}])
            self.assertEqual(asyncio.run(run())["content"], "answer")
            for key, value in expected.items():
                self.assertEqual(requests[0][key], value)
            self.assertNotIn("effort", requests[0])
            self.assertNotIn("thinking", requests[0])

    def test_nested_effort_preserves_extra_options_without_mutating_source(self):
        for protocol, key in (("responses", "reasoning"), ("messages", "output_config")):
            extra = {key: {"format": {"type": "json_schema"}, "effort": "low"},
                     "thinking": {"type": "adaptive", "display": "omitted"}}
            before = copy.deepcopy(extra)
            payload = client(protocol=protocol, reasoning_effort="max", extra_body=extra)._body({"model": "glm-5.3"})
            self.assertEqual(payload[key]["format"], extra[key]["format"])
            self.assertEqual(payload[key]["effort"], "max")
            self.assertEqual(payload["thinking"], extra["thinking"])
            self.assertEqual(extra, before)

    def test_explicit_gateway_parameter_override_is_respected(self):
        for param in ("reasoning_effort", "reasoning.effort", "output_config.effort"):
            connection = client(model="alias", reasoning_effort="medium", reasoning_config=custom(
                ["medium"], effort_param=param))
            body = connection._body({})
            value = body
            for key in param.split("."):
                value = value[key]
            self.assertEqual(value, "medium")
        self.assertNotIn("reasoning", client(reasoning_effort="max", reasoning_config={"mode": "off"})._body({}))

    def test_toggle_and_budget_have_native_parameter_shape(self):
        enabled = client("claude-sonnet-4-5", "messages", reasoning_effort="enabled")._body({})
        self.assertEqual(enabled["thinking"], {"type": "enabled", "budget_tokens": 2048})
        disabled = client("claude-sonnet-4-5", "messages", reasoning_effort="disabled",
                          extra_body={"thinking": {"type": "enabled", "budget_tokens": 3000, "display": "omitted"}})._body({})
        self.assertEqual(disabled["thinking"], {"type": "disabled"})
        native = client("glm-5", "chat_completions", reasoning_effort="enabled")._body({})
        self.assertEqual(native["thinking"], {"type": "enabled"})
        self.assertNotIn("reasoning_effort", native)
        with self.assertRaises(ValueError):
            client("claude-sonnet-4-5", "messages", reasoning_effort="enabled", max_tokens=1024)._body({})

    def test_off_removes_stale_controls_but_keeps_other_output_options(self):
        extra = {"reasoning_effort": "high", "effort": "high", "reasoning": {"effort": "max", "summary": "auto"},
                 "output_config": {"effort": "low", "format": {"type": "json_schema"}},
                 "thinking": {"type": "enabled", "budget_tokens": 2048}}
        body = client(reasoning_config={"mode": "off"}, extra_body=extra)._body({})
        self.assertNotIn("thinking", body)
        self.assertNotIn("effort", body)
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["reasoning"], {"summary": "auto"})
        self.assertEqual(body["output_config"], {"format": {"type": "json_schema"}})
        self.assertEqual(extra["reasoning_effort"], "high")

    def test_kimi_fixed_sampling_is_omitted_and_other_models_unchanged(self):
        for model in ("kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"):
            for protocol in ("chat_completions", "responses", "messages"):
                c = client(model, protocol, extra_body={"temperature": 0.2})
                self.assertFalse(c.supports_temperature)
                self.assertNotIn("temperature", c._body({"temperature": 0.5}))
        self.assertEqual(client()._body({"temperature": 0.5})["temperature"], 0.5)

    def test_custom_choice_survives_empty_response_recovery(self):
        requests = []

        async def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json={"content": [{"type": "text", "text": "recovered"}]})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with patch("backend.llm.client.get_http_client", return_value=http):
                    return await client("alias", "messages", reasoning_effort="enabled",
                                        reasoning_config=custom(["disabled", "enabled"], "thinking_toggle"))._recover_empty_message(
                        [{"role": "user", "content": "answer"}], 0.5, reasoning_chars=20)
        self.assertEqual(asyncio.run(run())["content"], "recovered")
        self.assertEqual(requests[0]["thinking"], {"type": "enabled", "budget_tokens": 2048})
        self.assertNotIn("output_config", requests[0])

    def test_recovery_does_not_invent_values_for_custom_or_extra_controls(self):
        for c, original in (
            (client(reasoning_config=custom(["max"])), {}),
            (client(), {"reasoning": {"effort": "max", "summary": "auto"}}),
            (client("claude-sonnet-4-5", "messages"), {}),
        ):
            body = copy.deepcopy(original)
            c._apply_recovery_reasoning(body, c.wire_api)
            self.assertEqual(body, original)

    def test_stream_recovery_keeps_off_and_custom_inherit_in_http_body(self):
        for config in ({"mode": "off"}, custom(["max"])):
            requests = []

            async def handler(request):
                body = json.loads(request.content)
                requests.append(body)
                if body.get("stream"):
                    event = {"choices": [{"delta": {"reasoning_content": "private fixture"}, "finish_reason": "length"}]}
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")
                return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]})

            async def run():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    with patch("backend.llm.client.get_http_client", return_value=http):
                        return await client("openai/gpt-oss-20b", "chat_completions", reasoning_config=config).chat_messages_stream([
                            {"role": "user", "content": "answer"}])
            self.assertEqual(asyncio.run(run()), "recovered")
            self.assertEqual(len(requests), 2)
            for body in requests:
                self.assertNotIn("reasoning_effort", body)

    def test_claude_tool_response_is_preserved_in_real_followup_request(self):
        requests = []
        blocks = [{"type": "thinking", "thinking": "", "signature": "opaque signed fixture"},
                  {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"q": "test"}}]

        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"content": blocks if len(requests) == 1 else [
                {"type": "text", "text": "final answer"}]})

        async def run():
            c = client("claude-opus-4-6", "messages", reasoning_effort="high")
            messages = [{"role": "user", "content": "question"}]
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with patch("backend.llm.client.get_http_client", return_value=http):
                    messages.append(await c.chat_with_tools(messages, [{"type": "function", "function": {
                        "name": "lookup", "parameters": {"type": "object"}}}]))
                    messages.append({"role": "tool", "tool_call_id": "call-1", "content": "tool result"})
                    return await c.chat_with_tools(messages)
        self.assertEqual(asyncio.run(run())["content"], "final answer")
        self.assertEqual(requests[1]["messages"][1]["content"], blocks)
        self.assertEqual(requests[1]["messages"][2]["content"][0]["tool_use_id"], "call-1")
        self.assertEqual(requests[1]["output_config"]["effort"], "high")

    def test_claude_blocks_round_trip_only_to_same_connection(self):
        c = client("claude-opus-4-6", "messages", provider_id=7)
        blocks = [{"type": "thinking", "thinking": "private fixture", "signature": "signature fixture"},
                  {"type": "redacted_thinking", "data": "encrypted fixture"},
                  {"type": "text", "text": "using a tool"},
                  {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"query": "test"}}]
        message = c._parse_anthropic({"content": blocks})
        messages = [{"role": "user", "content": "question"}, message,
                    {"role": "tool", "tool_call_id": "call-1", "content": "result"}]
        body = c._anthropic_payload(messages)
        self.assertEqual(body["messages"][1]["content"], blocks)
        self.assertEqual(message["content"], "using a tool")
        for other in (client("claude-opus-4-6", "messages", provider_id=8),
                      client("claude-opus-4-7", "messages", provider_id=7)):
            self.assertNotIn("signature fixture", json.dumps(other._anthropic_payload(messages)))
        self.assertNotIn("signature fixture", json.dumps(client()._responses_payload(messages)))
        chat_body = client(protocol="chat_completions")._body({"messages": messages})
        self.assertNotIn("signature fixture", json.dumps(chat_body))
        self.assertIn("_anthropic_content", message)  # Original internal state unchanged.

    def test_private_claude_blocks_never_enter_public_process_projection(self):
        message = client("claude-opus-4-6", "messages")._parse_anthropic({"content": [
            {"type": "thinking", "thinking": "private fixture", "signature": "signature fixture"},
            {"type": "text", "text": "public answer"}]})
        for event in PUBLIC_PROCESS_EVENTS:
            projected = json.dumps(public_process_payload(event, message))
            self.assertNotIn("private fixture", projected)
            self.assertNotIn("signature fixture", projected)
            self.assertNotIn("_anthropic_content", projected)


if __name__ == "__main__":
    unittest.main()
