"""Scope isolation, owner authorization, persistence, and real memory composition."""
import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.memories import router
from backend.api.chat import _build_memory
from backend.database import Base, get_db
from backend.memory_store import MemoryEntry, explicit_candidates
from backend.models import Agent, Project, Thread, User
from backend.runtime.memory import select_recall
from backend.security import get_current_user


class MemoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.user = User(id=1, username="owner", password_hash="x", role="admin", permissions='["memory"]')
        with self.sessions() as db:
            db.add_all([self.user, User(id=2, username="other", password_hash="x", role="admin"), User(id=3, username="root", password_hash="x", role="root")])
            db.flush()
            db.add_all([Agent(id=1, name="Phoenix", created_by=1, memory_enabled=True), Agent(id=2, name="Other personal agent", created_by=1, memory_enabled=True), Agent(id=3, name="Private other agent", created_by=2, memory_enabled=True)])
            db.add_all([Project(id=1, user_id=1, name="Phoenix"), Project(id=2, user_id=1, name="Other project"), Project(id=3, user_id=2, name="Private project")])
            db.flush()
            db.add_all([Thread(id="t1", owner_id=1, project_id=1, agent_id=1, title="Phoenix task"), Thread(id="t2", owner_id=1, project_id=1, agent_id=2), Thread(id="t3", owner_id=1, project_id=2, agent_id=1), Thread(id="foreign", owner_id=2, project_id=3, agent_id=3)])
            db.commit()
        self.user = User(id=1, username="owner", password_hash="x", role="admin", permissions='["memory"]')
        app = FastAPI(); app.include_router(router)
        def db_session():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = db_session
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close(); self.engine.dispose()

    def create(self, scope="global", **changes):
        response = self.client.post("/api/v1/memories", json={"scope": scope, "title": "Phoenix launch deadline", "content": "Phoenix launch deadline is Friday.", **changes})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_each_scope_resolves_only_to_its_target(self):
        global_row = self.create()
        custom = self.create("custom", agent_id=1)
        context = self.create("context", thread_id="t1")
        project = self.create("project", project_id=1)
        with self.sessions() as db:
            first = {row["memory_id"] for row in explicit_candidates(db, 1, session_id="t1", agent_id=1)}
            self.assertEqual(first, {global_row["id"], custom["id"], context["id"], project["id"]})
            sibling = {row["memory_id"] for row in explicit_candidates(db, 1, session_id="t2", agent_id=2)}
            self.assertEqual(sibling, {global_row["id"], project["id"]})
            elsewhere = {row["memory_id"] for row in explicit_candidates(db, 1, session_id="t3", agent_id=1)}
            self.assertEqual(elsewhere, {global_row["id"], custom["id"]})

    def test_root_and_other_account_cannot_read_mutate_or_recall_owned_memory(self):
        item = self.create()
        for user_id, role in ((2, "admin"), (3, "root")):
            self.user = User(id=user_id, username=role, role=role, permissions='["memory"]')
            self.assertEqual(self.client.get("/api/v1/memories").json()["total"], 0)
            self.assertEqual(self.client.patch(f"/api/v1/memories/{item['id']}", json={"enabled": False}).status_code, 404)
            self.assertEqual(self.client.delete(f"/api/v1/memories/{item['id']}").status_code, 404)
            with self.sessions() as db:
                self.assertEqual(explicit_candidates(db, user_id, session_id="t1", agent_id=1), [])
                self.assertEqual(explicit_candidates(db, user_id, agent_id=1), [])

    def test_binding_other_users_targets_is_rejected_even_for_root(self):
        for scope, target in (("context", {"thread_id": "foreign"}), ("project", {"project_id": 3}), ("custom", {"agent_id": 3})):
            response = self.client.post("/api/v1/memories", json={"scope": scope, "title": "x", "content": "y", **target})
            self.assertEqual(response.status_code, 404)
        self.user = User(id=3, username="root", role="root")
        for scope, target in (("context", {"thread_id": "t1"}), ("project", {"project_id": 1})):
            self.assertEqual(self.client.post("/api/v1/memories", json={"scope": scope, "title": "x", "content": "y", **target}).status_code, 404)

    def test_targets_do_not_reveal_other_users_conversations_or_projects(self):
        result = self.client.get("/api/v1/memories/targets").json()
        self.assertEqual({row["id"] for row in result["projects"]}, {1, 2})
        self.assertEqual({row["id"] for row in result["threads"]}, {"t1", "t2", "t3"})
        self.assertNotIn(3, {row["id"] for row in result["agents"]})

    def test_disabled_deleted_and_excluded_contexts_do_not_recall(self):
        disabled = self.create(enabled=False)
        deleted = self.create()
        self.create("context", thread_id="t1")
        self.assertEqual(self.client.delete(f"/api/v1/memories/{deleted['id']}").status_code, 204)
        with self.sessions() as db:
            thread = db.get(Thread, "t1"); thread.memory_excluded = True; db.commit()
            self.assertEqual(explicit_candidates(db, 1, session_id="t1", agent_id=1), [])
            self.assertIsNotNone(db.get(MemoryEntry, disabled["id"]))

    def test_followup_runtime_uses_saved_and_updated_memory_and_respects_switches(self):
        entry = self.create()
        with self.sessions() as db:
            result = _build_memory(db, db.get(Agent, 1), 1, "Phoenix launch deadline", session_id="t1")
            self.assertIn("Friday", result["content"])
            self.assertEqual(result["sources"][0]["memory_id"], entry["id"])
            self.assertIn("当前请求", result["content"])
        self.client.patch(f"/api/v1/memories/{entry['id']}", json={"content": "Phoenix launch deadline is Monday."})
        with self.sessions() as db:
            agent = db.get(Agent, 1)
            result = _build_memory(db, agent, 1, "Phoenix launch deadline", session_id="t1")
            self.assertIn("Monday", result["content"])
            self.assertNotIn("Friday", result["content"])
            agent.memory_enabled = False
            self.assertEqual(_build_memory(db, agent, 1, "Phoenix", session_id="t1"), {})
            agent.memory_enabled = True
            db.get(Thread, "t1").memory_enabled = False
            self.assertTrue(_build_memory(db, agent, 1, "Phoenix", session_id="t1")["disabled_by_thread"])

    def test_scope_edit_clears_previous_target_and_search_treats_wildcards_literally(self):
        row = self.create("custom", agent_id=1, title="100% documented")
        updated = self.client.patch(f"/api/v1/memories/{row['id']}", json={"scope": "project", "project_id": 2})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertIsNone(updated.json()["agent_id"])
        self.assertEqual(updated.json()["project_id"], 2)
        self.create(title="Not a percent")
        self.assertEqual(self.client.get("/api/v1/memories", params={"q": "%"}).json()["total"], 1)
        self.assertEqual(self.client.get("/api/v1/memories", params={"scope": "project"}).json()["total"], 1)

    def test_empty_and_invalid_bindings_are_rejected(self):
        for payload in ({"title": " ", "content": "x"}, {"scope": "context", "title": "x", "content": "y"}, {"scope": "project", "project_id": 99, "title": "x", "content": "y"}):
            self.assertIn(self.client.post("/api/v1/memories", json=payload).status_code, (404, 422))

    def test_deleting_target_cascades_without_promoting_memory_to_global(self):
        row = self.create("project", project_id=1)
        with self.sessions() as db:
            db.delete(db.get(Project, 1)); db.commit()
            self.assertIsNone(db.get(MemoryEntry, row["id"]))
            self.assertEqual(explicit_candidates(db, 1, session_id="t1", agent_id=1), [])

    def test_explicit_memory_has_lower_weight_and_is_bounded(self):
        self.create(content="Phoenix launch deadline " * 250)
        with self.sessions() as db:
            candidates = explicit_candidates(db, 1, session_id="t1", agent_id=1)
            explicit = select_recall(candidates, "Phoenix launch deadline", max_chars=300)
            history = [{**candidates[0], "source_type": "conversation"}]
            historical = select_recall(history, "Phoenix launch deadline", max_chars=300)
            self.assertLess(explicit["max_score"], historical["max_score"])
            self.assertLess(len(explicit["content"]), 340)
            self.assertIn("低权重", explicit["content"])

    def test_ungranted_user_cannot_manage_memories(self):
        self.user.role = "user"; self.user.permissions = "[]"
        self.assertEqual(self.client.get("/api/v1/memories").status_code, 403)
        self.user.permissions = json.dumps(["memory"])
        self.assertEqual(self.client.get("/api/v1/memories").status_code, 200)


if __name__ == "__main__":
    unittest.main()
