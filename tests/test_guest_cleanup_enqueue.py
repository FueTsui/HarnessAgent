"""Account cleanup and queue insertion share a real SQLite write boundary."""
import tempfile
import datetime
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from backend import jobs
from backend.database import Base
from backend.models import Job, User


class GuestCleanupEnqueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine(
            "sqlite:///" + (Path(self.temp.name) / "queue.db").as_posix(),
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, autoflush=False)
        with self.factory.begin() as db:
            db.add(User(id=1, username="visitor_cleanup", password_hash="x",
                        role="user", is_active=True, token_version=7))

    def enqueue(self, db, owner_id=1, **kwargs):
        # Non-chat jobs expose the missing owner FK without another table hiding it.
        return jobs.enqueue_in_session(db, owner_id, None, "evaluation", {}, **kwargs)

    def test_deleted_owner_is_rejected_despite_cached_authentication(self):
        with self.factory() as request_db:
            cached_user = request_db.get(User, 1)
            with self.factory.begin() as cleanup_db:
                cleanup_db.delete(cleanup_db.get(User, 1))
            self.assertTrue(cached_user.is_active)
            with self.assertRaises(jobs.JobOwnerUnavailable):
                self.enqueue(request_db)
            request_db.rollback()
        with self.factory() as db:
            self.assertEqual(db.query(Job).count(), 0)

    def test_disabled_owner_is_reloaded_before_idempotency_shortcut(self):
        with self.factory.begin() as db:
            job_id = self.enqueue(db, idempotency_key="same-request")
        with self.factory() as request_db:
            cached_user = request_db.get(User, 1)
            with self.factory.begin() as disable_db:
                disable_db.execute(update(User).where(User.id == 1).values(is_active=False))
            self.assertTrue(cached_user.is_active)
            with self.assertRaises(jobs.JobOwnerUnavailable):
                self.enqueue(request_db, idempotency_key="same-request")
            self.assertFalse(cached_user.is_active)
            request_db.rollback()
        with self.factory() as db:
            self.assertEqual([row.id for row in db.query(Job)], [job_id])

    def test_reused_user_id_cannot_receive_previously_authenticated_request(self):
        with self.factory() as request_db:
            cached_user = request_db.get(User, 1)
            old_identity = jobs.owner_identity(cached_user)
            with self.factory.begin() as cleanup_db:
                cleanup_db.delete(cleanup_db.get(User, 1))
            with self.factory.begin() as new_guest_db:
                replacement = User(username="visitor_replacement", password_hash="x", role="user",
                                   is_active=True, created_at=cached_user.created_at + datetime.timedelta(seconds=1))
                new_guest_db.add(replacement)
                new_guest_db.flush()
                self.assertEqual(replacement.id, cached_user.id)
            self.assertEqual(jobs.owner_identity(cached_user), old_identity)
            with self.assertRaises(jobs.JobOwnerUnavailable):
                self.enqueue(request_db)
            request_db.rollback()
        with self.factory() as db:
            self.assertEqual(db.get(User, 1).username, "visitor_replacement")
            self.assertEqual(db.query(Job).count(), 0)

    def test_new_enqueue_session_rejects_reused_id_with_frozen_request_identity(self):
        with self.factory() as authenticated_db:
            authenticated = authenticated_db.get(User, 1)
            expected = jobs.owner_identity(authenticated)
            with self.factory.begin() as cleanup_db:
                cleanup_db.delete(cleanup_db.get(User, 1))
            with self.factory.begin() as new_guest_db:
                replacement = User(username="visitor_new_session", password_hash="x", role="user",
                                   is_active=True, created_at=authenticated.created_at + datetime.timedelta(seconds=1))
                new_guest_db.add(replacement)
                new_guest_db.flush()
                self.assertEqual(replacement.id, authenticated.id)
            with patch.object(jobs, "SessionLocal", self.factory):
                with self.assertRaises(jobs.JobOwnerUnavailable):
                    jobs.enqueue(authenticated.id, None, "evaluation", {}, expected_owner_identity=expected)
        with self.factory() as db:
            self.assertEqual(db.get(User, 1).username, "visitor_new_session")
            self.assertEqual(db.query(Job).count(), 0)

    def test_security_version_change_invalidates_frozen_enqueue_identity(self):
        with self.factory() as db:
            expected = jobs.owner_identity(db.get(User, 1))
        with self.factory.begin() as db:
            db.execute(update(User).where(User.id == 1).values(token_version=8))
        with patch.object(jobs, "SessionLocal", self.factory):
            with self.assertRaises(jobs.JobOwnerUnavailable):
                jobs.enqueue(1, None, "evaluation", {}, expected_owner_identity=expected)
        with self.factory() as db:
            self.assertEqual(db.query(Job).count(), 0)

    def test_owner_identity_normalizes_database_and_utc_timestamps(self):
        with self.factory() as db:
            user = db.get(User, 1)
            expected = jobs.owner_identity(user)
            user.created_at = user.created_at.replace(tzinfo=datetime.timezone.utc).astimezone(
                datetime.timezone(datetime.timedelta(hours=8)))
            self.assertEqual(jobs.owner_identity(user), expected)

    def test_active_owner_keeps_idempotency_and_security_version(self):
        with self.factory.begin() as db:
            first = self.enqueue(db, idempotency_key="same-request")
            self.assertEqual(self.enqueue(db, idempotency_key="same-request"), first)
        with self.factory() as db:
            self.assertEqual(db.get(User, 1).token_version, 7)
            self.assertEqual(db.query(Job).count(), 1)

    def test_system_job_with_no_owner_keeps_existing_behavior(self):
        statements = []

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lower())

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            with self.factory.begin() as db:
                job_id = self.enqueue(db, owner_id=None)
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        self.assertFalse(any("users" in statement for statement in statements))
        with self.factory() as db:
            self.assertIsNone(db.get(Job, job_id).owner_id)

    def test_cleanup_winning_write_lock_prevents_late_orphan_job(self):
        authenticated = threading.Event()
        try_enqueue = threading.Event()
        update_attempted = threading.Event()

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            if (threading.current_thread().name.startswith("late-request")
                    and statement.lstrip().upper().startswith("UPDATE USERS")):
                update_attempted.set()

        def late_request():
            with self.factory.begin() as db:
                cached_user = db.get(User, 1)
                self.assertTrue(cached_user.is_active)
                authenticated.set()
                self.assertTrue(try_enqueue.wait(5))
                return self.enqueue(db)

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-request") as pool:
                future = pool.submit(late_request)
                self.assertTrue(authenticated.wait(5))
                try:
                    with self.factory.begin() as cleanup_db:
                        cleanup_db.execute(update(User).where(User.id == 1).values(id=User.id))
                        cleanup_db.delete(cleanup_db.get(User, 1))
                        cleanup_db.flush()
                        try_enqueue.set()
                        self.assertTrue(update_attempted.wait(5))
                        self.assertFalse(future.done())
                finally:
                    try_enqueue.set()
                with self.assertRaises(jobs.JobOwnerUnavailable):
                    future.result(timeout=5)
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        with self.factory() as db:
            self.assertIsNone(db.get(User, 1))
            self.assertEqual(db.query(Job).count(), 0)

    def test_enqueue_winning_write_lock_exposes_active_job_to_cleanup(self):
        cleanup_attempted = threading.Event()

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            if (threading.current_thread().name.startswith("account-cleanup")
                    and statement.lstrip().upper().startswith("UPDATE USERS")):
                cleanup_attempted.set()

        def cleanup():
            with self.factory.begin() as db:
                db.execute(update(User).where(User.id == 1).values(id=User.id))
                active = db.query(Job).filter(Job.owner_id == 1, Job.status.in_(jobs._INFLIGHT)).first()
                if active is not None:
                    return "blocked_by_active_job"
                db.delete(db.get(User, 1))
                return "deleted"

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-cleanup") as pool:
                with self.factory.begin() as enqueue_db:
                    job_id = self.enqueue(enqueue_db)
                    future = pool.submit(cleanup)
                    self.assertTrue(cleanup_attempted.wait(5))
                    self.assertFalse(future.done())
                self.assertEqual(future.result(timeout=5), "blocked_by_active_job")
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        with self.factory() as db:
            self.assertIsNotNone(db.get(User, 1))
            self.assertEqual(db.get(Job, job_id).status, jobs.PENDING)

    def test_postgres_owner_check_uses_for_update_and_refresh(self):
        db = MagicMock()
        db.get_bind.return_value.dialect.name = "postgresql"
        db.execute.return_value.scalar_one_or_none.return_value = None
        with self.assertRaises(jobs.JobOwnerUnavailable):
            self.enqueue(db)
        statement = db.execute.call_args.args[0]
        self.assertIn("FOR UPDATE", str(statement.compile(dialect=postgresql.dialect())))
        self.assertTrue(statement.get_execution_options()["populate_existing"])
        db.add.assert_not_called()

    def test_api_reports_unavailable_owner_as_401(self):
        from backend.main import app as production_app

        app = FastAPI()
        app.add_exception_handler(
            jobs.JobOwnerUnavailable,
            production_app.exception_handlers[jobs.JobOwnerUnavailable],
        )

        @app.post("/enqueue-after-deletion")
        def enqueue_after_deletion():
            with self.factory.begin() as db:
                self.enqueue(db, owner_id=999)

        with TestClient(app) as client:
            response = client.post("/enqueue-after-deletion")
        self.assertEqual(response.status_code, 401)
        self.assertIn("账号不存在或已禁用", response.json()["detail"])
        with self.factory() as db:
            self.assertEqual(db.query(Job).count(), 0)


if __name__ == "__main__":
    unittest.main()
