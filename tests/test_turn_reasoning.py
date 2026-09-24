"""Per-turn reasoning choices are capability-bound and frozen, never cosmetic."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import FormData

from backend import jobs
from backend.api import chat as chat_api
from backend.database import Base
from backend.model_governance import ProviderRoute
from backend.models import Agent, Job, ModelProvider, User
from backend.reasoning_options import reasoning_capabilities


class FormRequest:
    headers = {}

    def __init__(self, values):
        self.values = FormData(values)

    async def form(self):
        return self.values


class RecordingHttp:
    def __init__(self):
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs["json"]))
        output = ({"output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "测试答复"},
        ]}]} if url.endswith("/responses") else {
            "choices": [{"message": {"role": "assistant", "content": "测试答复"}}],
        })
        return httpx.Response(200, json=output, request=httpx.Request("POST", url))


class TurnReasoningTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.db = self.factory()
        self.owner = User(username="root", password_hash="x", role="root", is_active=True)
        self.user = User(username="user", password_hash="x", role="user", is_active=True)
        self.db.add_all([self.owner, self.user])
        self.db.commit()
        self.provider = self.add_provider("main", "gpt-5.6-sol")
        self.agent = Agent(name="assistant", provider_id=self.provider.id, created_by=self.owner.id,
                           enabled=True, is_public=True, active_version=1)
        self.db.add(self.agent)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_provider(self, name, model, *, public=True, protocol="responses", reasoning=True, kind="openai"):
        provider = ModelProvider(name=name, model_id=model, model_reasoning=reasoning,
                                 provider_type=kind, wire_api=protocol, reasoning_effort="low",
                                 base_url="https://example.invalid/v1", enabled=True, is_public=public,
                                 created_by=self.owner.id, max_retries=0)
        self.db.add(provider)
        self.db.commit()
        return provider

    def test_catalog_preserves_acl_and_reports_verified_efforts_without_ultra(self):
        secret = self.add_provider("secret", "gpt-6-astra", public=False)
        unknown = self.add_provider("custom", "unverified-reasoner")
        subscription = self.add_provider("subscription", "gpt-6-astra", kind="chatgpt")
        messages = self.add_provider("messages", "gpt-5.6-sol", protocol="messages")
        catalog = chat_api.chat_models(self.agent.id, self.user, self.db)
        items = {item["provider_id"]: item for item in catalog["items"]}
        self.assertNotIn(secret.id, items)
        self.assertEqual(items[self.provider.id]["reasoning_efforts"], ["none", "low", "medium", "high", "xhigh", "max"])
        self.assertTrue(items[self.provider.id]["reasoning_supported"])
        for provider in (unknown, subscription, messages):
            self.assertEqual(items[provider.id]["reasoning_efforts"], [])
            self.assertFalse(items[provider.id]["reasoning_supported"])
            self.assertTrue(items[provider.id]["reasoning_unavailable_reason"])
        self.assertNotIn("ultra", json.dumps(catalog))
        self.assertNotIn("api_key", json.dumps(catalog))

    def test_reasoning_flag_alone_and_unknown_alias_do_not_invent_options(self):
        for model, protocol, flag in [("future-model", "responses", True),
                                      ("gpt-5.6-sol-alias", "responses", True),
                                      ("gpt-5.6-sol", "responses", False),
                                      ("gpt-6-astra", "chat_completions", True)]:
            provider = {"model_id": model, "wire_api": protocol, "model_reasoning": flag}
            self.assertEqual(reasoning_capabilities(provider)["reasoning_efforts"], [])
        unverified_snapshot = {"model_id": "gpt-5.6-sol-2026-09-01", "wire_api": "responses", "model_reasoning": True}
        self.assertEqual(reasoning_capabilities(unverified_snapshot)["reasoning_efforts"], [])

    def test_default_catalog_intersects_rules_and_fallback_capabilities(self):
        lower = self.add_provider("lower", "gpt-5.4")
        self.agent.routing = json.dumps({"mode": "policy", "fallback_provider_ids": [lower.id]})
        self.db.commit()
        available = chat_api.chat_models(self.agent.id, self.user, self.db)["default"]["reasoning_efforts"]
        self.assertEqual(available, ["none", "low", "medium", "high", "xhigh"])
        unknown = self.add_provider("unknown", "unverified")
        self.agent.routing = json.dumps({"mode": "rules", "rules": [
            {"match": "keyword", "value": "任务", "provider_id": unknown.id},
        ]})
        self.db.commit()
        self.assertFalse(chat_api.chat_models(self.agent.id, self.user, self.db)["default"]["reasoning_supported"])

    def test_override_is_frozen_and_does_not_modify_provider_or_approval_defaults(self):
        snapshot = chat_api.build_execution_snapshot(self.db, self.agent, "hello", reasoning_effort="high")
        self.assertEqual(snapshot["reasoning_effort"], "high")
        self.assertEqual(snapshot["provider"]["reasoning_effort"], "high")
        self.assertEqual(snapshot["approval_policy"], "ask")
        self.assertEqual(self.provider.reasoning_effort, "low")
        self.provider.reasoning_effort = "medium"
        self.db.commit()
        client = chat_api._client_from_execution_snapshot(snapshot)
        self.assertEqual(client.reasoning_effort, "high")

    def test_empty_override_keeps_existing_provider_setting(self):
        snapshot = chat_api.build_execution_snapshot(self.db, self.agent, "hello")
        self.assertEqual(snapshot["reasoning_effort"], "")
        self.assertEqual(snapshot["provider"]["reasoning_effort"], "low")

    def test_snapshot_override_does_not_replace_explicit_role_settings(self):
        dedicated = self.add_provider("critic", "gpt-5.4")
        self.agent.routing = json.dumps({"version": 2, "roles": {
            "planner": {"provider_id": None, "reasoning_effort": "medium"},
            "router": {"provider_id": None, "max_tokens": 512},
            "critic": {"provider_id": dedicated.id},
        }})
        self.db.commit()
        snapshot = chat_api.build_execution_snapshot(self.db, self.agent, "hello", reasoning_effort="high")
        self.assertEqual(snapshot["model_roles"]["planner"]["provider"]["reasoning_effort"], "medium")
        self.assertEqual(snapshot["model_roles"]["router"]["provider"]["reasoning_effort"], "high")
        self.assertEqual(snapshot["model_roles"]["critic"]["provider"]["reasoning_effort"], "low")

    def test_child_agent_keeps_its_own_executor_defaults(self):
        child = Agent(name="child", provider_id=self.provider.id, created_by=self.owner.id, enabled=True)
        self.db.add(child)
        self.db.flush()
        self.agent.agent_ids = json.dumps([child.id])
        self.db.commit()
        snapshot = chat_api.build_execution_snapshot(self.db, self.agent, "hello", reasoning_effort="high")
        self.assertEqual(snapshot["provider"]["reasoning_effort"], "high")
        self.assertEqual(snapshot["sub_agents"][0]["provider"]["reasoning_effort"], "low")

    def test_every_fallback_must_support_requested_effort(self):
        fallback = self.add_provider("fallback", "gpt-5.4")
        route = ProviderRoute(primary=self.provider, fallbacks=[fallback])
        snapshot = chat_api.build_execution_snapshot(self.db, self.agent, "hello", provider_route=route, reasoning_effort="high")
        self.assertEqual(snapshot["provider_fallbacks"][0]["reasoning_effort"], "high")
        with self.assertRaises(HTTPException) as raised:
            chat_api.build_execution_snapshot(self.db, self.agent, "hello", provider_route=route, reasoning_effort="max")
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("max", raised.exception.detail)

    def test_unsupported_efforts_and_clients_are_rejected_instead_of_ignored(self):
        for effort in ("ultra", "Ultra", "unknown"):
            with self.subTest(effort=effort), self.assertRaises(HTTPException):
                chat_api.build_execution_snapshot(self.db, self.agent, "hello", reasoning_effort=effort)
        astra = self.add_provider("astra", "gpt-6-astra")
        with self.assertRaises(HTTPException):
            chat_api.build_execution_snapshot(self.db, self.agent, "hello", provider=astra, reasoning_effort="none")
        subscription = self.add_provider("sub", "gpt-6-astra", kind="chatgpt")
        with self.assertRaises(HTTPException) as raised:
            chat_api.build_execution_snapshot(self.db, self.agent, "hello", provider=subscription, reasoning_effort="high")
        self.assertIn("订阅", raised.exception.detail)
        self.assertEqual(chat_api.build_execution_snapshot(self.db, self.agent, "hello", provider=subscription)["reasoning_effort"], "")

    def test_override_reaches_real_client_http_payload_for_both_supported_protocols(self):
        for protocol in ("responses", "chat_completions"):
            with self.subTest(protocol=protocol):
                self.provider.wire_api = protocol
                self.db.commit()
                snapshot = chat_api.build_execution_snapshot(self.db, self.agent, "hello", reasoning_effort="high")
                client = chat_api._client_from_execution_snapshot(snapshot)
                fake = RecordingHttp()
                with patch("backend.llm.client.get_http_client", return_value=fake), \
                     patch("backend.llm.client.LLMClient._guard", new_callable=AsyncMock):
                    reply = asyncio.run(client.chat_with_tools([{"role": "user", "content": "hello"}]))
                self.assertEqual(reply["content"], "测试答复")
                self.assertEqual(len(fake.requests), 1)
                payload = fake.requests[0][1]
                self.assertEqual(payload.get("reasoning", {}).get("effort") if protocol == "responses" else payload.get("reasoning_effort"), "high")

    def test_real_governed_client_fallback_sends_same_effort(self):
        class FailingPrimaryHttp(RecordingHttp):
            async def post(self, url, **kwargs):
                if "fail.invalid" in url:
                    self.requests.append((url, kwargs["json"]))
                    return httpx.Response(500, json={"error": {"message": "fixture unavailable"}},
                                          request=httpx.Request("POST", url))
                return await super().post(url, **kwargs)

        self.provider.base_url = "https://fail.invalid/v1"
        fallback = self.add_provider("fallback-http", "gpt-5.4")
        self.db.commit()
        snapshot = chat_api.build_execution_snapshot(
            self.db, self.agent, "hello", reasoning_effort="high",
            provider_route=ProviderRoute(primary=self.provider, fallbacks=[fallback]),
        )
        client = chat_api._client_from_execution_snapshot(snapshot)
        fake = FailingPrimaryHttp()
        with patch("backend.llm.client.get_http_client", return_value=fake), \
             patch("backend.llm.client.LLMClient._guard", new_callable=AsyncMock):
            result = asyncio.run(client.chat_with_tools([{"role": "user", "content": "hello"}]))
        self.assertEqual(result["content"], "测试答复")
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual([payload["reasoning"]["effort"] for _, payload in fake.requests], ["high", "high"])

    def submit(self, effort, provider_id=None, approval="ask", owner=None):
        values = [("agent_id", str(self.agent.id)), ("query", "你好"), ("approval_policy", approval)]
        if effort is not None:
            values.append(("reasoning_effort", effort))
        if provider_id is not None:
            values.append(("provider_id", str(provider_id)))
        with patch.object(jobs, "SessionLocal", self.factory), patch.object(chat_api, "enforce"):
            return asyncio.run(chat_api.chat(FormRequest(values), owner or self.owner, self.db))

    def test_chat_enqueue_and_restore_keep_requested_controls(self):
        response = self.submit("high", self.provider.id)
        self.assertEqual(response["provider_id"], self.provider.id)
        self.assertEqual(response["reasoning_effort"], "high")
        self.assertEqual(response["approval_policy"], "ask")
        payload = json.loads(self.db.get(Job, response["turn_id"]).payload)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["execution_snapshot"]["provider"]["reasoning_effort"], "high")
        active = chat_api.active_chat_jobs(self.owner, self.db)["items"][0]
        self.assertEqual(active["agent_id"], self.agent.id)
        self.assertEqual(active["provider_id"], self.provider.id)
        self.assertEqual(active["reasoning_effort"], "high")
        self.assertEqual(active["approval_policy"], "ask")

    def test_chat_rejects_private_model_and_nonroot_full_access_before_enqueue(self):
        private = self.add_provider("private", "gpt-5.6-sol", public=False)
        for provider_id, approval in ((private.id, "ask"), (self.provider.id, "full_access")):
            with self.subTest(provider_id=provider_id, approval=approval), self.assertRaises(HTTPException):
                self.submit("high", provider_id, approval=approval, owner=self.user)
        self.assertEqual(self.db.query(Job).count(), 0)

    def test_root_can_choose_another_owners_private_model_with_verified_effort(self):
        private = self.add_provider("private-astra", "gpt-6-astra", public=False)
        private.created_by = self.user.id
        self.db.commit()
        catalog = chat_api.chat_models(self.agent.id, self.owner, self.db)
        item = next(item for item in catalog["items"] if item["provider_id"] == private.id)
        self.assertEqual(item["reasoning_efforts"], ["low", "medium", "high", "xhigh", "max"])
        self.assertNotIn("api_key", json.dumps(catalog))
        response = self.submit("max", private.id, owner=self.owner)
        payload = json.loads(self.db.get(Job, response["turn_id"]).payload)
        self.assertEqual(payload["execution_snapshot"]["provider"]["id"], private.id)
        self.assertEqual(payload["execution_snapshot"]["provider"]["reasoning_effort"], "max")
        self.assertEqual(payload["execution_snapshot"]["provider_fallbacks"], [])
        self.assertFalse(private.is_public)

    def test_private_provider_creator_can_list_and_enqueue_own_model(self):
        self.user.role = "admin"
        private = self.add_provider("creator-private", "gpt-5.6-sol", public=False)
        private.created_by = self.user.id
        self.db.commit()
        catalog = chat_api.chat_models(self.agent.id, self.user, self.db)
        self.assertIn(private.id, [item["provider_id"] for item in catalog["items"]])
        response = self.submit("high", private.id, owner=self.user)
        self.assertEqual(response["provider_id"], private.id)
        payload = json.loads(self.db.get(Job, response["turn_id"]).payload)
        self.assertEqual(payload["execution_snapshot"]["provider"]["id"], private.id)
        self.assertFalse(private.is_public)

    def test_nonowner_user_and_admin_cannot_discover_or_select_private_provider(self):
        private = self.add_provider("other-private", "gpt-5.6-sol", public=False)
        for role in ("user", "admin"):
            with self.subTest(role=role):
                self.user.role = role
                self.db.commit()
                catalog = chat_api.chat_models(self.agent.id, self.user, self.db)
                self.assertNotIn(private.id, [item["provider_id"] for item in catalog["items"]])
                with self.assertRaisesRegex(ValueError, "未开放"):
                    chat_api.select_chat_provider_route(self.db, self.user, self.agent, private.id)
                with self.assertRaises(HTTPException):
                    self.submit("high", private.id, owner=self.user)
        self.assertEqual(self.db.query(Job).count(), 0)
        self.assertFalse(private.is_public)

    def test_chat_rejects_unsupported_automatic_route_override_before_enqueue(self):
        fallback = self.add_provider("unsupported-fallback", "gpt-5.4")
        self.agent.routing = json.dumps({"mode": "policy", "fallback_provider_ids": [fallback.id]})
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            self.submit("max")
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(self.db.query(Job).count(), 0)


if __name__ == "__main__":
    unittest.main()
