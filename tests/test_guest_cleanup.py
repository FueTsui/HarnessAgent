"""Guest cleanup uses temporary databases/files; never touches app data."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import guest_cleanup as cleanup
from backend.api import users as api
from backend.database import Base, get_db
from backend.guardrail_models import GuardrailReview
from backend.memory_store import MemoryEntry
from backend.models import (
    Agent, ApiKey, Artifact, Attachment, AuditLog, AuthSession, HarnessVersion,
    Item, Job, JobGuidance, ModelProvider, Project, Thread, TokenUsage,
    ToolApproval, Turn, User, UserTokenLimit,
)
from backend.security import get_current_user


class GuestCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rootdir = Path(self.temp.name).resolve()
        self.patches = []
        for name in ("UPLOAD_DIR", "EXPORT_DIR", "WORKSPACE_DIR", "KNOWLEDGE_DIR"):
            directory = self.rootdir / name.lower()
            directory.mkdir()
            p = patch.object(cleanup, name, directory)
            p.start()
            self.patches.append(p)
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.factory()
        self.root = User(username="root-test", role="root", password_hash="unused", is_active=True)
        self.guest = User(username="unrelated-name", role="guest", password_hash="unused", is_active=True)
        self.member = User(username="visitor_but_registered", role="user", password_hash="unused", is_active=True)
        self.db.add_all([self.root, self.guest, self.member])
        self.db.commit()
        self.guest_id = self.guest.id
        self.identity = self.root
        app = FastAPI()
        app.include_router(api.router)
        def current():
            if self.identity is None:
                raise HTTPException(401, "请登录")
            return self.identity
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = current
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def graph(self):
        p = ModelProvider(name=f"__personal_model_{self.guest_id}_test", created_by=self.guest_id, api_key="fixture-secret")
        self.db.add(p)
        self.db.flush()
        a = Agent(name=f"__guest_agent_{self.guest_id}", created_by=self.guest_id, provider_id=p.id)
        self.db.add(a)
        self.db.flush()
        project = Project(user_id=self.guest_id, name="guest project", default_agent_id=a.id)
        self.db.add(project)
        self.db.flush()
        thread = Thread(id="guest-thread", owner_id=self.guest_id, project_id=project.id, agent_id=a.id)
        self.db.add(thread)
        self.db.flush()
        turn = Turn(id="guest-turn", thread_id=thread.id, owner_id=self.guest_id, agent_id=a.id, sequence=1, status="completed")
        job = Job(id="guest-job", owner_id=self.guest_id, agent_id=a.id, status="done", payload="{}")
        self.db.add_all([turn, job])
        self.db.flush()
        upload = cleanup.UPLOAD_DIR / "upload.txt"
        upload.write_text("private content", encoding="utf-8")
        upload.with_suffix(".name").write_text("original.txt", encoding="utf-8")
        export = cleanup.EXPORT_DIR / "result.txt"
        export.write_text("private result", encoding="utf-8")
        workspace = cleanup.WORKSPACE_DIR / f"user_{self.guest_id}" / f"agent_{a.id}" / "run_guest-job"
        workspace.mkdir(parents=True)
        (workspace / "code.py").write_text("# private", encoding="utf-8")
        now = dt.datetime.now(dt.timezone.utc)
        self.db.add_all([
            HarnessVersion(agent_id=a.id, created_by=self.guest_id, version=1),
            AuthSession(token_hash="x" * 64, user_id=self.guest_id, token_version=0, expires_at=now),
            UserTokenLimit(user_id=self.guest_id, total_limit=123),
            Item(id="guest-item", turn_id=turn.id, thread_id=thread.id, sequence=1, kind="message", content="private"),
            JobGuidance(id="guest-guidance", owner_id=self.guest_id, job_id=job.id, content="private"),
            Attachment(id="guest-upload", owner_id=self.guest_id, thread_id=thread.id, turn_id=turn.id,
                       storage_name=upload.name, original_name="original.txt"),
            Artifact(id="guest-artifact", owner_id=self.guest_id, run_id=job.id, turn_id=turn.id,
                     filename=export.name, expires_at=now),
            ToolApproval(id="approval", user_id=self.guest_id, agent_id=a.id, run_id=job.id,
                         scope="once", token_hash="approval", expires_at=now),
            GuardrailReview(user_id=self.guest_id, agent_id=a.id, summary="private", status="approved", expires_at=now),
            MemoryEntry(owner_id=self.guest_id, scope="context", thread_id=thread.id, title="private", content="private"),
            ApiKey(created_by=self.guest_id, name="fixture", key_hash="hash", prefix="sk-test"),
            TokenUsage(user_id=self.guest_id, username=self.guest.username, run_id=job.id, agent_id=a.id, provider_id=p.id,
                       model="fixture-model", input_tokens=100, output_tokens=20, total_tokens=120),
            AuditLog(user_id=self.guest_id, username=self.guest.username, role="guest", method="POST", path="/api/v1/chat",
                     status_code=200, ip="203.0.113.5"),
        ])
        self.db.execute(text("CREATE TABLE conversations (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), content TEXT)"))
        self.db.execute(text("INSERT INTO conversations VALUES (1, :id, 'old private')"), {"id": self.guest_id})
        self.db.execute(text("CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, content TEXT)"))
        self.db.execute(text("INSERT INTO run_events VALUES (1, 'guest-job', 'old private')"))
        self.db.commit()
        return dict(provider=p, agent=a, project=project, thread=thread, turn=turn, job=job,
                    upload=upload, export=export, workspace=workspace)

    def test_bulk_cleans_full_graph_and_anonymizes_financial_and_operation_audit(self):
        graph = self.graph()
        response = self.client.delete("/api/v1/users/guests")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        for field in ("users", "threads", "turns", "jobs", "attachments", "artifacts", "personal_models", "agents",
                      "sessions", "projects", "items", "guidance", "approvals", "reviews", "memories",
                      "harness_versions", "api_keys", "token_limits", "legacy_conversations", "legacy_events", "workspaces"):
            self.assertEqual(result[f"deleted_{field}"], 1, field)
        self.assertEqual(result["deleted_files"], 3)
        self.assertEqual(result["retained_token_usage"], 1)
        self.assertEqual(result["retained_audit_logs"], 1)
        self.assertEqual(result["cleanup_warnings"], [])
        for model in (Agent, ModelProvider, AuthSession, UserTokenLimit, Project, Thread, Turn, Job, Item,
                      Attachment, Artifact, JobGuidance, ToolApproval, GuardrailReview, MemoryEntry, HarnessVersion, ApiKey):
            self.assertEqual(self.db.query(model).count(), 0, model.__name__)
        self.assertEqual(self.db.query(User).count(), 2)
        self.assertIsNotNone(self.db.get(User, self.member.id))
        usage = self.db.query(TokenUsage).one()
        self.assertEqual((usage.user_id, usage.agent_id, usage.provider_id, usage.username, usage.run_id),
                         (None, None, None, cleanup.ANONYMOUS_NAME, ""))
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.model), (100, 20, 120, "fixture-model"))
        audit = self.db.query(AuditLog).one()
        self.assertEqual((audit.user_id, audit.username, audit.ip), (None, cleanup.ANONYMOUS_NAME, ""))
        self.assertEqual((audit.method, audit.path, audit.status_code), ("POST", "/api/v1/chat", 200))
        self.assertFalse(graph["upload"].exists())
        self.assertFalse(graph["export"].exists())
        self.assertFalse(graph["workspace"].exists())
        self.assertEqual(self.db.execute(text("PRAGMA foreign_key_check")).all(), [])

    def test_bulk_and_individual_routes_are_root_only(self):
        for identity, expected in ((None, 401), (self.member, 403), (self.guest, 403)):
            self.identity = identity
            for path in ("/api/v1/users/guests", f"/api/v1/users/{self.guest_id}"):
                self.assertEqual(self.client.delete(path).status_code, expected)
        self.assertIsNotNone(self.db.get(User, self.guest_id))

    def test_individual_guest_uses_same_cascade_but_registered_user_keeps_409(self):
        self.graph()
        self.db.add(Project(user_id=self.member.id, name="keep registered"))
        self.db.commit()
        self.assertEqual(self.client.delete(f"/api/v1/users/{self.member.id}").status_code, 409)
        response = self.client.delete(f"/api/v1/users/{self.guest_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deleted_users"], 1)
        self.assertEqual(self.db.query(Project).one().user_id, self.member.id)

    def test_normal_empty_user_delete_still_returns_204(self):
        self.assertEqual(self.client.delete(f"/api/v1/users/{self.member.id}").status_code, 204)

    def test_active_jobs_refuse_the_entire_batch_for_each_nonterminal_state(self):
        graph = self.graph()
        other_guest = User(username="second", password_hash="unused", role="guest")
        self.db.add(other_guest)
        self.db.commit()
        for status in ("pending", "running", "awaiting_approval", "unknown_future_state"):
            graph["job"].status = status
            graph["job"].cancel_requested = True
            self.db.commit()
            response = self.client.delete("/api/v1/users/guests")
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.db.query(User).filter(User.role == "guest").count(), 2)
            self.assertTrue(graph["upload"].exists())

    def test_foreign_owner_memory_cascade_is_blocked(self):
        graph = self.graph()
        self.db.add(MemoryEntry(owner_id=self.member.id, scope="project", project_id=graph["project"].id,
                                title="keep", content="another owner"))
        self.db.commit()
        response = self.client.delete("/api/v1/users/guests")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.db.query(MemoryEntry).count(), 2)
        self.assertTrue(graph["upload"].exists())

    def test_foreign_owner_turn_in_guest_thread_is_blocked(self):
        graph = self.graph()
        self.db.add(Turn(id="foreign-turn", owner_id=self.member.id, thread_id=graph["thread"].id, sequence=2, status="completed"))
        self.db.commit()
        self.assertEqual(self.client.delete("/api/v1/users/guests").status_code, 409)
        self.assertEqual(self.db.query(Turn).count(), 2)

    def test_foreign_json_model_role_reference_blocks_deletion(self):
        graph = self.graph()
        self.db.add(Agent(name="member-agent", created_by=self.member.id,
                          routing=json.dumps({"roles": {"planner": {"provider_id": graph["provider"].id}}})))
        self.db.commit()
        response = self.client.delete("/api/v1/users/guests")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.db.query(ModelProvider).count(), 1)

    def test_shared_upload_export_and_job_payload_paths_are_preserved(self):
        graph = self.graph()
        thread = Thread(id="member-thread", owner_id=self.member.id)
        self.db.add(thread)
        self.db.flush()
        self.db.add_all([
            Attachment(id="shared-upload", owner_id=self.member.id, thread_id=thread.id, storage_name=graph["upload"].name, original_name="shared"),
            Artifact(id="shared-export", owner_id=self.member.id, filename=graph["export"].name, expires_at=dt.datetime.now(dt.timezone.utc)),
            Job(id="member-job", owner_id=self.member.id, status="done", payload=json.dumps({"attachment_documents": [str(graph["upload"])]})),
        ])
        self.db.commit()
        result = cleanup.cleanup_guest_users(self.db, self.root)
        self.assertEqual(result["deleted_files"], 0)
        self.assertTrue(graph["upload"].exists())
        self.assertTrue(graph["upload"].with_suffix(".name").exists())
        self.assertTrue(graph["export"].exists())
        self.assertEqual(self.db.query(Job).one().id, "member-job")

    def test_commit_failure_rolls_back_every_record_and_keeps_all_files(self):
        graph = self.graph()
        with patch.object(self.db, "commit", side_effect=IntegrityError("fixture", {}, Exception("failure"))), \
             patch.object(cleanup, "_cleanup_files") as physical:
            with self.assertRaises(HTTPException) as caught:
                cleanup.cleanup_guest_users(self.db, self.root)
        self.assertEqual(caught.exception.status_code, 409)
        physical.assert_not_called()
        self.assertIsNotNone(self.db.get(User, self.guest_id))
        self.assertEqual(self.db.query(Attachment).count(), 1)
        self.assertEqual(self.db.query(TokenUsage).one().user_id, self.guest_id)
        self.assertTrue(graph["upload"].exists())
        self.assertTrue(graph["workspace"].exists())

    def test_file_failure_is_reported_after_successful_database_commit(self):
        graph = self.graph()
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == graph["upload"]:
                raise PermissionError("fixture")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            result = cleanup.cleanup_guest_users(self.db, self.root)
        self.assertEqual(result["deleted_users"], 1)
        self.assertEqual(result["deleted_files"], 2)
        self.assertTrue(result["cleanup_warnings"])
        self.assertIsNone(self.db.get(User, self.guest_id))
        self.assertTrue(graph["upload"].exists())

    def test_workspace_shared_by_surviving_job_is_retained_with_warning(self):
        graph = self.graph()
        self.db.add(Job(id="shared-workspace-job", owner_id=self.member.id, status="done",
                        payload=json.dumps({"workspace": str(graph["workspace"])})))
        self.db.commit()
        result = cleanup.cleanup_guest_users(self.db, self.root)
        self.assertEqual(result["deleted_workspaces"], 0)
        self.assertTrue(graph["workspace"].exists())
        self.assertTrue(result["cleanup_warnings"])

    def test_malicious_attachment_path_cannot_delete_outside_upload_root(self):
        graph = self.graph()
        outside = self.rootdir / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        self.db.query(Attachment).one().storage_name = "../outside.txt"
        self.db.commit()
        cleanup.cleanup_guest_users(self.db, self.root)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_role_restoration_and_cleanup_share_one_transaction(self):
        self.db.add(Project(user_id=self.member.id, name="restore-before-cleanup"))
        self.db.commit()
        self.member.role = "guest"
        result = cleanup.cleanup_guest_users(self.db, self.root, [self.member.id])
        self.assertEqual(result["deleted_users"], 1)
        self.assertEqual(result["deleted_projects"], 1)
        self.assertIsNotNone(self.db.get(User, self.guest_id))

    def test_selected_registered_user_and_upgraded_guest_are_never_cascaded(self):
        self.guest.role = "user"
        self.db.commit()
        for target in (self.member.id, self.guest_id):
            with self.assertRaises(HTTPException) as caught:
                cleanup.cleanup_guest_users(self.db, self.root, [target])
            self.assertEqual(caught.exception.status_code, 409)
        result = cleanup.cleanup_guest_users(self.db, self.root)
        self.assertEqual(result["deleted_users"], 0)
        self.assertEqual(self.db.query(User).count(), 3)

    def test_guest_owned_knowledge_is_explicitly_blocked(self):
        (cleanup.KNOWLEDGE_DIR / "_datasets.json").write_text(
            json.dumps({"private": {"created_by": self.guest_id}}), encoding="utf-8")
        response = self.client.delete("/api/v1/users/guests")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNotNone(self.db.get(User, self.guest_id))

    def test_query_path_is_not_file_ownership_evidence(self):
        graph = self.graph()
        unowned = cleanup.UPLOAD_DIR / "someone-else.txt"
        unowned.write_text("must remain", encoding="utf-8")
        graph["job"].payload = json.dumps({"query": str(unowned), "inputs": {"query": str(unowned)}})
        self.db.commit()
        cleanup.cleanup_guest_users(self.db, self.root)
        self.assertEqual(unowned.read_text(encoding="utf-8"), "must remain")

    def test_legacy_only_export_reference_is_preserved_and_expired_job_events_removed(self):
        graph = self.graph()
        self.db.execute(text("ALTER TABLE conversations ADD COLUMN export_files TEXT"))
        self.db.execute(text("INSERT INTO conversations VALUES (2, :id, 'keep', :files)"),
                        {"id": self.member.id, "files": json.dumps([graph["export"].name])})
        self.db.execute(text("INSERT INTO run_events VALUES (2, 'guest-turn', 'legacy turn event')"))
        self.db.commit()
        result = cleanup.cleanup_guest_users(self.db, self.root)
        self.assertTrue(graph["export"].exists())
        self.assertEqual(result["deleted_legacy_events"], 2)
        self.assertEqual(self.db.execute(text("SELECT user_id FROM conversations")).scalar(), self.member.id)

    def test_malformed_non_reference_json_does_not_raise_type_error(self):
        self.graph()
        self.db.add(Agent(name="malformed-other", created_by=self.member.id,
                          routing=json.dumps({"provider_id": [], "roles": {"planner": {"provider_id": {}}}})))
        self.db.commit()
        result = cleanup.cleanup_guest_users(self.db, self.root)
        self.assertEqual(result["deleted_users"], 1)
        self.assertEqual(self.db.query(Agent).one().name, "malformed-other")

    def test_structured_legacy_export_candidates_and_item_only_shared_references(self):
        graph = self.graph()
        legacy_file = cleanup.EXPORT_DIR / "legacy-only.txt"
        legacy_file.write_text("private", encoding="utf-8")
        item_file = cleanup.EXPORT_DIR / "item-only.txt"
        item_file.write_text("private", encoding="utf-8")
        self.db.execute(text("ALTER TABLE conversations ADD COLUMN export_files TEXT"))
        self.db.execute(text("UPDATE conversations SET export_files=:files"), {"files": json.dumps([legacy_file.name])})
        self.db.query(Item).one().payload = json.dumps({"legacy_export_files": [item_file.name]})
        thread = Thread(id="survivor-thread", owner_id=self.member.id)
        self.db.add(thread)
        self.db.flush()
        turn = Turn(id="survivor-turn", owner_id=self.member.id, thread_id=thread.id, sequence=1, status="completed")
        self.db.add(turn)
        self.db.flush()
        self.db.add(Item(id="survivor-item", turn_id=turn.id, thread_id=thread.id, sequence=1, kind="message",
                         payload=json.dumps({"legacy_export_files": [graph["export"].name]})))
        self.db.commit()
        result = cleanup.cleanup_guest_users(self.db, self.root)
        self.assertFalse(legacy_file.exists())
        self.assertFalse(item_file.exists())
        self.assertTrue(graph["export"].exists())
        self.assertEqual(result["deleted_files"], 4)


if __name__ == "__main__":
    unittest.main()
