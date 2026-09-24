"""Reasoning profiles persist, freeze and respect every execution boundary."""
import asyncio
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend import jobs
from backend.api import chat as chat_api, reasoning as reasoning_api, providers as providers_api
from backend.database import Base
from backend.guest_access import guest_agent, personal_model_client
from backend.model_governance import ProviderRoute
from backend.models import Agent, ModelProvider, User
from backend.schemas import ProviderCreate, ProviderUpdate
from backend.security import get_current_user


def profile(*efforts, control="effort", param="auto", **extra):
    return {"mode": "custom", "control": control, "effort_param": param,
            "supported_efforts": list(efforts), **extra}


class ReasoningConfigTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.owner = User(username="root", role="root", password_hash="x", is_active=True)
        self.guest = User(username="guest", role="guest", password_hash="x", is_active=True)
        self.other = User(username="other", role="guest", password_hash="x", is_active=True)
        self.db.add_all([self.owner, self.guest, self.other])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def provider(self, name="custom", *, protocol="responses", config=None, effort="low"):
        result = providers_api.create_provider(ProviderCreate(
            name=name, base_url="https://example.invalid/v1", model_id=name,
            wire_api=protocol, model_reasoning=True, reasoning_effort=effort,
            reasoning_config=config or profile("low", "high"),
        ), self.owner, self.db)
        return self.db.get(ModelProvider, result.id)

    def agent(self, provider, roles=None):
        row = Agent(name="agent", provider_id=provider.id, created_by=self.owner.id,
                    enabled=True, active_version=1,
                    routing=json.dumps({"version": 2, "roles": roles or {}}))
        self.db.add(row)
        self.db.commit()
        return row

    def test_provider_roundtrip_and_name_only_patch_preserve_configuration(self):
        row = self.provider()
        original = row.reasoning_config
        out = providers_api.update_provider(row.id, ProviderUpdate(name="renamed"), self.owner, self.db)
        self.assertEqual(row.reasoning_config, original)
        self.assertEqual(out.reasoning_config, json.loads(original))
        self.assertEqual(out.reasoning_efforts, ["low", "high"])
        self.assertEqual(out.reasoning_effort, "low")
        self.assertNotIn("reasoning_config", json.loads(row.extra_body))

    def test_legacy_unknown_default_is_preserved_on_name_only_patch(self):
        row = ModelProvider(name="legacy", model_id="unknown", model_reasoning=True,
                            reasoning_effort="medium", reasoning_config="{}", created_by=self.owner.id)
        self.db.add(row)
        self.db.commit()
        out = providers_api.update_provider(row.id, ProviderUpdate(name="renamed"), self.owner, self.db)
        self.assertEqual((row.reasoning_config, row.reasoning_effort), ("{}", "medium"))
        self.assertEqual(out.reasoning_effort, "medium")
        self.assertEqual(out.reasoning_efforts, [])

    def test_invalid_custom_profile_or_default_cannot_be_saved(self):
        for config in (profile(), profile("ultra"), profile("low", param="model"),
                       {**profile("low"), "api_key": "not-a-config-field"}):
            with self.subTest(config=config), self.assertRaises(HTTPException):
                self.provider(config=config)
        row = self.provider()
        with self.assertRaises(HTTPException):
            providers_api.update_provider(row.id, ProviderUpdate(reasoning_effort="max"), self.owner, self.db)
        self.assertEqual(row.reasoning_effort, "low")

    def test_patch_checks_merged_budget_before_mutating_provider(self):
        row = self.provider(protocol="messages", config=profile(
            "disabled", "enabled", control="thinking_toggle", budget_tokens=2048), effort="enabled")
        original = row.reasoning_config
        with self.assertRaises(HTTPException):
            providers_api.update_provider(row.id, ProviderUpdate(max_tokens=2048), self.owner, self.db)
        self.assertEqual((row.max_tokens, row.reasoning_config), (8192, original))
        updated = providers_api.update_provider(row.id, ProviderUpdate(
            max_tokens=3072, reasoning_config=profile("disabled", "enabled", control="thinking_toggle", budget_tokens=1024)),
            self.owner, self.db)
        self.assertEqual(updated.max_tokens, 3072)
        self.assertEqual(updated.reasoning_config["budget_tokens"], 1024)

    def test_none_and_toggle_choices_survive_input_schemas(self):
        for value in ("none", "disabled", "enabled"):
            self.assertEqual(ProviderUpdate(reasoning_effort=value).reasoning_effort, value)
            self.assertEqual(reasoning_api.ReasoningCapabilitiesPreview(reasoning_effort=value).reasoning_effort, value)

    def test_preview_is_authenticated_local_and_has_no_credential_inputs(self):
        app = FastAPI()
        app.include_router(reasoning_api.router)
        client = TestClient(app)
        denied = client.post("/api/v1/reasoning-capabilities", json={})
        self.assertEqual(denied.status_code, 401)
        app.dependency_overrides[get_current_user] = lambda: self.owner
        with patch("backend.llm.client.client_for_provider", side_effect=AssertionError("preview must stay local")):
            response = client.post("/api/v1/reasoning-capabilities", json={
                "model_id": "private-alias", "wire_api": "messages", "model_reasoning": True,
                "reasoning_config": profile("low", "high"),
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["reasoning_efforts"], ["low", "high"])
        self.assertIsInstance(response.json()["reasoning_effort_labels"], dict)
        for forbidden in ("api_key", "base_url", "custom_headers", "extra_body"):
            self.assertEqual(client.post("/api/v1/reasoning-capabilities", json={forbidden: "secret"}).status_code, 422)
        self.assertEqual(client.post("/api/v1/reasoning-capabilities", json={
            "model_reasoning": True, "reasoning_config": profile("ultra"),
        }).status_code, 422)

    def test_discovery_probe_receives_configuration_without_persisting(self):
        probe = providers_api.ProviderProbe(base_url="https://example.invalid/v1", model_id="alias",
            model_reasoning=True, reasoning_config=profile("low", "high"))
        captured = []

        class Discover:
            async def list_models(self):
                return ["alias"]

        with patch.object(providers_api, "client_for_provider", side_effect=lambda value: captured.append(value) or Discover()):
            self.assertEqual(asyncio.run(providers_api.discover_models(probe, self.owner))["models"], ["alias"])
        self.assertEqual(json.loads(captured[0].reasoning_config)["supported_efforts"], ["low", "high"])
        self.assertEqual(self.db.query(ModelProvider).count(), 0)

    def test_frozen_custom_mapping_does_not_follow_later_provider_edits(self):
        row = self.provider(config=profile("low", "high", param="reasoning.effort"))
        agent = self.agent(row)
        snapshot = chat_api.build_execution_snapshot(self.db, agent, "hello", reasoning_effort="high")
        row.reasoning_config = json.dumps(profile("low", "high", param="reasoning_effort"))
        self.db.commit()
        client = chat_api._client_from_provider_snapshot(snapshot["provider"])
        body = client._body({"model": "custom", "input": "hello"}, "responses")
        self.assertEqual(body["reasoning"]["effort"], "high")
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("reasoning_config", body)

    def test_off_profile_freezes_no_effective_effort_and_audits_no_strength(self):
        row = self.provider(config={"mode": "off"}, effort="high")
        agent = self.agent(row)
        snapshot = chat_api.build_execution_snapshot(self.db, agent, "hello")
        self.assertEqual(row.reasoning_effort, "high")
        self.assertEqual(snapshot["provider"]["reasoning_effort"], "")
        body = chat_api._client_from_provider_snapshot(snapshot["provider"])._body({}, "responses")
        self.assertNotIn("reasoning", body)
        events = []

        class CapturedRoute(Exception):
            pass

        def capture(name, payload):
            if name == "provider.routed":
                events.append(payload)
                raise CapturedRoute()

        with patch("backend.guardrail_policies.guard_model_client", side_effect=lambda client, **_: client):
            with self.assertRaises(CapturedRoute):
                asyncio.run(chat_api.execute_chat(self.db, agent, SimpleNamespace(query="hello"),
                    user_id=self.owner.id, execution_snapshot=snapshot, runtime_event=capture))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reasoning_effort"], "")
        self.assertNotIn("high", json.dumps(chat_api._public_process_payload("provider.routed", events[0])))

    def test_fixed_minimax_profile_freezes_no_fake_effort_but_keeps_stored_default(self):
        row = self.provider("minimax/MiniMax-M2.7", config={"mode": "auto"}, effort="high")
        agent = self.agent(row)
        snapshot = chat_api.build_execution_snapshot(self.db, agent, "hello")
        self.assertEqual(row.reasoning_effort, "high")
        self.assertEqual(snapshot["provider"]["reasoning_effort"], "")
        self.assertEqual(snapshot["provider"]["model_id"], "minimax/MiniMax-M2.7")
        catalog = chat_api.chat_models(agent.id, self.owner, self.db)
        self.assertEqual(catalog["items"][0]["reasoning_efforts"], [])
        self.assertEqual(catalog["items"][0]["reasoning_effort"], "")
        events = []

        class CapturedRoute(Exception):
            pass

        def capture(name, payload):
            if name == "provider.routed":
                events.append(payload)
                raise CapturedRoute()

        with patch("backend.guardrail_policies.guard_model_client", side_effect=lambda client, **_: client):
            with self.assertRaises(CapturedRoute):
                asyncio.run(chat_api.execute_chat(self.db, agent, SimpleNamespace(query="hello"),
                    user_id=self.owner.id, execution_snapshot=snapshot, runtime_event=capture))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reasoning_effort"], "")
        self.assertNotIn("high", json.dumps(chat_api._public_process_payload("provider.routed", events[0])))

    def test_heterogeneous_fallbacks_compile_their_own_frozen_wire_mapping(self):
        first = self.provider("primary")
        fallback = self.provider("backup", protocol="messages")
        agent = self.agent(first)
        snapshot = chat_api.build_execution_snapshot(self.db, agent, "hello", reasoning_effort="high",
            provider_route=ProviderRoute(primary=first, fallbacks=[fallback], reason="test"))
        primary_body = chat_api._client_from_provider_snapshot(snapshot["provider"])._body({}, "responses")
        backup_body = chat_api._client_from_provider_snapshot(snapshot["provider_fallbacks"][0])._body({}, "messages")
        self.assertEqual(primary_body["reasoning"]["effort"], "high")
        self.assertEqual(backup_body["output_config"]["effort"], "high")
        self.assertNotIn("reasoning", backup_body)
        fallback.reasoning_config = json.dumps(profile("low"))
        self.db.commit()
        with self.assertRaises(HTTPException):
            chat_api.build_execution_snapshot(self.db, agent, "hello", reasoning_effort="high",
                provider_route=ProviderRoute(primary=first, fallbacks=[fallback], reason="test"))

    def test_roles_freeze_own_profiles_and_respect_effective_budget(self):
        first = self.provider("primary")
        role = self.provider("role", protocol="messages", config=profile(
            "disabled", "enabled", control="thinking_toggle", budget_tokens=2048), effort="disabled")
        agent = self.agent(first, {"planner": {"provider_id": role.id, "reasoning_effort": "enabled", "max_tokens": 4096},
                                   "router": {"reasoning_effort": "low"}})
        snapshot = chat_api.build_execution_snapshot(self.db, agent, "hello", reasoning_effort="high")
        self.assertEqual(snapshot["model_roles"]["router"]["provider"]["reasoning_effort"], "low")
        frozen = snapshot["model_roles"]["planner"]["provider"]
        body = chat_api._client_from_provider_snapshot(frozen)._body({}, "messages")
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 2048})
        self.assertEqual(body["max_tokens"], 4096)
        agent.routing = json.dumps({"version": 2, "roles": {"planner": {
            "provider_id": role.id, "reasoning_effort": "enabled", "max_tokens": 2048}}})
        self.db.commit()
        with self.assertRaises(HTTPException):
            chat_api.build_execution_snapshot(self.db, agent, "hello")




    def test_guidance_accepts_identical_toggle_but_rejects_changed_frozen_profile(self):
        row = self.provider(protocol="messages", config=profile(
            "disabled", "enabled", control="thinking_toggle", budget_tokens=2048), effort="enabled")
        agent = self.agent(row)
        snapshot = chat_api.build_execution_snapshot(self.db, agent, "hello", reasoning_effort="enabled")
        payload = {"agent_id": agent.id, "reasoning_effort": "enabled", "provider_id": None,
                   "approval_policy": "ask", "execution_snapshot": snapshot}
        source = SimpleNamespace(agent_id=agent.id)
        target = SimpleNamespace(agent_id=agent.id, payload=json.dumps(payload))
        jobs._require_same_guidance_settings(source, target, payload)
        changed = deepcopy(payload)
        changed["execution_snapshot"]["provider"]["reasoning_config"] = json.dumps(profile(
            "disabled", "enabled", control="thinking_toggle", budget_tokens=3072))
        with self.assertRaisesRegex(RuntimeError, "保留为排队"):
            jobs._require_same_guidance_settings(source, target, changed)


class ReasoningConfigMigrationTests(unittest.TestCase):
    def test_upgrade_is_idempotent_and_preserves_existing_provider_columns(self):
        engine = create_engine("sqlite:///:memory:")
        migration = importlib.import_module("migrations.versions.0027_reasoning_config")
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE model_providers (id INTEGER PRIMARY KEY, name TEXT, api_key TEXT)"))
                connection.execute(text("INSERT INTO model_providers VALUES (7, 'keep', 'ciphertext-unchanged')"))
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    migration.upgrade()
                row = connection.execute(text("SELECT * FROM model_providers")).mappings().one()
                self.assertEqual(dict(row), {"id": 7, "name": "keep", "api_key": "ciphertext-unchanged", "reasoning_config": "{}"})
                self.assertEqual(len(inspect(connection).get_columns("model_providers")), 4)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
