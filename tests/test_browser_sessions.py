"""Browser sessions are opaque, server-revocable and separate from Bearer JWTs."""
import datetime
import hashlib
import importlib
from http.cookies import SimpleCookie
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import auth as auth_api, users as users_api
from backend.database import Base, get_db
from backend.models import Agent, AuthSession, ROLE_GUEST, User
from backend.security import (
    can_access_agent, create_browser_session, create_token, has_module_access,
    hash_password, resolve_access_token, resolve_session_token, revoke_browser_session,
)


class BrowserSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password("fixture-pass")

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory() as db:
            self.root = User(username="session-root", password_hash=self.password_hash, role="root")
            self.user = User(username="session-user", password_hash=self.password_hash, role="user")
            self.guest = User(username="guest-fixture", password_hash=self.password_hash, role=ROLE_GUEST)
            db.add_all([self.root, self.user, self.guest])
            db.commit()
            self.root_id, self.user_id, self.guest_id = self.root.id, self.user.id, self.guest.id
        app = FastAPI()
        app.include_router(auth_api.router)
        app.include_router(users_api.router)

        def database():
            with self.factory() as db:
                yield db

        app.dependency_overrides[get_db] = database
        self.app = app
        self.client = self.browser()
        for patcher in (patch.object(auth_api, "enforce"), patch.object(auth_api, "reset")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.dispose()

    def browser(self, scheme="https"):
        client = TestClient(self.app, base_url=f"{scheme}://testserver")
        self.addCleanup(client.close)
        return client

    def login(self, client=None, username="session-user", password="fixture-pass"):
        return (client or self.client).post("/api/v1/auth/login", json={"username": username, "password": password})

    def cookie_token(self, response):
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        return cookie[auth_api.settings.AUTH_COOKIE_NAME].value

    def issue(self, user_id):
        with self.factory() as db:
            user = db.get(User, user_id)
            old_role = user.role
            if old_role == ROLE_GUEST:
                user.role = "user"
            token = create_browser_session(db, user)
            user.role = old_role
            db.commit()
            return token

    def use_token(self, token, client=None):
        selected = client or self.browser()
        selected.cookies.set(auth_api.settings.AUTH_COOKIE_NAME, token)
        return selected

    def assert_invalid(self, token):
        with self.factory() as db, self.assertRaises(HTTPException) as raised:
            resolve_session_token(token, db)
        self.assertEqual(raised.exception.status_code, 401)

    def test_login_cookie_is_opaque_session_only_and_database_contains_only_digest(self):
        response = self.login()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], self.user_id)
        self.assertFalse(response.json()["is_guest"])
        self.assertNotIn("access_token", response.json())
        self.assertNotIn("token_type", response.json())
        cookie = response.headers["set-cookie"]
        for expected in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            self.assertIn(expected, cookie)
        self.assertNotIn("max-age", cookie.lower())
        self.assertNotIn("expires=", cookie.lower())
        token = self.cookie_token(response)
        self.assertRegex(token, r"^sess_[A-Za-z0-9_-]{43}$")
        self.assertNotIn(token, response.text)
        with self.factory() as db:
            row = db.query(AuthSession).one()
            self.assertEqual(row.token_hash, hashlib.sha256(token.encode()).hexdigest())
            self.assertEqual(row.user_id, self.user_id)
            self.assertEqual(row.token_version, 0)
            self.assertAlmostEqual((row.expires_at - row.created_at).total_seconds(),
                                   auth_api.settings.AUTH_SESSION_EXPIRE_MINUTES * 60, delta=1)
            values = db.execute(text("SELECT * FROM auth_sessions")).one()
            self.assertNotIn(token, str(values))
        self.assertEqual(self.client.get("/api/v1/auth/me").json()["id"], self.user_id)

    def test_local_http_cookie_and_secure_configuration(self):
        client = self.browser("http")
        with patch.object(auth_api.settings, "AUTH_COOKIE_SECURE", False):
            response = self.login(client)
        self.assertNotIn("Secure", response.headers["set-cookie"])
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
        with patch.object(auth_api.settings, "AUTH_COOKIE_SECURE", True):
            response = self.login(client)
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_cookie_rejects_jwt_and_bearer_retains_legacy_jwt_only(self):
        with self.factory() as db:
            jwt_token = create_token(db.get(User, self.user_id))
        client = self.use_token(jwt_token)
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)
        self.assertEqual(client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {jwt_token}"}).status_code, 200)
        opaque = self.issue(self.user_id)
        client = self.use_token(opaque)
        self.assertEqual(client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {opaque}"}).status_code, 401)
        self.assertEqual(client.get("/api/v1/auth/me", headers={"Authorization": "Bearer invalid"}).status_code, 401)
        self.assertEqual(self.browser().get("/api/v1/auth/me", params={"token": opaque}).status_code, 401)

    def test_login_rotates_preexisting_cookie_and_revokes_old_credential(self):
        first = self.cookie_token(self.login())
        second = self.cookie_token(self.login())
        self.assertNotEqual(first, second)
        self.assert_invalid(first)
        with self.factory() as db:
            self.assertEqual(db.query(AuthSession).count(), 1)
            self.assertEqual(resolve_session_token(second, db).id, self.user_id)

    def test_account_switch_revokes_previous_guest_session(self):
        guest_token = self.issue(self.guest_id)
        client = self.use_token(guest_token)
        response = self.login(client)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.cookie_token(response), guest_token)
        self.assert_invalid(guest_token)
        self.assertEqual(client.get("/api/v1/auth/me").json()["id"], self.user_id)

    def test_failed_login_does_not_revoke_existing_session(self):
        first = self.cookie_token(self.login())
        response = self.login(password="wrong")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        with self.factory() as db:
            self.assertEqual(resolve_session_token(first, db).id, self.user_id)

    def test_logout_revokes_only_current_browser_and_preserves_other_sessions_and_jwt(self):
        current = self.cookie_token(self.login())
        other_browser = self.browser()
        other = self.cookie_token(self.login(other_browser))
        with self.factory() as db:
            jwt_token = create_token(db.get(User, self.user_id))
        response = self.client.post("/api/v1/auth/logout")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assert_invalid(current)
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)
        with self.factory() as db:
            self.assertEqual(resolve_session_token(other, db).id, self.user_id)
            self.assertEqual(resolve_access_token(jwt_token, db).id, self.user_id)
            self.assertEqual(db.get(User, self.user_id).token_version, 0)

    def test_logout_is_idempotent_with_invalid_or_missing_cookie(self):
        for token in ("", "not-a-session", "sess_" + "A" * 43):
            with self.subTest(token=token):
                client = self.use_token(token)
                self.assertEqual(client.post("/api/v1/auth/logout").status_code, 204)
                self.assertEqual(client.post("/api/v1/auth/logout").status_code, 204)

    def test_server_expiration_is_enforced_even_if_browser_keeps_cookie(self):
        token = self.issue(self.user_id)
        with self.factory() as db:
            row = db.query(AuthSession).one()
            row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.use_token(token).get("/api/v1/auth/me").status_code, 401)

    def test_password_change_revokes_all_browser_sessions_and_legacy_jwt(self):
        first = self.cookie_token(self.login())
        second = self.issue(self.user_id)
        with self.factory() as db:
            jwt_token = create_token(db.get(User, self.user_id))
        response = self.client.post("/api/v1/auth/change-password", json={"old_password": "fixture-pass", "new_password": "updated-pass"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_invalid(first)
        self.assert_invalid(second)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        with self.factory() as db:
            self.assertEqual(db.query(AuthSession).filter_by(user_id=self.user_id).count(), 0)
            with self.assertRaises(HTTPException):
                resolve_access_token(jwt_token, db)
        self.assertEqual(self.login(password="updated-pass").status_code, 200)

    def test_wrong_password_change_preserves_session(self):
        token = self.cookie_token(self.login())
        response = self.client.post("/api/v1/auth/change-password", json={"old_password": "wrong-pass", "new_password": "updated-pass"})
        self.assertEqual(response.status_code, 400)
        with self.factory() as db:
            self.assertEqual(resolve_session_token(token, db).id, self.user_id)

    def test_admin_disable_and_reenable_does_not_resurrect_session(self):
        token = self.cookie_token(self.login())
        root_browser = self.use_token(self.issue(self.root_id))
        self.assertEqual(root_browser.patch(f"/api/v1/users/{self.user_id}", json={"is_active": False}).status_code, 200)
        self.assert_invalid(token)
        self.assertEqual(root_browser.patch(f"/api/v1/users/{self.user_id}", json={"is_active": True}).status_code, 200)
        self.assert_invalid(token)
        self.assertEqual(self.login().status_code, 200)

    def test_admin_password_permission_and_role_changes_revoke_sessions(self):
        root_browser = self.use_token(self.issue(self.root_id))
        for change in ({"password": "reset-pass"}, {"modules": ["tools"]}, {"role": "admin"}):
            with self.subTest(change=change):
                token = self.issue(self.user_id)
                response = root_browser.patch(f"/api/v1/users/{self.user_id}", json=change)
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_invalid(token)

    def test_deleted_user_invalidates_cookie_and_cascades_session_record(self):
        token = self.issue(self.user_id)
        root_browser = self.use_token(self.issue(self.root_id))
        response = root_browser.delete(f"/api/v1/users/{self.user_id}")
        self.assertEqual(response.status_code, 204, response.text)
        self.assert_invalid(token)
        with self.factory() as db:
            self.assertEqual(db.query(AuthSession).filter_by(user_id=self.user_id).count(), 0)

    def test_resolver_refreshes_cached_session_and_user_before_authorizing(self):
        token = self.issue(self.user_id)
        with self.factory() as cached:
            self.assertEqual(resolve_session_token(token, cached).id, self.user_id)
            with self.factory() as other:
                revoke_browser_session(other, token)
                other.commit()
            with self.assertRaises(HTTPException):
                resolve_session_token(token, cached)
        token = self.issue(self.user_id)
        with self.factory() as cached:
            resolve_session_token(token, cached)
            with self.factory() as other:
                other.get(User, self.user_id).token_version += 1
                other.commit()
            with self.assertRaises(HTTPException):
                resolve_session_token(token, cached)

    def test_guest_session_has_no_management_modules_and_cannot_change_password_or_login(self):
        client = self.use_token(self.issue(self.guest_id))
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)
        self.assertEqual(client.get("/api/v1/users").status_code, 401)
        self.assertEqual(client.post("/api/v1/auth/change-password", json={"old_password": "fixture-pass", "new_password": "new-pass"}).status_code, 401)
        self.assertEqual(self.login(client, username="guest-fixture").status_code, 401)
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)

    def test_guest_cannot_access_even_own_dedicated_agent(self):
        with self.factory() as db:
            guest = db.get(User, self.guest_id)
            allowed = Agent(name=f"__guest_agent_{guest.id}", created_by=guest.id, enabled=True)
            self.assertFalse(can_access_agent(guest, allowed))
            for agent in (
                Agent(name="shared", enabled=True, is_public=True, is_default=True),
                Agent(name="other-owned", enabled=True, created_by=guest.id),
                Agent(name=f"__guest_agent_{guest.id}", enabled=True, created_by=self.user_id),
                Agent(name=f"__guest_agent_{guest.id}", enabled=False, created_by=guest.id),
            ):
                self.assertFalse(can_access_agent(guest, agent))
            guest.permissions = '["providers", "agents"]'
            self.assertFalse(has_module_access(guest, "providers"))
            self.assertEqual(users_api.user_out(guest).modules, [])


class BrowserSessionMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_preserves_existing_users(self):
        migration = importlib.import_module("migrations.versions.0026_browser_sessions")
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
                connection.execute(text("INSERT INTO users VALUES (7, 'preserved')"))
                operations = Operations(MigrationContext.configure(connection))
                with patch.object(migration, "op", operations):
                    migration.upgrade()
                    migration.upgrade()
                inspector = inspect(connection)
                self.assertEqual({column["name"] for column in inspector.get_columns("auth_sessions")},
                                 {"token_hash", "user_id", "token_version", "created_at", "expires_at"})
                self.assertEqual({index["name"] for index in inspector.get_indexes("auth_sessions")},
                                 {"ix_auth_sessions_user_id", "ix_auth_sessions_expires_at"})
                self.assertEqual(inspector.get_foreign_keys("auth_sessions")[0]["options"]["ondelete"], "CASCADE")
                with patch.object(migration, "op", operations):
                    migration.downgrade()
                self.assertEqual(connection.execute(text("SELECT username FROM users WHERE id=7")).scalar_one(), "preserved")
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
