"""Behavioral checks for the public execution boundary, including the live stream."""
import asyncio
import copy
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import jobs
from backend.api import chat
from backend.runtime.process_view import is_public_process_event, public_process_payload, runtime_event_type
from backend.database import Base
from backend.models import Agent, Job, User
from backend.runtime import task_store


class ProcessViewTests(unittest.TestCase):
    def test_provider_projection_retains_thinking_switch_without_private_blocks(self):
        for effort in ("disabled", "enabled"):
            payload = {"provider_id": 4, "reasoning_effort": effort,
                       "_anthropic_content": [{"thinking": "PRIVATE", "signature": "PRIVATE"}],
                       "reasoning": "PRIVATE", "api_key": "PRIVATE"}
            self.assertEqual(public_process_payload("provider.attempt", payload),
                             {"provider_id": 4, "reasoning_effort": effort})
        self.assertNotIn("reasoning_effort", public_process_payload(
            "provider.attempt", {"reasoning_effort": "arbitrary-private-value"}))

    def test_pdf_visual_source_projection_keeps_page_evidence_without_source_content(self):
        payload = {"page": 33, "source_page_count": 44, "ok": True, "provider_id": 4,
                   "result_chars": 234, "duration_ms": 1200, "source_path": "PRIVATE",
                   "visual_text": "PRIVATE", "reasons": ["PRIVATE"], "raw": "PRIVATE"}
        result = public_process_payload("attachments.visual_source", payload)
        self.assertTrue(is_public_process_event("attachments.visual_source"))
        self.assertEqual(result, {"page": 33, "source_page_count": 44, "ok": True,
                                "provider_id": 4, "result_chars": 234, "duration_ms": 1200})
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_content_guardrail_projection_bounds_metadata_and_omits_content(self):
        payload = {
            "decision": "block", "point": "model_output", "agent_id": 7, "provider_id": 9,
            "raw_output": "PRIVATE_TEXT", "matched_text": "PRIVATE_TEXT", "content": "PRIVATE_TEXT",
            "matches": [{"policy_id": 2, "policy_name": "policy" * 100, "action": "block",
                         "detector": "pii", "rule_index": 1, "count": 2,
                         "matched_text": "PRIVATE_TEXT", "value": "PRIVATE_TEXT"}] * 25,
        }
        original = copy.deepcopy(payload)
        result = public_process_payload("guardrail.content_evaluated", payload)
        self.assertTrue(is_public_process_event("guardrail.content_evaluated"))
        self.assertEqual(result["decision"], "block")
        self.assertEqual(result["point"], "model_output")
        self.assertEqual(result["provider_id"], 9)
        self.assertEqual(len(result["matches"]), 20)
        self.assertEqual(len(result["matches"][0]["policy_name"]), 128)
        self.assertNotIn("PRIVATE_TEXT", json.dumps(result))
        self.assertEqual(payload, original)

    def test_checkpoint_drops_private_fields_without_changing_source(self):
        payload = {
            "checkpoint": {
                "status": "completed_with_issues", "iteration": 3,
                "verification_issues": ["缺少产物"],
                "successful_tool_names": ["read_file"],
                "reasoning": "private", "messages": [{"content": "private"}],
            },
            "raw_output": "private",
        }
        original = copy.deepcopy(payload)
        result = public_process_payload("loop.completed", payload)
        self.assertEqual(result["checkpoint"], {
            "status": "completed_with_issues", "iteration": 3,
            "verification_issues": ["缺少产物"],
            "successful_tool_names": ["read_file"],
        })
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(payload, original)

    def test_tool_identity_and_failure_survive_projection(self):
        payload = {
            "tool": "read_file", "ok": False, "call_id": "call-2",
            "execution_scope": "inline_subagent", "child_run_id": "child-1",
            "parent_agent_id": 4, "agent_id": 6, "subagent_depth": 1,
            "error_type": "tool_unavailable", "error_code": "not_available",
            "arguments": {"secret": "private"}, "raw": "private",
        }
        result = public_process_payload("tool.completed", payload)
        for key in ("ok", "call_id", "execution_scope", "child_run_id", "error_type"):
            self.assertEqual(result[key], payload[key])
        self.assertNotIn("private", json.dumps(result))
        self.assertTrue(is_public_process_event("delegation.completed"))
        self.assertTrue(is_public_process_event("verification.started"))
        self.assertFalse(is_public_process_event("model.private_reasoning"))

    def test_legacy_tool_items_recover_lifecycle_from_kind_and_status(self):
        for kind, status, expected in (
            ("tool_call", "started", "tool.called"),
            ("tool_result", "completed", "tool.completed"),
            ("tool_result", "failed", "tool.completed"),
            ("tool_result", "deferred", "tool.deferred"),
        ):
            with self.subTest(status=status):
                item = SimpleNamespace(name="read_file", kind=kind, status=status)
                self.assertEqual(runtime_event_type(item, {}), expected)
        item = SimpleNamespace(name="read_file", kind="tool_result", status="failed")
        self.assertEqual(runtime_event_type(item, {"_event_type": "tool.rejected"}), "tool.rejected")

    def test_live_stream_uses_same_projection_as_history(self):
        async def exercise():
            queue = asyncio.Queue()
            payload = {"tool": "read_file", "ok": False, "call_id": "call-1",
                       "arguments": {"token": "private"}, "raw": "private"}
            queue.put_nowait({"type": "runtime", "event_type": "model.private_reasoning",
                              "event_id": "hidden", "payload": {"text": "private"}})
            queue.put_nowait({"type": "runtime", "event_type": "tool.completed",
                              "event_id": "visible", "payload": payload})
            running = SimpleNamespace(status=jobs.RUNNING, payload={})
            with ExitStack() as stack:
                for target, name, kwargs in (
                    (chat, "_user_id_from_request", {"return_value": 1}),
                    (chat, "_persisted_events_after", {"return_value": []}),
                    (jobs, "view", {"return_value": running}),
                    (jobs, "subscribe", {"return_value": queue}),
                    (jobs, "unsubscribe", {}),
                    (jobs, "get_partial", {"return_value": ""}),
                ):
                    stack.enter_context(patch.object(target, name, **kwargs))
                response = await chat.stream_chat_job("task-1", SimpleNamespace(query_params={}))
                stream = response.body_iterator
                try:
                    value = json.loads(await anext(stream))
                finally:
                    await stream.aclose()
            self.assertEqual(value["event_type"], "tool.completed")
            self.assertEqual(value["payload"], public_process_payload("tool.completed", payload))
            self.assertNotIn("private", json.dumps(value))
        asyncio.run(exercise())

    def test_child_plan_and_terminal_do_not_replace_parent_snapshot(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                owner = User(username="projection-owner", password_hash="unused", role="user")
                agent = Agent(name="projection-agent")
                db.add_all([owner, agent])
                db.flush()
                job_id = jobs.enqueue_in_session(db, owner.id, agent.id, "chat", {"query": "parent"})
                row = db.get(Job, job_id)
                row.status = jobs.DONE
                for event_type, payload in (
                    ("tool.called", {"tool": "read_file", "call_id": "call-main"}),
                    ("tool.completed", {"tool": "read_file", "call_id": "call-main", "ok": False}),
                    ("plan.created", {"steps": [{"id": "parent", "step": "parent", "status": "completed"}]}),
                    ("task.completed", {"status": "completed"}),
                    ("plan.created", {"execution_scope": "inline_subagent", "child_run_id": "child",
                                      "steps": [{"id": "child", "step": "child", "status": "blocked"}]}),
                    ("task.failed", {"execution_scope": "inline_subagent", "child_run_id": "child", "status": "failed"}),
                ):
                    task_store.append_runtime_item(db, job_id, event_type, payload)
                db.commit()
                result = chat._run_processes(db, [row])[job_id]
                self.assertEqual(result["task_status"], "completed")
                self.assertEqual(result["events"][-1]["payload"]["execution_scope"], "inline_subagent")
                tool_events = [event for event in result["events"] if event["event_type"].startswith("tool.")]
                self.assertEqual([event["event_type"] for event in tool_events], ["tool.called", "tool.completed"])
                self.assertFalse(tool_events[-1]["payload"]["ok"])
                self.assertEqual(tool_events[-1]["payload"]["call_id"], "call-main")
                with patch.object(chat, "SessionLocal", return_value=db):
                    replay = chat._persisted_events_after(job_id, 0)
                self.assertEqual(replay, result["events"])
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
