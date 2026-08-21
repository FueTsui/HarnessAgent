"""Project / Thread / Turn / Item 核心状态模型测试。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend import attachments, jobs
from backend.models import Agent, Attachment, Item, Thread, Turn, User
from backend.runtime import task_store


class TaskModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(username="task-owner", password_hash="x")
        self.agent = Agent(name="task-agent")
        self.db.add_all([self.user, self.agent])
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_turn_items_are_append_only_ordered_facts(self):
        thread = task_store.ensure_thread(
            self.db,
            thread_id="thread-core",
            owner_id=self.user.id,
            agent_id=self.agent.id,
        )
        turn = task_store.create_turn(
            self.db,
            thread=thread,
            input_text="检查项目",
            payload={"harness": {"version": 2}},
        )
        task_store.append_runtime_item(
            self.db, turn.id, "tool.called", {"tool": "read"}
        )
        task_store.append_runtime_item(
            self.db, turn.id, "tool.completed", {"tool": "read", "ok": True}
        )
        task_store.finish_turn(self.db, turn, answer="检查完成")
        task_store.append_runtime_item(
            self.db, turn.id, "turn.completed", {"has_result": True}
        )
        self.db.commit()

        rows = self.db.query(Item).filter_by(turn_id=turn.id).order_by(Item.sequence).all()
        self.assertEqual([row.sequence for row in rows], list(range(1, 6)))
        self.assertEqual(rows[0].role, "user")
        self.assertEqual(rows[1].kind, "tool_call")
        self.assertEqual(rows[1].status, "started")
        self.assertEqual(rows[2].kind, "tool_result")
        self.assertEqual(rows[3].role, "assistant")
        self.assertEqual(rows[4].name, "turn.completed")
        self.assertEqual(self.db.get(Turn, turn.id).final_output, "检查完成")
        self.assertEqual(self.db.get(Thread, thread.id).owner_id, self.user.id)
        self.assertEqual(json.loads(turn.execution_snapshot)["harness"]["version"], 2)

    def test_thread_history_is_built_only_from_message_items(self):
        thread = task_store.ensure_thread(
            self.db,
            thread_id="thread-history",
            owner_id=self.user.id,
            agent_id=self.agent.id,
        )
        for question, answer in (("第一问", "第一答"), ("第二问", "第二答")):
            turn = task_store.create_turn(
                self.db, thread=thread, input_text=question, payload={}
            )
            task_store.append_runtime_item(
                self.db, turn.id, "verification.completed", {"passed": True}
            )
            task_store.finish_turn(self.db, turn, answer=answer)
        self.db.commit()

        self.assertEqual(
            task_store.thread_messages(
                self.db, owner_id=self.user.id, thread_id=thread.id
            ),
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
                {"role": "user", "content": "第二问"},
                {"role": "assistant", "content": "第二答"},
            ],
        )

    def test_persistent_attachment_is_resolved_by_followup_turn(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            attachments, "UPLOAD_DIR", Path(temp)
        ):
            source = Path(temp) / "source.txt"
            source.write_text("需要跨轮保留的资料", encoding="utf-8")
            source.with_suffix(".name").write_text("项目资料.txt", encoding="utf-8")
            record = attachments.pending_record(source, "document")
            thread = task_store.ensure_thread(
                self.db,
                thread_id="thread-attachment",
                owner_id=self.user.id,
                agent_id=self.agent.id,
            )
            first = task_store.create_turn(
                self.db,
                thread=thread,
                input_text="处理附件",
                payload={
                    "attachment_context": [attachments.public_record(record)],
                },
            )
            attachments.persist_pending_records(
                self.db,
                owner_id=self.user.id,
                thread_id=thread.id,
                turn_id=first.id,
                records=[record],
            )
            self.db.commit()

            inherited = attachments.thread_context(
                self.db, owner_id=self.user.id, thread_id=thread.id
            )
            self.assertEqual(inherited["documents"], [source.resolve()])
            self.assertEqual(inherited["continuation_of_turn_id"], first.id)
            self.assertTrue(inherited["metadata"][0]["inherited"])
            self.assertEqual(inherited["metadata"][0]["name"], "项目资料.txt")

            followup = task_store.create_turn(
                self.db,
                thread=thread,
                input_text="仅改格式",
                payload={
                    "continuation_of_turn_id": first.id,
                    "attachment_context": inherited["metadata"],
                },
            )
            self.db.commit()
            self.assertEqual(followup.continuation_of_turn_id, first.id)
            message = self.db.query(Item).filter_by(
                turn_id=followup.id, kind="message", role="user"
            ).one()
            self.assertEqual(
                json.loads(message.payload)["attachments"][0]["id"], record["id"]
            )
            self.assertEqual(self.db.query(Attachment).count(), 1)

    def test_upload_cleanup_keeps_files_referenced_by_persistent_attachment(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            attachments, "UPLOAD_DIR", Path(temp)
        ), patch.object(
            jobs, "UPLOAD_DIR", Path(temp)
        ), patch.object(
            jobs, "SessionLocal", self.Session
        ):
            source = Path(temp) / "kept.txt"
            source.write_text("persistent", encoding="utf-8")
            source.with_suffix(".name").write_text("保留.txt", encoding="utf-8")
            record = attachments.pending_record(source, "document")
            thread = task_store.ensure_thread(
                self.db, thread_id="cleanup-thread", owner_id=self.user.id,
                agent_id=self.agent.id,
            )
            turn = task_store.create_turn(
                self.db, thread=thread, input_text="保留附件", payload={}
            )
            attachments.persist_pending_records(
                self.db,
                owner_id=self.user.id,
                thread_id=thread.id,
                turn_id=turn.id,
                records=[record],
            )
            self.db.commit()
            os.utime(source, (1, 1))
            os.utime(source.with_suffix(".name"), (1, 1))
            self.assertEqual(jobs.cleanup_uploads(1), 0)
            self.assertTrue(source.exists())

            self.db.query(Attachment).delete()
            self.db.commit()
            self.assertEqual(jobs.cleanup_uploads(1), 2)
            self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
