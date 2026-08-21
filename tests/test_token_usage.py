import unittest
import datetime
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from backend import jobs
from backend.api.token_usage import (
    reset_user_monthly_usage,
    reset_user_weekly_usage,
    token_usage_summary,
    update_user_token_limits,
)
from backend.api.users import delete_user
from backend.database import Base
from backend.models import Artifact, Job, ROLE_ROOT, ROLE_USER, TokenUsage, User, UserTokenLimit
from backend.schemas import UserTokenLimitsUpdate
from backend.token_usage import (
    TokenQuotaExceeded, bind_usage_context, ensure_usage_allowed,
    extract_usage,
    record_response_usage,
    token_limit_violation,
    usage_limit_snapshot,
)


class TokenUsageExtractionTests(unittest.TestCase):
    def test_chat_completions_usage(self):
        result = extract_usage({
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 12},
            }
        })
        self.assertEqual(result, {
            "input_tokens": 120,
            "output_tokens": 30,
            "cached_tokens": 40,
            "reasoning_tokens": 12,
            "total_tokens": 150,
        })

    def test_responses_terminal_event_usage(self):
        result = extract_usage({
            "type": "response.completed",
            "response": {
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 20,
                    "total_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 10},
                }
            },
        })
        self.assertEqual(result["total_tokens"], 100)
        self.assertEqual(result["cached_tokens"], 10)

    def test_anthropic_cache_usage(self):
        result = extract_usage({
            "usage": {
                "input_tokens": 55,
                "output_tokens": 9,
                "cache_read_input_tokens": 21,
            }
        })
        self.assertEqual(result["total_tokens"], 64)
        self.assertEqual(result["cached_tokens"], 21)

    def test_empty_or_missing_usage_is_ignored(self):
        self.assertIsNone(extract_usage({}))
        self.assertIsNone(extract_usage({"usage": {"input_tokens": 0, "output_tokens": 0}}))

    def test_bound_usage_is_persisted_and_aggregated(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        db = session_factory()
        root = User(username="root", password_hash="x", role=ROLE_ROOT, is_active=True)
        db.add(root)
        db.commit()
        db.refresh(root)
        with patch("backend.token_usage.SessionLocal", session_factory):
            with bind_usage_context(root.id, run_id="run-1", agent_id=None):
                self.assertTrue(record_response_usage({
                    "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}
                }, model="test-model"))
        result = token_usage_summary(days=0, user_id=None, _=root, db=db)
        self.assertEqual(result["totals"]["total_tokens"], 100)
        self.assertEqual(result["totals"]["requests"], 1)
        self.assertEqual(result["users"][0]["username"], "root")
        db.close()
        engine.dispose()

    def test_limits_and_period_resets_preserve_all_usage_rows(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        root = User(username="root", password_hash="x", role=ROLE_ROOT, is_active=True)
        user = User(username="user", password_hash="x", role=ROLE_USER, is_active=True)
        db.add_all([root, user])
        db.flush()
        now = datetime.datetime.now(datetime.timezone.utc)
        db.add_all([TokenUsage(
            user_id=user.id, username=user.username, model="a", total_tokens=value,
            created_at=now - datetime.timedelta(minutes=offset),
        ) for value, offset in ((10, 2), (20, 1))])
        db.commit()

        updated = update_user_token_limits(
            user.id,
            UserTokenLimitsUpdate(weekly_limit=30, monthly_limit=50, total_limit=100),
            _=root,
            db=db,
        )
        self.assertEqual(updated["periods"]["weekly"]["used"], 30)
        self.assertTrue(updated["periods"]["weekly"]["exceeded"])
        violation = token_limit_violation(db, user.id)
        self.assertIn("每周 Token 用量已达上限", violation)
        self.assertIn("0.003 万/0.003 万", violation)
        with self.assertRaises(jobs.JobQuotaExceeded) as raised:
            jobs.enqueue_in_session(
                db, user.id, None, "chat", {"session_id": "over-limit", "inputs": {"query": "x"}}
            )
        self.assertIn("每周 Token 用量已达上限", str(raised.exception))
        self.assertEqual(db.query(Job).count(), 0)

        reset_user_weekly_usage(user.id, _=root, db=db)
        weekly = usage_limit_snapshot(db, user.id)
        self.assertEqual(weekly["weekly"]["used"], 0)
        self.assertEqual(weekly["monthly"]["used"], 30)
        self.assertEqual(weekly["total"]["used"], 30)
        self.assertEqual(db.query(TokenUsage).count(), 2)

        reset_user_monthly_usage(user.id, _=root, db=db)
        monthly = usage_limit_snapshot(db, user.id)
        self.assertEqual(monthly["monthly"]["used"], 0)
        self.assertEqual(monthly["total"]["used"], 30)
        self.assertEqual(db.query(TokenUsage).count(), 2)
        db.close()
        engine.dispose()

    def test_running_agent_loop_rechecks_quota_before_next_model_call(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        db = session_factory()
        user = User(username="loop-user", password_hash="x", is_active=True)
        db.add(user)
        db.flush()
        db.add(UserTokenLimit(user_id=user.id, total_limit=10))
        db.add(TokenUsage(
            user_id=user.id,
            username=user.username,
            model="loop-model",
            total_tokens=10,
        ))
        db.commit()
        user_id = user.id
        db.close()

        with patch("backend.token_usage.SessionLocal", session_factory):
            with bind_usage_context(user_id, run_id="loop-run"):
                with self.assertRaises(TokenQuotaExceeded) as raised:
                    ensure_usage_allowed()
        self.assertIn("Token 用量已达上限", str(raised.exception))
        engine.dispose()

    def test_delete_user_clears_legacy_copy_and_preserves_usage_ledger(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        root = User(username="root", password_hash="x", role=ROLE_ROOT, is_active=True)
        user = User(username="user", password_hash="x", role=ROLE_USER, is_active=True)
        db.add_all([root, user])
        db.flush()
        usage = TokenUsage(
            user_id=user.id, username=user.username, model="a", total_tokens=10
        )
        db.add(usage)
        db.commit()
        usage_id = usage.id
        db.execute(text(
            "CREATE TABLE conversations ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, query TEXT, "
            "FOREIGN KEY(user_id) REFERENCES users(id))"
        ))
        db.execute(
            text("INSERT INTO conversations (id, user_id, query) VALUES (1, :uid, '旧会话')"),
            {"uid": user.id},
        )
        db.commit()

        delete_user(user.id, current=root, db=db)

        self.assertIsNone(db.get(User, user.id))
        retained = db.get(TokenUsage, usage_id)
        self.assertIsNotNone(retained)
        self.assertIsNone(retained.user_id)
        self.assertEqual(retained.username, "user")
        self.assertEqual(
            db.execute(text("SELECT COUNT(*) FROM conversations WHERE user_id=:uid"), {"uid": user.id}).scalar_one(),
            0,
        )
        self.assertIsNone(db.get(UserTokenLimit, user.id))
        db.close()
        engine.dispose()

    def test_delete_user_removes_owned_artifact_metadata_and_file(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        root = User(username="root", password_hash="x", role=ROLE_ROOT, is_active=True)
        user = User(username="artifact-user", password_hash="x", role=ROLE_USER, is_active=True)
        db.add_all([root, user])
        db.flush()

        with tempfile.TemporaryDirectory() as temp:
            export_dir = Path(temp)
            artifact_file = export_dir / "owned-result.docx"
            artifact_file.write_bytes(b"test artifact")
            artifact = Artifact(
                id="owned-artifact",
                run_id=None,
                turn_id=None,
                owner_id=user.id,
                filename=artifact_file.name,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                size_bytes=artifact_file.stat().st_size,
                expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
            )
            db.add(artifact)
            db.commit()
            artifact_id = artifact.id

            with patch("backend.artifacts.EXPORT_DIR", export_dir):
                delete_user(user.id, current=root, db=db)

            self.assertIsNone(db.get(User, user.id))
            self.assertIsNone(db.get(Artifact, artifact_id))
            self.assertFalse(artifact_file.exists())

        db.close()
        engine.dispose()

    def test_delete_user_preserves_artifact_file_still_referenced_by_another_user(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        root = User(username="root", password_hash="x", role=ROLE_ROOT, is_active=True)
        deleted_user = User(username="deleted", password_hash="x", role=ROLE_USER, is_active=True)
        remaining_user = User(username="remaining", password_hash="x", role=ROLE_USER, is_active=True)
        db.add_all([root, deleted_user, remaining_user])
        db.flush()

        with tempfile.TemporaryDirectory() as temp:
            export_dir = Path(temp)
            artifact_file = export_dir / "shared-result.docx"
            artifact_file.write_bytes(b"shared artifact")
            expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
            db.add_all([
                Artifact(
                    id="deleted-owner-artifact", run_id=None, turn_id=None,
                    owner_id=deleted_user.id, filename=artifact_file.name,
                    media_type="application/octet-stream", size_bytes=artifact_file.stat().st_size,
                    expires_at=expires_at,
                ),
                Artifact(
                    id="remaining-owner-artifact", run_id=None, turn_id=None,
                    owner_id=remaining_user.id, filename=artifact_file.name,
                    media_type="application/octet-stream", size_bytes=artifact_file.stat().st_size,
                    expires_at=expires_at,
                ),
            ])
            db.commit()

            with patch("backend.artifacts.EXPORT_DIR", export_dir):
                delete_user(deleted_user.id, current=root, db=db)

            self.assertIsNone(db.get(Artifact, "deleted-owner-artifact"))
            self.assertIsNotNone(db.get(Artifact, "remaining-owner-artifact"))
            self.assertTrue(artifact_file.is_file())

        db.close()
        engine.dispose()

    def test_delete_user_rollback_preserves_artifact_when_hidden_foreign_key_blocks(self):
        engine = create_engine("sqlite:///:memory:")

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        root = User(username="root", password_hash="x", role=ROLE_ROOT, is_active=True)
        user = User(username="blocked", password_hash="x", role=ROLE_USER, is_active=True)
        db.add_all([root, user])
        db.flush()

        with tempfile.TemporaryDirectory() as temp:
            export_dir = Path(temp)
            artifact_file = export_dir / "blocked-result.docx"
            artifact_file.write_bytes(b"must survive rollback")
            db.add(Artifact(
                id="blocked-owner-artifact", run_id=None, turn_id=None,
                owner_id=user.id, filename=artifact_file.name,
                media_type="application/octet-stream", size_bytes=artifact_file.stat().st_size,
                expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
            ))
            db.execute(text(
                "CREATE TABLE hidden_user_refs ("
                "id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, "
                "FOREIGN KEY(owner_id) REFERENCES users(id))"
            ))
            db.execute(
                text("INSERT INTO hidden_user_refs (id, owner_id) VALUES (1, :uid)"),
                {"uid": user.id},
            )
            db.commit()

            with patch("backend.artifacts.EXPORT_DIR", export_dir):
                with self.assertRaises(HTTPException) as caught:
                    delete_user(user.id, current=root, db=db)

            self.assertEqual(caught.exception.status_code, 409)
            self.assertIsNotNone(db.get(User, user.id))
            self.assertIsNotNone(db.get(Artifact, "blocked-owner-artifact"))
            self.assertTrue(artifact_file.is_file())

        db.close()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
