"""Stage-model contracts: permissions, frozen configuration and actual loop use.

All model clients are local fixtures; these tests never call a paid provider.
"""
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.agents import _validate_routing_refs, agent_model_options, create_agent, update_agent
from backend.api.chat import _role_clients_from_execution_snapshot, build_execution_snapshot
from backend.database import Base
from backend.guardrail_policies import ContentBlocked
from backend.model_roles import normalize_execution_options, parse_tool_selection
from backend.models import Agent, ModelProvider, User
from backend.runtime import builtin_tools, run_harness
from backend.runtime.orchestrator import CompletionVerificationError
from backend.runtime.process_view import is_public_process_event, public_process_payload
from backend.schemas import AgentCreate, AgentUpdate


def message(content="已完成答复", *, name=None, arguments=None):
    return {"role": "assistant", "content": content, "tool_calls": ([{
        "id": "selection", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False),
        },
    }] if name else [])}


class Client:
    context_tokens = 8192

    def __init__(self, response=None, provider_id=7):
        self.response = response or message()
        self.provider_id = provider_id
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(([], None))
        return self.response["content"]

    async def chat_with_tools(self, messages, tools=None):
        self.calls.append((list(messages), list(tools or [])))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ModelRoleConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.root = User(username="root", password_hash="x", role="root", is_active=True)
        self.admin = User(username="admin", password_hash="x", role="admin", is_active=True)
        self.db.add_all([self.root, self.admin])
        self.db.commit()
        self.main = self.provider("main", self.admin)
        self.role = self.provider("role", self.root)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def provider(self, name, owner):
        value = ModelProvider(name=name, model_id=name, base_url="https://example.invalid/v1",
                              enabled=True, created_by=owner.id, model_reasoning=True,
                              reasoning_config=json.dumps({"mode": "custom", "control": "effort",
                                  "effort_param": "auto", "supported_efforts": ["minimal", "low", "medium", "high"]}),
                              reasoning_effort="low", max_tokens=8192)
        self.db.add(value)
        self.db.commit()
        return value

    def routing(self, provider_id):
        return {"version": 2, "mode": "fixed", "roles": {
            "planner": {"provider_id": provider_id, "reasoning_effort": "high", "max_tokens": 512},
        }, "planning": {"mode": "always"}}

    def test_create_rejects_unauthorized_role_and_legacy_rule_before_insert(self):
        for routing in (self.routing(self.role.id), {"mode": "rules", "rules": [
                {"match": "keyword", "value": "a", "provider_id": self.role.id}]}):
            with self.subTest(routing=routing):
                with self.assertRaises(HTTPException) as raised:
                    create_agent(AgentCreate(name="bad", routing=routing), self.admin, self.db)
                self.assertEqual(raised.exception.status_code, 403)
                self.assertEqual(self.db.query(Agent).count(), 0)

    def test_fixed_role_contract_roundtrips_creation_and_update(self):
        created = create_agent(AgentCreate(name="valid", provider_id=self.main.id,
                              routing=self.routing(self.role.id)), self.root, self.db)
        self.assertEqual(created.routing["roles"]["planner"]["provider_id"], self.role.id)
        updated = update_agent(created.id, AgentUpdate(routing={
            **created.routing, "planning": {"mode": "off"}, "review": {"mode": "off"},
        }), self.root, self.db)
        self.assertEqual(updated.routing["planning"]["mode"], "off")
        self.assertEqual(updated.routing["roles"], created.routing["roles"])
        self.assertEqual(updated.routing["tool_routing"]["mode"], "deterministic")

    def test_disabled_role_provider_rejected_on_save(self):
        self.role.enabled = False
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            _validate_routing_refs(self.db, self.root, self.routing(self.role.id))
        self.assertEqual(raised.exception.status_code, 400)

    def test_model_options_expose_only_authorized_minimal_catalog(self):
        public = self.provider("shared", self.root)
        public.is_public = True
        disabled = self.provider("disabled", self.admin)
        disabled.enabled = False
        self.db.commit()
        rows = agent_model_options(self.admin, self.db)
        self.assertEqual({row["id"] for row in rows}, {self.main.id, public.id})
        self.assertTrue(all(set(row) == {"id", "name", "model_id", "enabled",
            "reasoning_efforts", "reasoning_effort", "reasoning_supported", "reasoning_unavailable_reason",
            "reasoning_control", "reasoning_effort_labels"} for row in rows))

    def test_v2_mode_changes_preserve_advanced_policy_and_extensions(self):
        routing = {**self.routing(self.role.id), "fallback_provider_ids": [self.main.id],
                   "default_provider_id": self.role.id, "strategy": "lowest_cost",
                   "rules": [{"match": "requires_image", "value": "", "provider_id": self.main.id}],
                   "health": {"enabled": False, "future_extension": {"window": "daily"}},
                   "extension": {"revision": 7}}
        for mode in ("fixed", "rules", "policy"):
            clean = _validate_routing_refs(self.db, self.root, {**routing, "mode": mode})
            for field in ("rules", "fallback_provider_ids", "strategy", "extension"):
                self.assertEqual(clean[field], routing[field])
            self.assertEqual(clean["health"]["future_extension"], routing["health"]["future_extension"])
            self.assertEqual(clean["mode"], mode)
        with self.assertRaises(HTTPException):
            _validate_routing_refs(self.db, self.admin, {**routing, "mode": "fixed", "roles": {}})

    def test_legacy_rule_count_and_long_keyword_are_preserved_on_v2_save(self):
        rules = [{"match": "keyword", "value": ("关键词" * 120) + str(index), "provider_id": self.main.id}
                 for index in range(40)]
        for mode in ("fixed", "rules", "policy"):
            clean = _validate_routing_refs(self.db, self.root, {"version": 2, "mode": mode, "rules": rules})
            self.assertEqual(clean["rules"], rules)

    def test_role_parameters_reach_actual_client(self):
        agent = Agent(name="real-client-parameters", provider_id=self.main.id, created_by=self.root.id,
                      routing=json.dumps(self.routing(self.role.id)))
        self.db.add(agent)
        self.db.commit()
        frozen = build_execution_snapshot(self.db, agent, "hello")
        client = _role_clients_from_execution_snapshot(frozen, self.db)["planner"]
        self.assertEqual(client.reasoning_effort, "high")
        self.assertEqual(client.max_output_tokens, 512)

    def test_unsupported_client_overrides_rejected_explicit_and_inherited(self):
        self.role.provider_type = "chatgpt"
        self.db.commit()
        with self.assertRaises(HTTPException) as explicit:
            _validate_routing_refs(self.db, self.root, self.routing(self.role.id))
        self.assertEqual(explicit.exception.status_code, 400)
        self.assertIn("ChatGPT", explicit.exception.detail)
        with self.assertRaises(HTTPException) as inherited:
            create_agent(AgentCreate(name="unsupported", provider_id=self.role.id,
                                    routing=self.routing(None)), self.root, self.db)
        self.assertEqual(inherited.exception.status_code, 400)
        clean = _validate_routing_refs(self.db, self.root, {
            "version": 2, "roles": {"planner": {"provider_id": self.role.id}},
        })
        self.assertEqual(clean["roles"]["planner"]["provider_id"], self.role.id)
        self.main.max_tokens_param = "none"
        self.db.commit()
        with self.assertRaises(HTTPException) as unsupported_limit:
            _validate_routing_refs(self.db, self.root, self.routing(None), primary_provider_id=self.main.id)
        self.assertIn("Token", unsupported_limit.exception.detail)

    def test_per_turn_executor_override_cannot_silently_drop_role_parameters(self):
        self.role.provider_type = "chatgpt"
        self.role.reasoning_config = "{}"
        self.role.reasoning_effort = ""
        agent = Agent(name="turn-override", provider_id=self.main.id, created_by=self.root.id,
                      routing=json.dumps(self.routing(None)))
        self.db.add(agent)
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            build_execution_snapshot(self.db, agent, "hello", provider=self.role)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("阶段参数", raised.exception.detail)

    def test_role_provider_and_options_frozen_without_mutating_main(self):
        agent = Agent(name="frozen", provider_id=self.main.id, created_by=self.root.id,
                      routing=json.dumps(self.routing(self.role.id)))
        self.db.add(agent)
        self.db.commit()
        frozen = build_execution_snapshot(self.db, agent, "hello")
        self.role.model_id = "changed-after-queue"
        self.role.reasoning_effort = "minimal"
        agent.routing = json.dumps({"planning": {"mode": "off"}})
        self.db.commit()
        values = []
        with patch("backend.api.chat._client_from_provider_snapshot", side_effect=lambda value: values.append(value) or Client()):
            clients = _role_clients_from_execution_snapshot(frozen, self.db)
        self.assertEqual(set(clients), {"planner"})
        self.assertEqual(values[0]["model_id"], "role")
        self.assertEqual(values[0]["reasoning_effort"], "high")
        self.assertEqual(values[0]["max_tokens"], 512)
        self.assertEqual(frozen["model_execution"]["planning"]["mode"], "always")
        self.assertEqual((self.main.reasoning_effort, self.main.max_tokens), ("low", 8192))

    def test_queued_role_revocation_falls_back_without_constructing_client(self):
        self.role.is_public = True
        agent = Agent(name="revoked", provider_id=self.main.id, created_by=self.admin.id,
                      routing=json.dumps(self.routing(self.role.id)))
        self.db.add(agent)
        self.db.commit()
        frozen = build_execution_snapshot(self.db, agent, "hello")
        self.assertIn("planner", frozen["model_roles"])
        self.role.is_public = False
        self.db.commit()
        with patch("backend.api.chat._client_from_provider_snapshot") as construct:
            self.assertEqual(_role_clients_from_execution_snapshot(frozen, self.db), {})
            construct.assert_not_called()
        self.assertEqual(build_execution_snapshot(self.db, agent, "hello")["model_roles"], {})

    def test_disabled_queued_role_falls_back_and_inherited_overrides_stay_frozen(self):
        agent = Agent(name="overrides", provider_id=self.main.id, created_by=self.root.id,
                      routing=json.dumps(self.routing(None)))
        self.db.add(agent)
        self.db.commit()
        frozen = build_execution_snapshot(self.db, agent, "hello")
        self.assertEqual(frozen["model_roles"]["planner"]["provider"]["id"], self.main.id)
        self.assertEqual(frozen["provider"]["max_tokens"], 8192)
        agent.routing = json.dumps(self.routing(self.role.id))
        self.db.commit()
        frozen = build_execution_snapshot(self.db, agent, "hello")
        self.role.enabled = False
        self.db.commit()
        self.assertEqual(_role_clients_from_execution_snapshot(frozen, self.db), {})

    def test_invalid_stage_settings_fail_closed(self):
        cases = [
            {"tool_routing": {"confidence_threshold": float("nan")}},
            {"tool_routing": {"confidence_threshold": True}},
            {"roles": {"planner": {"provider_id": True}}},
            {"roles": {"planner": {"max_tokens": 1.2}}},
            {"roles": {"planner": {"reasoning_effort": "invented"}}},
            {"roles": {"executor": {}}},
            {"planning": {"mode": "sometimes"}},
            {"review": {"mode": "always"}},
            {"roles": {"planner": []}},
            {"tool_routing": []},
            {"roles": {"planner": {"reasoning_effort": False}}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                normalize_execution_options(case)


class TypedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tools = [{"type": "function", "function": {"name": name}} for name in ("read", "search")]

    def select(self, choices, confidence):
        return parse_tool_selection(message(name="select_tools", arguments={
            "choices": choices, "confidence": confidence,
        }), self.tools, .7)

    def test_valid_subset_retains_original_tool_schemas(self):
        selected, audit = self.select(["search"], .9)
        self.assertEqual(selected, [self.tools[1]])
        self.assertIs(selected[0], self.tools[1])
        self.assertEqual(audit["decision"], "accepted")
        self.assertEqual(audit["confidence_kind"], "model_estimate")

    def test_low_confidence_and_unauthorized_invalid_choices_fall_back(self):
        for choices, confidence in [(["search"], .6), (["delete_everything"], 1),
                                    (["search", "search"], 1), ([], 1), (["search"], True),
                                    (["search"], float("nan")), (["search"], 2)]:
            with self.subTest(choices=choices, confidence=confidence):
                selected, audit = self.select(choices, confidence)
                self.assertIs(selected, self.tools)
                self.assertEqual(audit["source"], "deterministic")

    def test_raw_output_and_extra_properties_are_never_public(self):
        selected, audit = parse_tool_selection(message(name="select_tools", arguments={
            "choices": ["search"], "confidence": .99, "reasoning": "secret",
        }), self.tools, .7)
        self.assertIs(selected, self.tools)
        payload = public_process_payload("tools.selection", {**audit, "reasoning": "secret", "arguments": "secret"})
        self.assertNotIn("secret", json.dumps(payload))
        self.assertTrue(is_public_process_event("tools.selection"))
        self.assertEqual(public_process_payload("model.role.selected", {
            "role": "router", "provider_id": 4, "raw": "secret", "reasoning": "secret",
        }), {"role": "router", "provider_id": 4, "inherited": False})


class StageModelExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_simple_question_has_no_extra_stage_calls(self):
        main, planner, router, critic = [Client() for _ in range(4)]
        answer, _ = await run_harness(main, "", "你好", role_clients={
            "planner": planner, "router": router, "critic": critic,
        })
        self.assertEqual(answer, "已完成答复")
        self.assertEqual([len(c.calls) for c in (main, planner, router, critic)], [1, 0, 0, 0])

    async def test_planner_runs_initial_plan_and_closeout_on_short_question(self):
        class Planner(Client):
            async def chat_with_tools(self, messages, tools=None):
                self.calls.append((list(messages), list(tools or [])))
                states = ["in_progress", "pending"] if len(self.calls) == 1 else ["completed", "completed"]
                return message("", name="update_plan", arguments={"plan": [
                    {"step": "理解问题", "status": states[0]}, {"step": "形成答复", "status": states[1]},
                ]})
        main, planner = Client(), Planner()
        events = []
        await run_harness(main, "", "你好", role_clients={"planner": planner},
                          model_execution={"planning": {"mode": "always"}},
                          tool_policy={"max_iterations": 1}, runtime_event=lambda name, payload: events.append((name, payload)))
        self.assertEqual(len(planner.calls), 2)
        self.assertTrue(all([item["function"]["name"] for item in tools] == ["update_plan"]
                            for _, tools in planner.calls))
        self.assertTrue(any(name == "plan.closeout.completed" for name, _ in events))
        self.assertGreaterEqual(len(main.calls), 1)

    async def test_planning_off_omits_plan_tool_and_never_calls_planner(self):
        main, planner = Client(), Client()
        await run_harness(main, "", "请为我整理并分析一份完整材料", role_clients={"planner": planner},
                          model_execution={"planning": {"mode": "off"}})
        self.assertEqual(len(planner.calls), 0)
        self.assertFalse(any(item["function"]["name"] == "update_plan" for _, tools in main.calls for item in (tools or [])))

    async def test_critic_revises_then_deterministic_validation_reruns(self):
        main, critic = Client(message("初稿")), Client(message("修订答复带验收标识"))
        events = []
        answer, _ = await run_harness(main, "", "你好", role_clients={"critic": critic},
                                     verification_policy={"required_terms": ["验收标识"], "strict": True},
                                     runtime_event=lambda name, payload: events.append((name, payload)))
        self.assertIn("验收标识", answer)
        self.assertEqual(len(critic.calls), 1)
        self.assertTrue(any(name == "verification.completed" and payload["passed"] for name, payload in events))

    async def test_review_off_does_not_disable_validation(self):
        main, critic = Client(message("初稿")), Client(message("通过"))
        metadata = {}
        answer, _ = await run_harness(main, "", "你好", role_clients={"critic": critic},
                                      model_execution={"review": {"mode": "off"}},
                                      verification_policy={"required_terms": ["验收标识"], "strict": True},
                                      completion_metadata=metadata)
        self.assertIn("未满足完成条件", answer)
        self.assertEqual(metadata["completion_status"], "completed_with_issues")
        self.assertIn("缺少必需内容：验收标识", metadata["completion_issues"])
        with self.assertRaises(CompletionVerificationError):
            await run_harness(main, "", "你好", role_clients={"critic": critic},
                              model_execution={"review": {"mode": "off"}},
                              verification_policy={"require_successful_tool": True})
        self.assertEqual(len(critic.calls), 0)

    async def test_unavailable_critic_falls_back_to_main(self):
        main, critic = Client(message("初稿")), Client(RuntimeError("provider unavailable"))
        events = []
        await run_harness(main, "", "你好", role_clients={"critic": critic},
                          verification_policy={"required_terms": ["标识"]},
                          runtime_event=lambda name, payload: events.append((name, payload)))
        self.assertEqual(len(critic.calls), 1)
        self.assertEqual(len(main.calls), 2)
        self.assertTrue(any(name == "model.role.fallback" for name, _ in events))

    async def test_content_guardrail_denial_never_falls_back(self):
        main, critic = Client(message("初稿")), Client(ContentBlocked("model_output"))
        with self.assertRaises(ContentBlocked):
            await run_harness(main, "", "你好", role_clients={"critic": critic},
                              verification_policy={"required_terms": ["标识"]})
        self.assertEqual(len(main.calls), 1)

    async def test_stage_model_keeps_user_agent_and_provider_guardrail_identity(self):
        main, critic = Client(message("初稿"), 7), Client(message("包含验收标识"), 8)
        context = builtin_tools.BuiltinToolContext(user_id=42, agent_id=73, enabled_tools=set())
        with patch("backend.guardrail_policies.active_content_policies", return_value=([], {})) as policies:
            await run_harness(main, "", "你好", role_clients={"critic": critic}, builtin_context=context,
                              verification_policy={"required_terms": ["验收标识"]})
        identities = [item.kwargs for item in policies.call_args_list]
        self.assertIn({"user_id": 42, "agent_id": 73, "provider_id": 8}, identities)

    async def test_model_router_controls_offered_subset_and_low_confidence_fallback(self):
        for choice, confidence, decision in [("read_skill_resource", .9, "accepted"),
                                              ("read_skill_resource", .2, "low_confidence"),
                                              ("unauthorized", .99, "invalid_selection")]:
            with self.subTest(decision=decision):
                main = Client()
                router = Client(message(name="select_tools", arguments={"choices": [choice], "confidence": confidence}))
                events = []
                await run_harness(main, "", "你好", skills=[{"name": "说明", "instructions": "资料"}],
                                  role_clients={"router": router}, model_execution={"tool_routing": {"mode": "model"}},
                                  runtime_event=lambda name, payload: events.append((name, payload)))
                self.assertEqual(len(router.calls), 1)
                offered = [item["function"]["name"] for item in main.calls[0][1]]
                self.assertEqual(offered, ["read_skill_resource"] if decision == "accepted" else ["update_plan", "read_skill_resource"])
                self.assertTrue(any(name == "tools.selection" and payload["decision"] == decision for name, payload in events))


if __name__ == "__main__":
    unittest.main()
