import json
import unittest

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.model_governance import (
    GovernedLLMClient, append_price, estimate_cost, resolve_route,
)
from backend.api.chat import _validate_invocations, create_project
from backend.models import Agent, McpServer, ModelProvider, Project, Skill, User
from backend.resource_governance import (
    public_lifecycle, record_version, resource_state, update_resource_state,
)
from backend.schemas import ProjectCreate, ThreadMemoryUpdate


class PlatformGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="root", password_hash="x", role="root", is_active=True)
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def provider(self, name, model, modalities=("text",)):
        row = ModelProvider(
            name=name, model_id=model, model_input=json.dumps(list(modalities)),
            enabled=True, created_by=self.user.id,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_versioned_price_cost_and_lowest_cost_route(self):
        expensive = self.provider("expensive", "model-a")
        cheap = self.provider("cheap", "model-b")
        append_price(
            self.db, provider_id=expensive.id, model=expensive.model_id,
            input_usd_per_million="10", output_usd_per_million="20",
            created_by=self.user.id,
        )
        append_price(
            self.db, provider_id=cheap.id, model=cheap.model_id,
            input_usd_per_million="1", output_usd_per_million="2",
            created_by=self.user.id,
        )
        cost = estimate_cost(
            self.db, provider_id=expensive.id, model=expensive.model_id,
            input_tokens=1_000_000, output_tokens=500_000,
        )
        self.assertEqual(cost["status"], "estimated")
        self.assertEqual(cost["usd"], "20")
        agent = Agent(
            name="router", provider_id=expensive.id,
            routing=json.dumps({
                "mode": "policy", "default_provider_id": expensive.id,
                "fallback_provider_ids": [cheap.id], "strategy": "lowest_cost",
                "health": {"enabled": False},
            }),
        )
        self.db.add(agent)
        self.db.commit()
        route = resolve_route(self.db, agent, "hello")
        self.assertEqual(route.primary.id, cheap.id)
        self.assertEqual([row.id for row in route.fallbacks], [expensive.id])

    def test_resource_versions_are_append_only_and_review_state_is_explicit(self):
        skill = Skill(name="s", instructions="one", created_by=self.user.id)
        self.db.add(skill)
        self.db.flush()
        first = record_version(
            self.db, "skill", skill.id, {"instructions_sha256": "one"},
            actor_id=self.user.id, change="created",
        )
        second = record_version(
            self.db, "skill", skill.id, {"instructions_sha256": "two"},
            actor_id=self.user.id, change="updated",
        )
        self.db.commit()
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(public_lifecycle(self.db, "skill", skill.id)["version"], 2)
        update_resource_state(
            self.db, "mcp", 7, catalog_hash="abc", review_required=True
        )
        self.db.commit()
        self.assertTrue(resource_state(self.db, "mcp", 7)["review_required"])

    def test_memory_patch_rejects_explicit_null(self):
        with self.assertRaises(ValidationError):
            ThreadMemoryUpdate.model_validate({"enabled": None})

    def test_new_project_can_explicitly_replace_default(self):
        first = create_project(ProjectCreate(name="first"), self.user, self.db)
        second = create_project(
            ProjectCreate(name="second", default=True), self.user, self.db
        )
        self.assertTrue(first["default"])
        self.assertTrue(second["default"])
        defaults = self.db.query(Project).filter(Project.is_default.is_(True)).all()
        self.assertEqual([row.id for row in defaults], [second["id"]])

    def test_mcp_catalog_change_blocks_explicit_invocation_until_reviewed(self):
        server = McpServer(
            name="review-me", url="http://example.invalid/mcp", enabled=True,
            is_public=True, created_by=self.user.id,
        )
        self.db.add(server)
        self.db.flush()
        agent = Agent(name="agent", mcp_ids=json.dumps([server.id]), enabled=True)
        self.db.add(agent)
        update_resource_state(
            self.db, "mcp", server.id, catalog_hash="changed", review_required=True
        )
        self.db.commit()

        with self.assertRaisesRegex(Exception, "需管理员复核") as raised:
            _validate_invocations(
                self.db, self.user, agent, [], [server.id], []
            )
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)


class GovernedClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_observability_failure_does_not_repeat_successful_call(self):
        class Client:
            calls = 0

            async def chat(self):
                self.calls += 1
                return "ok"

        first, second = Client(), Client()

        async def broken_event(*_args):
            raise RuntimeError("event store unavailable")

        client = GovernedLLMClient(
            [(1, "a", first), (2, "b", second)], runtime_event=broken_event
        )
        self.assertEqual(await client.chat(), "ok")
        self.assertEqual((first.calls, second.calls), (1, 0))

    async def test_stream_failure_after_delta_never_falls_back(self):
        class StreamingClient:
            def __init__(self, fail=False):
                self.fail = fail
                self.calls = 0

            async def chat_messages_stream(self, _messages, on_delta=None):
                self.calls += 1
                if self.fail:
                    on_delta("visible")
                    raise RuntimeError("failed after output")
                return "fallback"

        first, second = StreamingClient(True), StreamingClient(False)
        client = GovernedLLMClient([(1, "a", first), (2, "b", second)])
        with self.assertRaisesRegex(RuntimeError, "failed after output"):
            await client.chat_messages_stream([], on_delta=lambda _value: None)
        self.assertEqual((first.calls, second.calls), (1, 0))


if __name__ == "__main__":
    unittest.main()
