"""A queue-to-guidance conversion must retain the queued Turn's settings."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import jobs
from backend.api import chat as chat_api
from backend.database import Base
from backend.models import Agent, Item, Job, ModelProvider, Turn, User


class GuidanceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.db = self.factory()
        self.owner = User(username="owner", password_hash="x", role="root", is_active=True)
        self.db.add(self.owner)
        self.db.flush()
        self.provider = ModelProvider(
            name="fixture", model_id="gpt-5.6", provider_type="openai",
            wire_api="responses", model_reasoning=True, reasoning_effort="medium",
            base_url="https://example.invalid/v1", api_key="secret-never-in-conflict",
            enabled=True, created_by=self.owner.id,
        )
        self.db.add(self.provider)
        self.db.flush()
        self.agent = Agent(name="fixture", provider_id=self.provider.id,
                           created_by=self.owner.id, active_version=1, enabled=True)
        self.db.add(self.agent)
        self.db.commit()
        self.snapshot = chat_api.build_execution_snapshot(self.db, self.agent, "fixture")
        for patcher in (
            patch.object(jobs, "SessionLocal", self.factory),
            patch.object(jobs.settings, "JOB_MAX_QUEUED_GLOBAL", 0),
            patch.object(jobs.settings, "JOB_MAX_INFLIGHT_PER_USER", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def payload(self, text="排队文本"):
        return {
            "session_id": "guidance-settings", "agent_id": self.agent.id,
            "harness_version": 1, "inputs": {"query": text},
            "provider_id": None, "reasoning_effort": "", "approval_policy": "ask",
            "execution_snapshot": deepcopy(self.snapshot),
        }

    def pair(self, source_payload=None, target_payload=None):
        target = jobs.enqueue(self.owner.id, self.agent.id, "chat",
                              target_payload if target_payload is not None else self.payload("当前任务"))
        source = jobs.enqueue(self.owner.id, self.agent.id, "chat",
                              source_payload if source_payload is not None else self.payload())
        return source, target

    def assert_retained(self, source, target):
        self.assertEqual(jobs.view(source, self.owner.id).status, jobs.PENDING)
        self.assertFalse(jobs.view(source, self.owner.id).cancel_requested)
        self.assertEqual(jobs.pending_guidance(target, self.owner.id), [])
        self.db.expire_all()
        self.assertEqual(self.db.get(Turn, source).status, "queued")
        self.assertEqual(self.db.query(Item).filter_by(turn_id=source, name="turn.cancelled").count(), 0)

    def assert_conflict(self, source_payload, target_payload=None):
        source, target = self.pair(source_payload, target_payload)
        with self.assertRaisesRegex(RuntimeError, "请保留为排队任务"):
            jobs.convert_queued_message_to_guidance(source, target, self.owner.id)
        self.assert_retained(source, target)

    def test_matching_frozen_settings_convert_and_preserve_audit(self):
        source, target = self.pair()
        original = jobs.view(source, self.owner.id).payload
        result = jobs.convert_queued_message_to_guidance(source, target, self.owner.id)
        self.assertEqual(result["content"], "排队文本")
        self.assertEqual(result["job_id"], target)
        self.assertEqual(jobs.view(source, self.owner.id).status, jobs.CANCELLED)
        self.assertEqual(jobs.view(source, self.owner.id).payload, original)
        self.assertEqual(jobs.pending_guidance(target, self.owner.id)[0]["content"], "排队文本")
        self.db.expire_all()
        self.assertEqual(self.db.get(Turn, source).status, "cancelled")
        event = self.db.query(Item).filter_by(turn_id=source, name="turn.cancelled").one()
        self.assertEqual(json.loads(event.payload)["reason"], "converted_to_guidance")

    def test_each_composer_setting_difference_keeps_queue(self):
        for field, value in (("provider_id", self.provider.id), ("reasoning_effort", "high"),
                             ("approval_policy", "full_access"), ("agent_id", self.agent.id + 1)):
            with self.subTest(field=field):
                payload = self.payload()
                payload[field] = value
                if field == "reasoning_effort":
                    payload["execution_snapshot"]["reasoning_effort"] = value
                    payload["execution_snapshot"]["provider"]["reasoning_effort"] = value
                elif field == "approval_policy":
                    payload["execution_snapshot"]["approval_policy"] = value
                self.assert_conflict(payload)

    def test_inherited_effort_still_rejects_changed_provider_default(self):
        target = self.payload()
        self.provider.reasoning_effort = "high"
        self.db.commit()
        source = self.payload()
        source["execution_snapshot"] = chat_api.build_execution_snapshot(self.db, self.agent, "next")
        self.assertEqual(source["reasoning_effort"], target["reasoning_effort"])
        self.assertEqual(source["provider_id"], target["provider_id"])
        self.assert_conflict(source, target)

    def test_other_agent_with_same_model_cannot_become_guidance(self):
        other = Agent(name="another", provider_id=self.provider.id, created_by=self.owner.id,
                      active_version=1, enabled=True)
        self.db.add(other)
        self.db.commit()
        payload = self.payload()
        payload["agent_id"] = other.id
        payload["execution_snapshot"] = chat_api.build_execution_snapshot(self.db, other, "other")
        target = jobs.enqueue(self.owner.id, self.agent.id, "chat", self.payload("current"))
        source = jobs.enqueue(self.owner.id, other.id, "chat", payload)
        with self.assertRaisesRegex(RuntimeError, "请保留为排队任务"):
            jobs.convert_queued_message_to_guidance(source, target, self.owner.id)
        self.assert_retained(source, target)

    def test_matching_choices_do_not_hide_changed_frozen_execution(self):
        variants = []
        changed_model = self.payload()
        changed_model["execution_snapshot"]["provider"]["model_id"] = "gpt-5.4"
        variants.append(("model", changed_model))
        changed_connection = self.payload()
        changed_connection["execution_snapshot"]["provider"]["id"] += 1
        variants.append(("provider", changed_connection))
        changed_harness = self.payload()
        changed_harness["execution_snapshot"]["harness"]["system_prompt"] = "changed instructions"
        variants.append(("harness", changed_harness))
        changed_fallback = self.payload()
        changed_fallback["execution_snapshot"]["provider_fallbacks"] = [deepcopy(self.snapshot["provider"])]
        variants.append(("fallback", changed_fallback))
        changed_role = self.payload()
        changed_role["execution_snapshot"]["model_roles"]["planner"] = {"provider": deepcopy(self.snapshot["provider"])}
        variants.append(("role", changed_role))
        changed_capability = self.payload()
        changed_capability["execution_snapshot"]["builtin_tools"] = ["fixture-extra-tool"]
        variants.append(("capability", changed_capability))
        for name, payload in variants:
            with self.subTest(name=name):
                self.assert_conflict(payload)

    def test_legacy_missing_request_fields_are_proved_by_frozen_snapshot(self):
        legacy = self.payload()
        for field in ("provider_id", "reasoning_effort", "approval_policy"):
            legacy.pop(field)
        legacy["execution_snapshot"].pop("reasoning_effort")
        source, target = self.pair(legacy)
        self.assertEqual(jobs.convert_queued_message_to_guidance(source, target, self.owner.id)["kind"], "guidance")

    def test_legacy_missing_fields_cannot_mask_different_frozen_provider(self):
        legacy = self.payload()
        legacy.pop("provider_id")
        legacy.pop("reasoning_effort")
        legacy["execution_snapshot"].pop("reasoning_effort")
        legacy["execution_snapshot"]["provider"]["reasoning_effort"] = "high"
        self.assert_conflict(legacy)

    def test_unknown_legacy_settings_never_default_to_equal(self):
        for missing in ("snapshot", "approval", "model_reasoning", "reasoning_effort", "harness", "agent"):
            with self.subTest(missing=missing):
                unknown = self.payload()
                unknown.pop("provider_id")
                unknown.pop("reasoning_effort")
                snapshot = unknown["execution_snapshot"]
                if missing == "snapshot":
                    unknown.pop("execution_snapshot")
                elif missing == "approval":
                    unknown.pop("approval_policy")
                    snapshot.pop("approval_policy")
                elif missing in ("model_reasoning", "reasoning_effort"):
                    snapshot["provider"].pop(missing)
                else:
                    snapshot.pop(missing)
                # Both sides missing the same evidence is still unknown.
                self.assert_conflict(unknown, deepcopy(unknown))

    def test_route_health_diagnostics_do_not_prevent_same_effective_settings(self):
        source = self.payload()
        source["execution_snapshot"]["provider_route"]["health"] = {str(self.provider.id): {"samples": 42}}
        source["execution_snapshot"]["provider_route"]["reason"] = "lowest_cost"
        source_id, target_id = self.pair(source)
        self.assertEqual(jobs.convert_queued_message_to_guidance(source_id, target_id, self.owner.id)["kind"], "guidance")

    def test_guidance_queue_guidance_round_trip_preserves_settings(self):
        target = jobs.enqueue(self.owner.id, self.agent.id, "chat", self.payload("current"))
        guidance = jobs.add_guidance(target, self.owner.id, "round trip")
        queued = jobs.convert_guidance_to_queue(guidance["id"], self.owner.id, job_id=target)
        result = jobs.convert_queued_message_to_guidance(queued["job_id"], target, self.owner.id)
        self.assertEqual(result["content"], "round trip")
        self.assertEqual(jobs.pending_guidance(target, self.owner.id)[0]["id"], result["id"])

    def test_api_returns_safe_conflict_without_mutating_queue(self):
        payload = self.payload()
        payload["execution_snapshot"]["provider"]["api_key"] = "other-private-secret"
        source, target = self.pair(payload)
        request = SimpleNamespace(json=AsyncMock(return_value={"target": "guidance", "target_job_id": target}))
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(chat_api.transform_staged_message("queue", source, request, self.owner))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, jobs._GUIDANCE_SETTINGS_CONFLICT)
        self.assertNotIn("secret", raised.exception.detail)
        self.assert_retained(source, target)

    def test_other_owner_cannot_convert_matching_settings(self):
        source, target = self.pair()
        with self.assertRaises(LookupError):
            jobs.convert_queued_message_to_guidance(source, target, self.owner.id + 1)
        self.assert_retained(source, target)


if __name__ == "__main__":
    unittest.main()
