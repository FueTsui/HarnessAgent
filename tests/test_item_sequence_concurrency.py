"""Item sequence allocation must preserve every concurrent runtime event."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend import jobs
from backend.database import Base
from backend.models import Item, Job, Thread, Turn, User
from backend.runtime import task_store


class ItemSequenceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine(
            "sqlite:///" + (Path(self.temp.name) / "events.db").as_posix(),
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        with self.sessions() as db:
            db.add(User(id=1, username="owner", password_hash="test", role="root"))
            db.flush()
            db.add(Thread(id="thread", owner_id=1, title="Concurrent events"))
            db.flush()
            db.add(Turn(id="run", thread_id="thread", owner_id=1, sequence=1, status="running"))
            db.add(Job(id="run", owner_id=1, kind="chat", status=jobs.RUNNING, worker_id="worker", lease_token="lease"))
            db.commit()

    def test_stale_orm_snapshot_cannot_reserve_an_existing_sequence(self):
        with self.sessions() as first, self.sessions() as second:
            first_turn = first.get(Turn, "run")
            second_turn = second.get(Turn, "run")
            self.assertEqual((first_turn.item_sequence, second_turn.item_sequence), (0, 0))
            first_item = task_store.append_item(first, first_turn, kind="event", name="first")
            first.commit()
            # The second session deliberately still owns the old ORM counter.
            self.assertEqual(second_turn.item_sequence, 0)
            second_item = task_store.append_item(second, second_turn, kind="event", name="second")
            second.commit()
            self.assertEqual((first_item.sequence, second_item.sequence), (1, 2))
        with self.sessions() as db:
            self.assertEqual(db.get(Turn, "run").item_sequence, 2)
            self.assertEqual(db.query(Item).order_by(Item.sequence).count(), 2)

    def test_parallel_visual_progress_and_runtime_events_keep_every_event(self):
        writers = 6
        barrier = threading.Barrier(writers)
        loaded_sequences = []
        original = task_store.append_item

        def synchronize_loaded_turn(db, turn, **kwargs):
            # Reproduce the actual race deterministically: every event writer
            # has loaded sequence zero before any writer reserves its revision.
            loaded_sequences.append(turn.item_sequence)
            barrier.wait(timeout=5)
            return original(db, turn, **kwargs)

        def persist(index):
            if index % 2 == 0:
                cancelled, meta = jobs.set_progress(
                    "run", "worker", "lease", f"识读来源图表：第 {index + 1} 页", include_event=True,
                )
                self.assertFalse(cancelled)
                return meta
            return jobs.append_event(
                "run", "worker", "lease", "attachments.visual_source", {"page": index + 1, "ok": True},
            )

        with patch.object(jobs, "SessionLocal", self.sessions), \
             patch.object(task_store, "append_item", side_effect=synchronize_loaded_turn), \
             ThreadPoolExecutor(max_workers=writers) as executor:
            metadata = list(executor.map(persist, range(writers)))

        self.assertEqual(loaded_sequences, [0] * writers)
        self.assertEqual(sorted(meta["revision"] for meta in metadata), list(range(1, writers + 1)))
        self.assertEqual(len({meta["event_id"] for meta in metadata}), writers)
        with self.sessions() as db:
            rows = db.query(Item).filter(Item.turn_id == "run").order_by(Item.sequence).all()
            self.assertEqual(len(rows), writers)
            self.assertEqual([row.sequence for row in rows], list(range(1, writers + 1)))
            self.assertEqual({row.id for row in rows}, {meta["event_id"] for meta in metadata})
            self.assertEqual(db.get(Turn, "run").item_sequence, writers)
            self.assertEqual(db.get(Turn, "run").status, "running")
            self.assertEqual(db.get(Job, "run").status, jobs.RUNNING)
            self.assertEqual(sum(row.name == "turn.progress" for row in rows), writers // 2)
            self.assertEqual(
                {json.loads(row.payload)["page"] for row in rows if row.name == "attachments.visual_source"},
                {2, 4, 6},
            )

    def test_allocation_and_items_rollback_together_without_committing_other_changes(self):
        with self.sessions() as db:
            turn = db.get(Turn, "run")
            turn.status = "failed"
            task_store.append_item(db, turn, kind="event", name="rolled_back")
            db.flush()
            with self.sessions() as observer:
                self.assertEqual(observer.query(Item).count(), 0)
                self.assertEqual(observer.get(Turn, "run").item_sequence, 0)
                self.assertEqual(observer.get(Turn, "run").status, "running")
            db.rollback()
            replacement = task_store.append_item(db, turn, kind="event", name="committed")
            db.commit()
            self.assertEqual(replacement.sequence, 1)
        with self.sessions() as db:
            self.assertEqual(db.query(Item).one().name, "committed")
            self.assertEqual(db.get(Turn, "run").status, "running")
            self.assertEqual(db.get(Turn, "run").item_sequence, 1)

    def test_multiple_pending_items_and_orm_flush_do_not_overwrite_counter(self):
        with self.sessions() as db:
            turn = db.get(Turn, "run")
            items = [task_store.append_item(db, turn, kind="event", name=f"event-{index}") for index in range(4)]
            self.assertEqual([item.sequence for item in items], [1, 2, 3, 4])
            self.assertEqual(turn.item_sequence, 4)
            turn.status = "completed"
            db.commit()
        with self.sessions() as db:
            self.assertEqual(db.get(Turn, "run").item_sequence, 4)
            self.assertEqual(db.get(Turn, "run").status, "completed")
            self.assertEqual(db.query(Item).count(), 4)


if __name__ == "__main__":
    unittest.main()
