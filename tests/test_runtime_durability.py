"""Private recovery, lease fencing and invocation-specific approval contracts."""
import importlib
import json
import os
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import approvals, jobs
from backend.database import Base
from backend.models import Agent, Job, ToolInvocation, User
from backend.runtime.durability import (
    CheckpointConflict, ExecutionStore, InvocationConflict, RuntimeLeaseLost,
    UnknownToolOutcome, bind_execution_lease, execution_store,
)


class RuntimeDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SECRET_MASTER_KEY": "isolated-durable-runtime-test-master-key"})
        self.env.start()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        with self.sessions() as db:
            db.add(User(id=7, username="ledger-owner", password_hash="x"))
            db.add(Agent(id=9, name="ledger-agent"))
            db.add(Job(id="run1", owner_id=7, agent_id=9, status="running",
                       worker_id="worker1", lease_token="lease1", payload="{}"))
            db.commit()
        self.store = ExecutionStore("run1", 7, session_factory=self.sessions)
        self.patches = [patch.object(module, "SessionLocal", self.sessions) for module in (approvals, jobs)]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.engine.dispose()
        self.env.stop()

    def prepare(self, call_id="call1", effect="write"):
        return self.store.prepare(call_id, "write", {"path": "private.txt", "content": "private body"}, "cap-v1", effect)

    def test_private_payloads_are_encrypted_and_checkpoint_updates_compare_revision(self):
        state = {"messages": [{"role": "user", "content": "checkpoint secret"}]}
        revision = self.store.save_checkpoint(state)
        self.assertEqual(revision, 1)
        self.assertEqual(self.store.load_checkpoint(), {"revision": 1, "state": state})
        with self.assertRaises(CheckpointConflict):
            self.store.save_checkpoint({"messages": []}, expected_revision=0)
        self.assertEqual(self.store.save_checkpoint({"messages": []}, expected_revision=1), 2)
        with self.assertRaises(CheckpointConflict):
            self.store.save_checkpoint(state, expected_revision=1)
        invocation = self.prepare()
        self.store.begin(invocation)
        self.store.complete(invocation, {"content": "result secret"})
        with self.engine.connect() as conn:
            checkpoint = conn.execute(text("SELECT state FROM runtime_checkpoints")).scalar_one()
            arguments, result = conn.execute(text("SELECT arguments, result FROM tool_invocations")).one()
        for ciphertext in (checkpoint, arguments, result):
            self.assertTrue(ciphertext.startswith("enc:v1:"))
        self.assertNotIn("private body", arguments)
        self.assertNotIn("result secret", result)

    def test_completed_observation_replays_without_dispatch(self):
        first = self.prepare()
        self.assertTrue(self.store.begin(first))
        self.store.complete(first, {"ok": True, "content": "saved observation"})
        recovered = self.prepare()
        self.assertEqual(first.id, recovered.id)
        self.assertFalse(self.store.begin(recovered))
        self.assertEqual(recovered.result, {"ok": True, "content": "saved observation"})
        with self.assertRaises(InvocationConflict):
            self.store.complete(recovered, {"ok": True, "content": "different"})

    def test_interrupted_write_has_unknown_outcome_and_is_never_retried(self):
        first = self.prepare()
        self.store.begin(first)
        resumed = self.prepare()
        with self.assertRaises(UnknownToolOutcome) as raised:
            self.store.begin(resumed)
        self.assertEqual(raised.exception.call_id, "call1")
        self.assertEqual(raised.exception.tool, "write")
        with self.sessions() as db:
            self.assertEqual(db.get(ToolInvocation, first.id).state, "unknown_outcome")
        with self.assertRaises(UnknownToolOutcome):
            self.store.begin(self.prepare())

    def test_only_explicit_read_effect_can_retry_after_interruption(self):
        first = self.prepare(effect="read_only")
        self.store.begin(first)
        self.assertTrue(self.store.begin(self.prepare(effect="read_only")))
        other = self.prepare(call_id="unknown-call", effect="unknown")
        self.store.begin(other)
        with self.assertRaises(UnknownToolOutcome):
            self.store.begin(other)

    def test_approval_wait_can_resume_same_invocation(self):
        first = self.prepare()
        self.store.begin(first)
        self.store.awaiting_approval(first)
        recovered = self.prepare()
        self.assertEqual(recovered.binding, first.binding)
        self.assertTrue(self.store.begin(recovered))
        self.store.complete(recovered, {"ok": True})

    def test_changed_arguments_or_capability_cannot_reuse_call_id(self):
        self.prepare()
        with self.assertRaises(InvocationConflict):
            self.store.prepare("call1", "write", {"path": "other.txt"}, "cap-v1", "write")
        with self.assertRaises(InvocationConflict):
            self.store.prepare("call1", "write", {"path": "private.txt", "content": "private body"}, "cap-v2", "write")

    def test_argument_hash_ignores_dictionary_key_order_and_child_execution_is_separate(self):
        first = self.prepare()
        same = self.store.prepare("call1", "write", {"content": "private body", "path": "private.txt"}, "cap-v1", "write")
        self.assertEqual(first.binding, same.binding)
        child = ExecutionStore("run1", 7, "child1", session_factory=self.sessions)
        child_call = child.prepare("call1", "write", {"path": "private.txt", "content": "private body"}, "cap-v1", "write")
        self.assertNotEqual(first.id, child_call.id)

    def test_other_owner_cannot_read_or_write_execution_state(self):
        self.store.save_checkpoint({"private": True})
        other = ExecutionStore("run1", 8, session_factory=self.sessions)
        with self.assertRaises(RuntimeLeaseLost):
            other.load_checkpoint()
        with self.assertRaises(RuntimeLeaseLost):
            other.save_checkpoint({})

    def test_stale_lease_cannot_checkpoint_or_complete_started_tool(self):
        self.assertIsNone(execution_store("run1", 7))
        with bind_execution_lease("run1", 7, "worker1", "lease1", session_factory=self.sessions):
            store = execution_store("run1", 7)
            invocation = store.prepare("call1", "write", {}, "cap-v1", "write")
            store.begin(invocation)
            with self.sessions() as db:
                job = db.get(Job, "run1")
                job.worker_id, job.lease_token = "worker2", "lease2"
                db.commit()
            with self.assertRaises(RuntimeLeaseLost):
                store.save_checkpoint({})
            with self.assertRaises(RuntimeLeaseLost):
                store.complete(invocation, {"ok": True})

    def test_exact_approval_rejects_other_arguments_revision_or_invocation(self):
        binding = self.prepare().binding
        token = approvals.issue("run1", 7, 9, "write", binding=binding)
        for key in binding:
            changed = {**binding, key: "changed"}
            with approvals.bind_invocation(changed):
                self.assertFalse(approvals.consume([token], run_id="run1", user_id=7, agent_id=9, scope="write"))
        self.assertFalse(approvals.consume([token], run_id="run1", user_id=7, agent_id=9, scope="write"))
        with approvals.bind_invocation(binding):
            required = approvals.ApprovalRequired("write", "确认写入")
            self.assertEqual(required.binding, binding)
            self.assertEqual(required.execution_context, {})
            self.assertTrue(approvals.consume([token], run_id="run1", user_id=7, agent_id=9, scope="write"))
            self.assertFalse(approvals.consume([token], run_id="run1", user_id=7, agent_id=9, scope="write"))

    def test_legacy_approval_cannot_authorize_bound_runtime(self):
        token = approvals.issue("run1", 7, 9, "write")
        with approvals.bind_invocation(self.prepare().binding):
            self.assertFalse(approvals.consume([token], run_id="run1", user_id=7, agent_id=9, scope="write"))
        self.assertTrue(approvals.consume([token], run_id="run1", user_id=7, agent_id=9, scope="write"))

    def test_job_approval_round_trip_preserves_binding(self):
        invocation = self.prepare()
        self.store.begin(invocation)
        self.store.awaiting_approval(invocation)
        self.assertTrue(jobs.wait_for_approval("run1", "worker1", "lease1", "write", "确认写入", binding=invocation.binding))
        self.assertTrue(jobs.approve_waiting("run1", 7))
        with self.sessions() as db:
            payload = json.loads(db.get(Job, "run1").payload)
        self.assertNotIn("_approval_binding", payload)
        with approvals.bind_invocation(invocation.binding):
            self.assertTrue(approvals.consume(payload["approval_tokens"], run_id="run1", user_id=7, agent_id=9, scope="write"))

    def test_migration_upgrades_old_tables_idempotently(self):
        engine = create_engine("sqlite://")
        migration = importlib.import_module("migrations.versions.0028_runtime_durability")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE tool_approvals (id VARCHAR(32) PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE jobs (id VARCHAR(32) PRIMARY KEY)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
            self.assertIn("runtime_checkpoints", inspect(connection).get_table_names())
            self.assertIn("tool_invocations", inspect(connection).get_table_names())
            self.assertTrue({"invocation_id", "arguments_digest", "capability_revision"}.issubset(
                {column["name"] for column in inspect(connection).get_columns("tool_approvals")}))
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
