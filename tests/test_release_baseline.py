import contextlib
import importlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.api.chat import create_project, delete_project, update_project
from backend.database import Base
from backend.models import Project, User
from backend.resource_governance import (
    record_version, resource_state, update_resource_state, versions,
)
from backend.schemas import ProjectCreate, ProjectUpdate
from tools import release_preflight, sqlite_backup


class ReleaseBaselineTests(unittest.TestCase):
    def test_github_ci_triggers_and_pins_browser_cli(self):
        workflow = (
            release_preflight.ROOT / ".github" / "workflows" /
            "security-regression.yml"
        ).read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^on:\s*$")
        for trigger in ("push:", "pull_request:", "workflow_dispatch:"):
            self.assertIn(trigger, workflow)
        self.assertIn("ubuntu-latest", workflow)
        self.assertIn("windows-latest", workflow)
        self.assertIn("python tools/release_preflight.py --skip-database", workflow)
        self.assertIn("Prepare isolated runtime fixtures", workflow)
        self.assertIn("APP_DATA_DIR'])/'branding'/'favicon.svg'", workflow)
        smoke = (
            release_preflight.ROOT / "tools" / "playwright_smoke.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('@playwright/cli@0.1.18', smoke)
        self.assertNotIn("--package '@playwright/cli'", smoke)

    def test_generated_runtime_artifacts_are_ignored(self):
        result = release_preflight.check_git(allow_unborn=True, allow_dirty=True)
        ignored = next(item for item in result if item.name == "generated-artifacts-ignore")
        self.assertTrue(ignored.ok, ignored.message)

    def test_preflight_explicit_ci_skips_do_not_print_secret_values(self):
        secret = release_preflight.settings.JWT_SECRET
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = release_preflight.main([
                "--skip-production-config", "--skip-database",
                "--allow-unborn", "--allow-dirty",
            ])
        self.assertEqual(status, 0, stream.getvalue())
        if secret:
            self.assertNotIn(secret, stream.getvalue())

    def test_sqlite_online_backup_and_restore_drill_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, backup, restored = root / "source.db", root / "backup.db", root / "restore.db"
            live = sqlite3.connect(source)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
            live.execute(
                "INSERT INTO alembic_version VALUES (?)",
                (sqlite_backup.EXPECTED_SCHEMA_REVISION,),
            )
            live.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)")
            live.execute("INSERT INTO evidence(value) VALUES ('committed-through-wal')")
            live.commit()
            metadata = sqlite_backup.online_copy(source, backup)
            self.assertEqual(metadata["revision"], sqlite_backup.EXPECTED_SCHEMA_REVISION)
            self.assertEqual(len(metadata["sha256"]), 64)
            self.assertGreater(metadata["size_bytes"], 0)
            sqlite_backup.online_copy(backup, restored)
            check = sqlite3.connect(restored)
            try:
                self.assertEqual(
                    check.execute("SELECT value FROM evidence").fetchone()[0],
                    "committed-through-wal",
                )
            finally:
                check.close()
            with self.assertRaises(FileExistsError):
                sqlite_backup.online_copy(source, backup)
            live.close()


class ProjectDefaultConstraintTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="owner", password_hash="x", role="user", is_active=True)
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_fresh_schema_enforces_one_default_per_user(self):
        self.db.add_all([
            Project(user_id=self.user.id, name="one", is_default=True),
            Project(user_id=self.user.id, name="two", is_default=True),
        ])
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_deleting_default_selects_unarchived_successor_in_same_transaction(self):
        first = create_project(ProjectCreate(name="first"), self.user, self.db)
        second = create_project(ProjectCreate(name="second"), self.user, self.db)
        create_project(ProjectCreate(name="archived"), self.user, self.db)
        archived = self.db.query(Project).filter_by(name="archived").one()
        archived.is_archived = True
        self.db.commit()
        delete_project(first["id"], self.user, self.db)
        successor = self.db.query(Project).filter(Project.is_default.is_(True)).one()
        self.assertEqual(successor.id, second["id"])
        self.assertFalse(successor.is_archived)

    def test_archiving_default_selects_unarchived_successor(self):
        first = create_project(ProjectCreate(name="first"), self.user, self.db)
        second = create_project(ProjectCreate(name="second"), self.user, self.db)
        result = update_project(
            first["id"], ProjectUpdate(archived=True), self.user, self.db
        )
        self.assertTrue(result["archived"])
        self.assertFalse(result["default"])
        successor = self.db.query(Project).filter(Project.is_default.is_(True)).one()
        self.assertEqual(successor.id, second["id"])
        self.assertFalse(successor.is_archived)

    def test_concurrent_creates_leave_exactly_one_default(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = create_engine(
                f"sqlite:///{(Path(temp) / 'projects.db').as_posix()}",
                connect_args={"timeout": 10},
            )
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine)
            setup = factory()
            owner = User(username="concurrent", password_hash="x", role="user", is_active=True)
            setup.add(owner)
            setup.commit()
            owner_id = owner.id
            setup.close()

            def create(name):
                db = factory()
                try:
                    user = db.get(User, owner_id)
                    return create_project(ProjectCreate(name=name), user, db)
                finally:
                    db.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                created = list(pool.map(create, ("one", "two")))
            check = factory()
            self.assertEqual(len(created), 2)
            self.assertEqual(
                check.query(Project).filter(Project.is_default.is_(True)).count(), 1
            )
            check.close()
            engine.dispose()


class ResourceGovernanceMigrationTests(unittest.TestCase):
    def test_0019_migrates_state_without_versions_and_keeps_per_item_counts(self):
        migration = importlib.import_module("migrations.versions.0019_release_reliability")
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE app_settings (key VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL)"
            ))
            states = {
                "mcp:4": {
                    "catalog_hash": "valid", "review_required": False,
                    "tool_count": 7,
                },
                "mcp:5": {
                    "catalog_hash": "invalid", "review_required": True,
                    "tool_count": "not-a-number",
                },
            }
            # Deliberately omit VERSIONS_KEY: this covers the former unbound
            # tool_count path and proves each state parses its own value.
            connection.execute(text(
                "INSERT INTO app_settings (key, value) VALUES (:key, :value)"
            ), {"key": migration.STATE_KEY, "value": json.dumps(states)})
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
            rows = connection.execute(text(
                "SELECT resource_id, tool_count, review_required "
                "FROM resource_governance_states ORDER BY resource_id"
            )).all()
            self.assertEqual(rows, [(4, 7, 0), (5, 0, 1)])
            self.assertEqual(connection.execute(text(
                "SELECT COUNT(*) FROM resource_versions"
            )).scalar_one(), 0)
        engine.dispose()

    def test_0018_upgrade_normalizes_defaults_and_sanitizes_legacy_json(self):
        migration = importlib.import_module("migrations.versions.0019_release_reliability")
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE projects (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "name VARCHAR(80) NOT NULL, is_default BOOLEAN NOT NULL DEFAULT 0)"
            ))
            connection.execute(text(
                "INSERT INTO projects VALUES (1, 7, 'one', 1), (2, 7, 'two', 1)"
            ))
            connection.execute(text(
                "CREATE TABLE app_settings (key VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL)"
            ))
            legacy_versions = [{
                "id": "legacy-one", "resource_type": "mcp", "resource_id": 9,
                "version": 99, "change": "imported",
                "snapshot": {
                    "name": "safe", "headers": {"Authorization": "Bearer exposed"},
                    "accessToken": "exposed", "x-api-key": "exposed",
                    "url": "https://example.invalid/?token=exposed",
                    "reasoning": "private", "nested": {"auth_token": "exposed"},
                },
            }]
            legacy_state = {"mcp:9": {
                "catalog_hash": "abc", "review_required": True,
                "tool_count": "not-a-number",
            }}
            connection.execute(text(
                "INSERT INTO app_settings VALUES (:versions_key, :versions), (:state_key, :state)"
            ), {
                "versions_key": migration.VERSIONS_KEY,
                "versions": json.dumps(legacy_versions),
                "state_key": migration.STATE_KEY,
                "state": json.dumps(legacy_state),
            })
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
            migrated = json.loads(connection.execute(text(
                "SELECT snapshot FROM resource_versions"
            )).scalar_one())
            serialized = json.dumps(migrated).lower()
            for forbidden in ("exposed", "headers", "accesstoken", "api-key", "reasoning", "url", "auth_token"):
                self.assertNotIn(forbidden, serialized)
            self.assertEqual(migrated["name"], "safe")
            self.assertEqual(connection.execute(text(
                "SELECT tool_count FROM resource_governance_states"
            )).scalar_one(), 0)
            self.assertEqual(connection.execute(text(
                "SELECT COUNT(*) FROM projects WHERE user_id=7 AND is_default=1"
            )).scalar_one(), 1)
            # Legacy rows remain untouched for historical reconciliation.
            self.assertEqual(connection.execute(text(
                "SELECT value FROM app_settings WHERE key=:key"
            ), {"key": migration.VERSIONS_KEY}).scalar_one(), json.dumps(legacy_versions))
            with self.assertRaises(IntegrityError):
                connection.execute(text(
                    "UPDATE projects SET is_default=1 WHERE id=2"
                ))
        engine.dispose()

    def test_sql_rows_preserve_independent_state_and_append_versions(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        record_version(db, "skill", 1, {"name": "one"}, actor_id=None, change="created")
        record_version(db, "skill", 1, {"name": "two"}, actor_id=None, change="updated")
        update_resource_state(db, "mcp", 1, catalog_hash="a", review_required=False)
        update_resource_state(db, "mcp", 2, catalog_hash="b", review_required=True)
        db.commit()
        self.assertEqual([item["version"] for item in versions(db, "skill", 1)], [1, 2])
        self.assertEqual(resource_state(db, "mcp", 1)["catalog_hash"], "a")
        self.assertEqual(resource_state(db, "mcp", 2)["catalog_hash"], "b")
        db.close()
        engine.dispose()

    def test_concurrent_catalog_changes_cannot_clear_review_requirement(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = create_engine(
                f"sqlite:///{(Path(temp) / 'state.db').as_posix()}",
                connect_args={"timeout": 10},
            )
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine)

            def write_catalog(value):
                db = factory()
                try:
                    update_resource_state(
                        db, "mcp", 11, catalog_hash=value,
                        review_required=False, tool_count=1,
                    )
                    db.commit()
                finally:
                    db.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(write_catalog, ("catalog-a", "catalog-b")))
            db = factory()
            state = resource_state(db, "mcp", 11)
            self.assertTrue(state["review_required"])
            self.assertIn(state["catalog_hash"], {"catalog-a", "catalog-b"})
            db.close()
            engine.dispose()

    def test_concurrent_version_appends_keep_unique_monotonic_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = create_engine(
                f"sqlite:///{(Path(temp) / 'versions.db').as_posix()}",
                connect_args={"timeout": 10},
            )
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine)

            def append(value):
                db = factory()
                try:
                    record_version(
                        db, "skill", 21, {"name": value},
                        actor_id=None, change="updated",
                    )
                    db.commit()
                finally:
                    db.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(append, ("one", "two")))
            db = factory()
            rows = versions(db, "skill", 21)
            self.assertEqual([row["version"] for row in rows], [1, 2])
            self.assertEqual({row["snapshot"]["name"] for row in rows}, {"one", "two"})
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
