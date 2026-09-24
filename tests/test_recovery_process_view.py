"""Safe live and historical facts for recovery and tool-based repair."""
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
from backend.database import Base
from backend.models import Agent, Job, User
from backend.runtime import task_store
from backend.runtime.process_view import is_public_process_event, public_process_payload


CASES = (
    ("task.contract.updated", {"revision": 3, "mode": "redirect"}),
    ("capability.activated", {"kind": "skill", "name": "document.inspect"}),
    ("invocation.reused", {"tool": "read", "call_id": "call-3"}),
    ("recovery.resumed", {"checkpoint_revision": 7, "iteration": 4}),
    ("recovery.blocked", {"tool": "write", "call_id": "call-4", "reason": "unknown_outcome"}),
    ("verification.repair.started", {"attempt": 1, "issues_count": 2}),
    ("verification.repair.completed", {"attempt": 1, "issues_count": 0, "ok": True}),
)
PRIVATE_FIELDS = {key: {"text": "PRIVATE"} for key in (
    "messages", "arguments", "args", "result", "hash", "reasoning", "raw_output", "content",
)}


class RecoveryProcessViewTests(unittest.TestCase):
    def test_new_events_keep_only_public_facts_and_execution_identity(self):
        for event_type, expected in CASES:
            with self.subTest(event_type=event_type):
                payload = {**expected, **PRIVATE_FIELDS, "agent_id": 3,
                           "execution_scope": "inline_subagent", "child_run_id": "child-1"}
                original = copy.deepcopy(payload)
                result = public_process_payload(event_type, payload)
                self.assertTrue(is_public_process_event(event_type))
                self.assertEqual(result, {**expected, "agent_id": 3,
                                         "execution_scope": "inline_subagent", "child_run_id": "child-1"})
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertEqual(payload, original)
        self.assertFalse(is_public_process_event("recovery.private_state"))
        self.assertFalse(is_public_process_event("invocation.raw_result"))

    def test_malformed_public_fields_cannot_smuggle_nested_private_data(self):
        payload = {key: {"content": "PRIVATE"} for key in (
            "revision", "mode", "kind", "name", "tool", "call_id", "parent_run_id",
            "child_run_id", "delegation_call_id", "execution_scope", "checkpoint_revision",
            "iteration", "attempt", "issues_count", "reason", "ok",
        )}
        payload.update(agent_id=True, parent_agent_id="PRIVATE", subagent_depth=-1)
        for event_type, _ in CASES:
            with self.subTest(event_type=event_type):
                result = public_process_payload(event_type, payload)
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertNotIn("call_id", result)
                self.assertNotIn("agent_id", result)
                self.assertNotIn("execution_scope", result)
        self.assertEqual(public_process_payload("task.contract.updated", {
            "revision": True, "mode": "<think>PRIVATE</think>", "status": "completed",
            "error": "PRIVATE", "reason": "PRIVATE",
        }), {"revision": 0, "mode": "unknown"})
        self.assertEqual(public_process_payload("verification.repair.completed", {
            "attempt": float("inf"), "issues_count": -2, "ok": "PRIVATE",
        }), {"attempt": 0, "issues_count": 0, "ok": False})
        self.assertEqual(public_process_payload("invocation.reused", {
            "tool": "<think>PRIVATE</think>", "call_id": "x" * 97,
        }), {"tool": ""})
        self.assertEqual(public_process_payload("recovery.resumed", {
            "checkpoint_revision": 2 ** 80, "iteration": "PRIVATE",
        }), {"checkpoint_revision": 0, "iteration": 0})

    def test_history_and_reconnect_keep_recovery_facts_without_changing_task_status(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                owner = User(username="recovery-owner", password_hash="unused", role="user")
                agent = Agent(name="recovery-agent")
                db.add_all([owner, agent])
                db.flush()
                job_id = jobs.enqueue_in_session(db, owner.id, agent.id, "chat", {"query": "task"})
                row = db.get(Job, job_id)
                row.status = jobs.RUNNING
                task_store.append_runtime_item(db, job_id, "task.started", {"status": "executing"})
                for event_type, expected in CASES:
                    task_store.append_runtime_item(db, job_id, event_type,
                        {**expected, **PRIVATE_FIELDS, "status": "completed", "error": "PRIVATE"})
                db.commit()
                snapshot = chat._run_processes(db, [row])[job_id]
                self.assertEqual(snapshot["task_status"], "executing")
                self.assertEqual([(e["event_type"], e["payload"]) for e in snapshot["events"][-7:]], list(CASES))
                self.assertNotIn("PRIVATE", json.dumps(snapshot["events"]))
                with patch.object(chat, "SessionLocal", return_value=db):
                    self.assertEqual(chat._persisted_events_after(job_id, 0), snapshot["events"])
        finally:
            engine.dispose()

    def test_live_stream_uses_the_same_safe_event_projection(self):
        async def exercise():
            queue = asyncio.Queue()
            for index, (event_type, expected) in enumerate(CASES):
                queue.put_nowait({"type": "runtime", "event_type": event_type,
                                  "event_id": f"recovery-{index}", "payload": {**expected, **PRIVATE_FIELDS}})
            with ExitStack() as stack:
                for target, name, kwargs in (
                    (chat, "_user_id_from_request", {"return_value": 1}),
                    (chat, "_persisted_events_after", {"return_value": []}),
                    (jobs, "view", {"return_value": SimpleNamespace(status=jobs.RUNNING, payload={})}),
                    (jobs, "subscribe", {"return_value": queue}),
                    (jobs, "unsubscribe", {}),
                    (jobs, "get_partial", {"return_value": ""}),
                ):
                    stack.enter_context(patch.object(target, name, **kwargs))
                response = await chat.stream_chat_job("recovery-job", SimpleNamespace(query_params={}))
                stream = response.body_iterator
                try:
                    for event_type, expected in CASES:
                        event = json.loads(await anext(stream))
                        self.assertEqual(event["event_type"], event_type)
                        self.assertEqual(event["payload"], expected)
                        self.assertNotIn("PRIVATE", json.dumps(event))
                finally:
                    await stream.aclose()
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
