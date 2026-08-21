"""设置页模块授权回归测试。"""
import json
import inspect
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.token_usage import (
    reset_user_monthly_usage,
    reset_user_weekly_usage,
    token_usage_summary,
    update_user_token_limits,
)
from backend.api.capabilities import (
    create_schedule, delete_schedule, list_schedules, update_schedule,
)
from backend.api.users import list_modules, user_out
from backend.database import Base
from backend.models import ROLE_ADMIN, ROLE_ROOT, ROLE_USER, TokenUsage, User
from backend.security import has_module_access, require_module, require_root


class ModulePermissionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_regular_users_default_to_no_settings_modules(self):
        user = User(
            id=1, username="user", password_hash="x", role=ROLE_USER,
            permissions="", is_active=True,
        )
        output = user_out(user)
        self.assertFalse(output.all_modules)
        self.assertEqual(output.modules, [])
        self.assertFalse(has_module_access(user, "knowledge"))
        with self.assertRaises(HTTPException) as raised:
            require_module("knowledge")(user)
        self.assertEqual(raised.exception.status_code, 403)

    def test_regular_user_can_only_receive_common_modules(self):
        user = User(
            id=1,
            username="user",
            password_hash="x",
            role=ROLE_USER,
            permissions=json.dumps(["knowledge", "templates", "token_usage", "agents"]),
            is_active=True,
        )
        self.assertTrue(has_module_access(user, "knowledge"))
        self.assertTrue(has_module_access(user, "templates"))
        self.assertTrue(has_module_access(user, "token_usage"))
        self.assertFalse(has_module_access(user, "agents"))
        self.assertEqual(
            set(user_out(user).modules),
            {"knowledge", "templates", "token_usage"},
        )

    def test_template_module_is_assignable_to_regular_users(self):
        modules = {item["key"]: item for item in list_modules(None)}
        self.assertEqual(modules["templates"]["roles"], [ROLE_ADMIN, ROLE_USER])

    def test_legacy_admin_empty_permissions_still_means_all(self):
        admin = User(
            id=1, username="admin", password_hash="x", role=ROLE_ADMIN,
            permissions="", is_active=True,
        )
        self.assertTrue(user_out(admin).all_modules)
        self.assertTrue(has_module_access(admin, "providers"))
        self.assertTrue(has_module_access(admin, "token_usage"))

    def test_non_root_token_dashboard_is_scoped_to_current_user(self):
        user = User(
            username="user",
            password_hash="x",
            role=ROLE_USER,
            permissions=json.dumps(["token_usage"]),
        )
        other = User(username="other", password_hash="x", role=ROLE_USER)
        root = User(username="root", password_hash="x", role=ROLE_ROOT)
        self.db.add_all([user, other, root])
        self.db.flush()
        self.db.add_all([
            TokenUsage(user_id=user.id, model="own", total_tokens=10),
            TokenUsage(user_id=other.id, model="other", total_tokens=90),
        ])
        self.db.commit()

        own = token_usage_summary(days=0, user_id=other.id, _=user, db=self.db)
        self.assertEqual(own["totals"]["total_tokens"], 10)
        self.assertEqual([row["username"] for row in own["users"]], ["user"])
        self.assertEqual([row["username"] for row in own["user_options"]], ["user"])

        all_users = token_usage_summary(days=0, user_id=None, _=root, db=self.db)
        self.assertEqual(all_users["totals"]["total_tokens"], 100)

    def test_token_usage_write_endpoints_require_root(self):
        for endpoint in (
            update_user_token_limits,
            reset_user_weekly_usage,
            reset_user_monthly_usage,
        ):
            dependency = inspect.signature(endpoint).parameters["_"].default
            self.assertIs(dependency.dependency, require_root)

    def test_schedule_endpoints_enforce_schedule_module(self):
        user = User(
            id=1, username="user", password_hash="x", role=ROLE_USER,
            permissions="", is_active=True,
        )
        for endpoint in (
            list_schedules, create_schedule, update_schedule, delete_schedule,
        ):
            dependency = inspect.signature(endpoint).parameters["user"].default.dependency
            with self.assertRaises(HTTPException) as raised:
                dependency(user)
            self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
