"""Personal settings persist per account without changing resource/role permissions."""
import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.users import router
from backend.database import Base, get_db
from backend.models import User, Agent
from backend.security import get_current_user


class UserPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="preference-owner", password_hash="x", role="user")
        self.other = User(username="other", password_hash="x", role="root")
        self.db.add_all([self.user, self.other]); self.db.commit()
        self.app = FastAPI(); self.app.include_router(router)
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.app)
        self.url = "/api/v1/users/me/preferences"

    def tearDown(self):
        self.client.close(); self.db.close(); self.engine.dispose()

    def test_preferences_survive_new_session_and_are_owner_scoped(self):
        response = self.client.patch(self.url, json={"revision": 0, "theme": "dark", "approval_policy": "auto"})
        self.assertEqual(response.status_code, 200, response.text)
        self.db.expire_all()
        self.assertEqual(self.client.get(self.url).json()["theme"], "dark")
        self.assertEqual(self.other.preferences, "{}")
        self.assertEqual(self.user.permissions, "")

    def test_color_defaults_are_compatible_with_legacy_preferences(self):
        defaults = self.client.get(self.url).json()
        self.assertEqual((defaults["theme_color"], defaults["custom_color"]), ("default", "#8b5cf6"))
        self.user.preferences = json.dumps({
            "theme": "dark", "approval_policy": "auto", "recent_sort": "updated", "revision": 7,
        })
        self.db.commit()
        stored = self.user.preferences
        value = self.client.get(self.url).json()
        self.assertEqual((value["theme_color"], value["custom_color"]), ("default", "#8b5cf6"))
        self.assertEqual((value["theme"], value["approval_policy"], value["recent_sort"], value["revision"]),
                         ("dark", "auto", "updated", 7))
        self.assertEqual(self.user.preferences, stored, "Reading legacy JSON must not rewrite it")

    def test_each_color_preset_persists_and_reads_back(self):
        presets = ("default", "blue", "green", "yellow", "pink", "orange", "purple", "black", "custom")
        for revision, preset in enumerate(presets):
            with self.subTest(preset=preset):
                response = self.client.patch(self.url, json={"revision": revision, "theme_color": preset})
                self.assertEqual(response.status_code, 200, response.text)
                self.db.expire_all()
                value = self.client.get(self.url).json()
                self.assertEqual(value["theme_color"], preset)
                self.assertEqual(value["custom_color"], "#8b5cf6")
                self.assertEqual(value["revision"], revision + 1)

    def test_custom_color_is_normalized_and_survives_reload(self):
        response = self.client.patch(self.url, json={
            "revision": 0, "theme_color": "custom", "custom_color": "#A1b2C3",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["custom_color"], "#a1b2c3")
        self.db.expire_all()
        value = self.client.get(self.url).json()
        self.assertEqual((value["theme_color"], value["custom_color"]), ("custom", "#a1b2c3"))
        self.assertEqual(json.loads(self.user.preferences)["custom_color"], "#a1b2c3")

    def test_partial_appearance_update_preserves_color_and_other_settings(self):
        response = self.client.patch(self.url, json={
            "revision": 0, "theme_color": "custom", "custom_color": "#123ABC",
            "approval_policy": "auto", "recent_sort": "updated",
        })
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.patch(self.url, json={"revision": 1, "theme": "dark"})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual((value["theme"], value["theme_color"], value["custom_color"]),
                         ("dark", "custom", "#123abc"))
        self.assertEqual((value["approval_policy"], value["recent_sort"], value["revision"]),
                         ("auto", "updated", 2))
        response = self.client.patch(self.url, json={"revision": 2, "theme_color": "blue"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["custom_color"], "#123abc")
        self.assertEqual(response.json()["theme"], "dark")

    def test_invalid_color_preferences_are_rejected_without_writes(self):
        invalid = [{"theme_color": value} for value in (None, "", "red", "Custom", "var(--accent)", 1)]
        invalid += [{"custom_color": value} for value in (
            None, "", "#fff", "ffffff", "#12345678", "#gggggg", "rgb(1, 2, 3)",
            "var(--accent)", " #AABBCC", "#AABBCC ", "#AABBCC\n", 123456,
        )]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.client.patch(self.url, json={"revision": 0, **payload})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.client.get(self.url).json()["revision"], 0)
        self.assertEqual(self.user.preferences, "{}")

    def test_colors_are_isolated_between_accounts(self):
        response = self.client.patch(self.url, json={
            "revision": 0, "theme_color": "custom", "custom_color": "#123456",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.app.dependency_overrides[get_current_user] = lambda: self.other
        other = self.client.get(self.url).json()
        self.assertEqual((other["theme_color"], other["custom_color"], other["revision"]),
                         ("default", "#8b5cf6", 0))
        response = self.client.patch(self.url, json={"revision": 0, "theme_color": "green"})
        self.assertEqual(response.status_code, 200, response.text)
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.db.expire_all()
        own = self.client.get(self.url).json()
        self.assertEqual((own["theme_color"], own["custom_color"], own["revision"]),
                         ("custom", "#123456", 1))

    def test_stale_color_save_cannot_overwrite_newer_preferences(self):
        response = self.client.patch(self.url, json={
            "revision": 0, "theme": "dark", "theme_color": "custom", "custom_color": "#ABCDEF",
        })
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        response = self.client.patch(self.url, json={
            "revision": 0, "theme": "light", "theme_color": "pink", "custom_color": "#654321",
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get(self.url).json(), saved)

    def test_default_does_not_grant_full_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["approval_policy"], "ask")
        self.assertEqual(self.client.patch(self.url, json={"revision": 0, "approval_policy": "full_access"}).status_code, 403)
        self.assertEqual(self.client.get(self.url).json()["revision"], 0)

    def test_privilege_revocation_downgrades_stored_full_access(self):
        self.user.preferences = json.dumps({"revision": 2, "approval_policy": "full_access"})
        self.db.commit()
        self.assertEqual(self.client.get(self.url).json()["approval_policy"], "ask")
        response = self.client.patch(self.url, json={"revision": 2, "theme": "light"})
        self.assertEqual(response.json()["approval_policy"], "ask")

    def test_stale_save_cannot_overwrite_other_page(self):
        self.client.patch(self.url, json={"revision": 0, "theme": "dark"})
        response = self.client.patch(self.url, json={"revision": 0, "recent_sort": "updated"})
        self.assertEqual(response.status_code, 409)
        data = self.client.get(self.url).json()
        self.assertEqual((data["theme"], data["recent_sort"]), ("dark", "priority"))

    def test_private_and_disabled_default_agents_rejected(self):
        agent = Agent(name="private", created_by=self.other.id, enabled=True, is_public=False, is_default=False)
        self.db.add(agent); self.db.commit()
        response = self.client.patch(self.url, json={"revision": 0, "default_agent_id": agent.id})
        self.assertEqual(response.status_code, 403)
        agent.is_public = True; self.db.commit()
        response = self.client.patch(self.url, json={"revision": 0, "default_agent_id": agent.id})
        self.assertEqual(response.status_code, 200, response.text)
        agent.enabled = False; self.db.commit()
        self.assertIsNone(self.client.get(self.url).json()["default_agent_id"])

    def test_invalid_or_unknown_preferences_are_rejected(self):
        for payload in ({"theme": "dark"}, {"revision": 0, "theme": None}, {"revision": 0, "theme": "invalid"}, {"revision": 0, "role": "root"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch(self.url, json=payload).status_code, 422)

    def test_me_endpoint_requires_authentication(self):
        del self.app.dependency_overrides[get_current_user]
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.client.patch(self.url, json={"revision": 0, "theme": "dark"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
