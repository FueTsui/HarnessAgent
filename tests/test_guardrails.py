"""Guardrails persist, preserve authorization, and stop real dispatch boundaries."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import approvals, guardrails
from backend import guardrail_policies
from backend.api.guardrails import router
from backend.database import Base, get_db
from backend.models import AppSetting, User
from backend.runtime import builtin_tools, run_harness
from backend.llm import mcp_client
from backend.security import get_current_user


class _RemoteLlm:
    context_tokens = 8192

    def __init__(self):
        self.calls = 0

    async def chat_with_tools(self, messages, tools=None, temperature=1):
        self.calls += 1
        remote = next((tool["function"]["name"] for tool in tools or []
                       if tool["function"]["name"] != "update_plan"), "")
        if self.calls == 1 and remote:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "remote-action", "type": "function",
                "function": {"name": remote, "arguments": '{"record_id":"42"}'},
            }]}
        return {"role": "assistant", "content": "已检查调用结果", "tool_calls": []}


class _RemoteConnection:
    calls = []

    def __init__(self, server):
        self.server = server

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_tools(self):
        return [{
            "name": "delete_record", "description": "Delete external record",
            "inputSchema": {"type": "object", "properties": {"record_id": {"type": "string"}}, "required": ["record_id"]},
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        }]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return '{"ok":true}'


class GuardrailTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.runtime_store = patch.object(guardrails, "SessionLocal", self.sessions)
        self.approval_store = patch.object(approvals, "SessionLocal", self.sessions)
        self.content_store = patch.object(guardrail_policies, "SessionLocal", self.sessions)
        self.runtime_store.start()
        self.approval_store.start()
        self.content_store.start()
        self.app = FastAPI()
        self.app.include_router(router)
        self.user = User(id=1, username="root", password_hash="x", role="root", is_active=True)

        def session():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = session
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.app)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.client.close()
        self.runtime_store.stop()
        self.approval_store.stop()
        self.content_store.stop()
        self.temp.cleanup()
        self.engine.dispose()

    def configure(self, **changes):
        with self.sessions() as db:
            current = guardrails.read_config(db)
            config = guardrails.GuardrailConfig(**{**current["config"].model_dump(), **changes})
            return guardrails.save_config(db, config, current["revision"])

    def context(self, **changes):
        return builtin_tools.BuiltinToolContext(**{
            "root": self.root, "user_id": 1, "agent_id": 2,
            "run_id": "guardrail-test", "approval_policy": "full_access",
            "enabled_tools": {"read", "write", "shell"}, **changes,
        })

    def write_file(self, name="output.txt", **changes):
        return json.loads(asyncio.run(builtin_tools.execute(
            "write", {"path": name, "content": "hello", "overwrite": False, "confirm": True},
            self.context(**changes),
        )))

    def test_root_only_for_read_save_and_preview_even_with_forged_module(self):
        for role in ("admin", "user"):
            self.user.role = role
            self.user.permissions = '["guardrails"]'
            for method, path, body in (
                ("GET", "/api/v1/guardrails", None),
                ("PUT", "/api/v1/guardrails", {"config": {}, "revision": 0}),
                ("POST", "/api/v1/guardrails/preview", {"tool_name": "read"}),
            ):
                with self.subTest(role=role, method=method):
                    self.assertEqual(self.client.request(method, path, json=body).status_code, 403)
        self.app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.get("/api/v1/guardrails").status_code, 401)

    def test_save_round_trip_revision_and_conflict_preserves_winner(self):
        first = self.client.get("/api/v1/guardrails").json()
        self.assertEqual(first["revision"], 0)
        self.assertEqual(first["stats"]["active_rules"], 0)
        config = {**first["config"], "blocked_tools": [" shell ", "shell"]}
        updated = self.client.put("/api/v1/guardrails", json={"config": config, "revision": 0})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["revision"], 1)
        self.assertEqual(updated.json()["config"]["blocked_tools"], ["shell"])
        self.assertTrue(updated.json()["updated_at"])
        self.assertEqual(self.client.get("/api/v1/guardrails").json(), updated.json())
        stale = self.client.put("/api/v1/guardrails", json={"config": first["config"], "revision": 0})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.get("/api/v1/guardrails").json()["config"]["blocked_tools"], ["shell"])

    def test_invalid_configuration_is_rejected_without_writing(self):
        for config in (
            {"enabled": "false"}, {"max_argument_chars": -1}, {"max_argument_chars": 1_000_001},
            {"max_argument_chars": True}, {"blocked_tools": ["two names"]},
            {"blocked_tools": [""]}, {"blocked_tools": ["x"] * 101}, {"unknown": True},
        ):
            with self.subTest(config=config):
                response = self.client.put("/api/v1/guardrails", json={"config": config, "revision": 0})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/v1/guardrails").json()["revision"], 0)

    def test_preview_derives_builtin_risk_and_does_not_execute_or_save(self):
        self.configure(block_mutating_tools=True)
        response = self.client.post("/api/v1/guardrails/preview", json={
            "kind": "builtin", "tool_name": "write", "mutating": False,
            "arguments": {"path": "preview.txt", "content": "should not exist", "confirm": True},
            "approval_policy": "full_access",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "block")
        self.assertFalse(response.json()["authorization_checked"])
        self.assertFalse((self.root / "preview.txt").exists())
        self.assertEqual(self.client.get("/api/v1/guardrails").json()["revision"], 1)
        unknown = self.client.post("/api/v1/guardrails/preview", json={"tool_name": "invented_tool"})
        self.assertEqual(unknown.status_code, 400)

    def test_runtime_blocks_before_write_and_before_consuming_approval(self):
        self.configure(block_mutating_tools=True)
        events = []
        with patch.object(builtin_tools, "consume_approval") as consume:
            result = self.write_file(runtime_event=lambda kind, data: events.append((kind, data)))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "guardrail_blocked")
        self.assertFalse((self.root / "output.txt").exists())
        consume.assert_not_called()
        self.assertEqual(events[0][0], "guardrail.evaluated")
        self.assertNotIn("hello", json.dumps(events))

    def test_update_applies_on_next_dispatch(self):
        self.assertTrue(self.write_file("first.txt")["ok"])
        self.configure(blocked_tools=["write"])
        self.assertEqual(self.write_file("second.txt")["code"], "guardrail_blocked")
        self.assertFalse((self.root / "second.txt").exists())

    def test_disabled_keeps_original_approval_and_tool_authorization(self):
        self.configure(enabled=False, block_mutating_tools=True, blocked_tools=["read", "write"])
        with self.assertRaises(approvals.ApprovalRequired):
            self.write_file(approval_policy="ask")
        unauthorized = self.write_file(enabled_tools={"read"})
        self.assertFalse(unauthorized["ok"])
        self.assertIn("未授权", unauthorized["error"])
        self.assertTrue(self.write_file()["ok"])
        escaped = json.loads(asyncio.run(builtin_tools.execute("read", {"path": "../outside.txt"}, self.context())))
        self.assertFalse(escaped["ok"])

    def test_high_risk_rule_requires_single_use_approval_under_full_access(self):
        self.configure(require_high_risk_approval=True)
        args = {"path": "approved.txt", "content": "authorized", "overwrite": True, "confirm": True}
        context = self.context()
        with self.assertRaises(approvals.ApprovalRequired) as caught:
            asyncio.run(builtin_tools.execute("write", args, context))
        self.assertEqual(caught.exception.scope, "write")
        context.approval_tokens = [approvals.issue(context.run_id, context.user_id, context.agent_id, "write")]
        result = json.loads(asyncio.run(builtin_tools.execute("write", args, context)))
        self.assertTrue(result["ok"])
        self.assertEqual((self.root / "approved.txt").read_text(), "authorized")
        with self.assertRaises(approvals.ApprovalRequired):
            asyncio.run(builtin_tools.execute("write", args, context))

    def test_extra_approval_cannot_run_without_persistent_approval_context(self):
        self.configure(require_high_risk_approval=True)
        result = json.loads(asyncio.run(builtin_tools.execute(
            "write", {"path": "no-context.txt", "content": "hello", "overwrite": True, "confirm": True},
            self.context(run_id=None),
        )))
        self.assertEqual(result["code"], "guardrail_approval_context_required")
        self.assertFalse((self.root / "no-context.txt").exists())

    def test_argument_limit_and_scoped_mcp_denial_use_same_evaluator(self):
        self.configure(max_argument_chars=2)
        self.assertEqual(self.write_file()["code"], "guardrail_blocked")
        config = guardrails.GuardrailConfig(blocked_tools=["mcp:3:delete_record"])
        for server_id, expected in ((3, "block"), (4, "allow")):
            result = guardrails.evaluate_tool(config, kind="mcp", tool_name="delete_record", arguments={}, policy="full_access", mutating=True, server_id=server_id)
            self.assertEqual(result["decision"], expected)

    def test_destructive_mcp_preview_cannot_claim_read_only_to_bypass_guardrail(self):
        self.configure(require_high_risk_approval=True)
        response = self.client.post("/api/v1/guardrails/preview", json={
            "kind": "mcp", "tool_name": "delete_record", "mutating": False,
            "destructive": True, "approval_policy": "full_access",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "require_approval")

    def test_invalid_persisted_policy_fails_closed_without_leaking_content(self):
        with self.sessions() as db:
            db.add(AppSetting(key=guardrails.SETTING_KEY, value="sensitive-invalid-data"))
            db.commit()
        result = self.write_file()
        self.assertEqual(result["code"], "guardrail_blocked")
        self.assertNotIn("sensitive-invalid-data", json.dumps(result))
        self.assertFalse((self.root / "output.txt").exists())

    def run_remote(self, **context_options):
        _RemoteConnection.calls.clear()
        with patch.object(mcp_client, "McpConnection", _RemoteConnection):
            return asyncio.run(run_harness(
                _RemoteLlm(), "可靠回答", "invoke remote capability",
                mcp_servers=[SimpleNamespace(id=3, name="remote", transport="http")],
                builtin_context=self.context(enabled_tools=set(), **context_options),
                verification_policy={"required": False},
            ))

    def test_mcp_block_stops_actual_remote_dispatch(self):
        self.configure(blocked_tools=["mcp:3:delete_record"])
        self.run_remote()
        self.assertEqual(_RemoteConnection.calls, [])

    def test_mcp_destructive_operation_obeys_high_risk_rule(self):
        self.configure(require_high_risk_approval=True)
        with self.assertRaises(approvals.ApprovalRequired) as caught:
            self.run_remote()
        self.assertEqual(caught.exception.scope, "mcp:3:delete_record")
        self.assertEqual(_RemoteConnection.calls, [])

    def test_mcp_forced_approval_overrides_full_access_and_disabled_preserves_ask(self):
        self.configure(require_external_approval=True)
        with self.assertRaises(approvals.ApprovalRequired) as caught:
            self.run_remote()
        self.assertEqual(caught.exception.scope, "mcp:3:delete_record")
        self.assertEqual(_RemoteConnection.calls, [])
        self.configure(enabled=False)
        with self.assertRaises(approvals.ApprovalRequired):
            self.run_remote(approval_policy="ask")
        self.run_remote()
        self.assertEqual(len(_RemoteConnection.calls), 1)


if __name__ == "__main__":
    unittest.main()
