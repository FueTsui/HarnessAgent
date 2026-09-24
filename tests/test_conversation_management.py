"""会话置顶、归档和项目管理回归测试。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.chat import (
    _end_line,
    _persisted_events_after,
    _run_processes,
    agent_chat_statuses,
    create_project,
    delete_project,
    ensure_default_project,
    my_conversations,
    my_projects,
    update_project,
    update_thread,
)
from backend import jobs
from backend.api import chat as chat_api
from backend.database import Base
from backend.models import AuditLog, Agent, Item, Job, Project, Thread, Turn, User
from backend.runtime import task_store
from backend.schemas import ProjectCreate, ProjectUpdate, ThreadUpdate


class ConversationManagementTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(username="owner", password_hash="x", role="user")
        self.other = User(username="other", password_hash="x", role="user")
        self.db.add_all([self.user, self.other])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.other)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_thread_can_be_pinned_archived_restored_and_listed(self):
        project = create_project(ProjectCreate(name="  新项目  "), self.user, self.db)
        thread = task_store.ensure_thread(
            self.db, thread_id="thread-a", owner_id=self.user.id,
            agent_id=None, project_id=project["id"],
        )
        thread.title = "主题"
        for query, answer in (("第一问", "第一答"), ("第二问", "第二答")):
            turn = task_store.create_turn(
                self.db, thread=thread, input_text=query, payload={}
            )
            task_store.finish_turn(self.db, turn, answer=answer)
        self.db.commit()

        update_thread(
            "thread-a", ThreadUpdate(pinned=True), self.user, self.db
        )
        self.assertTrue(self.db.get(Thread, "thread-a").is_pinned)
        update_thread(
            "thread-a", ThreadUpdate(title="  新名称  "), self.user, self.db
        )
        self.assertEqual(self.db.get(Thread, "thread-a").title, "新名称")

        update_thread(
            "thread-a", ThreadUpdate(archived=True), self.user, self.db
        )
        self.assertEqual(my_conversations(None, False, self.user, self.db), [])
        archived = my_conversations(None, True, self.user, self.db)
        self.assertEqual(len(archived), 2)
        self.assertTrue(all(row["archived"] for row in archived))
        self.assertTrue(all(not row["pinned"] for row in archived))

        update_thread(
            "thread-a", ThreadUpdate(archived=False), self.user, self.db
        )
        self.assertEqual(len(my_conversations(None, False, self.user, self.db)), 2)
        projects = my_projects(self.user, self.db)
        self.assertEqual(projects[0]["conversation_count"], 1)

    def test_conversation_history_preserves_turn_status_semantics(self):
        thread = task_store.ensure_thread(
            self.db, thread_id="thread-status", owner_id=self.user.id,
            agent_id=None,
        )
        running = task_store.create_turn(
            self.db, thread=thread, input_text="运行中问题", payload={}
        )
        running.status = "running"
        cancelled = task_store.create_turn(
            self.db, thread=thread, input_text="已取消问题", payload={}
        )
        task_store.finish_turn(self.db, cancelled, answer="", status="cancelled")
        failed = task_store.create_turn(
            self.db, thread=thread, input_text="失败问题", payload={}
        )
        task_store.finish_turn(
            self.db, failed, answer="", status="failed", error="上游不可用"
        )
        empty_done = task_store.create_turn(
            self.db, thread=thread, input_text="空完成问题", payload={}
        )
        task_store.finish_turn(self.db, empty_done, answer="", status="completed")
        self.db.commit()

        rows = {
            row["query"]: row
            for row in my_conversations(None, False, self.user, self.db)
        }
        self.assertEqual(rows["运行中问题"]["status"], "running")
        self.assertEqual(rows["运行中问题"]["answer"], "")
        self.assertEqual(rows["已取消问题"]["answer"], "已停止生成。")
        self.assertEqual(rows["失败问题"]["answer"], "执行失败：上游不可用")
        self.assertEqual(rows["失败问题"]["error"], "上游不可用")
        self.assertEqual(
            rows["空完成问题"]["answer"],
            "该任务未返回可展示结果，请重新生成。",
        )

    def test_limited_answer_persists_distinct_completion_semantics(self):
        agent = Agent(name="有限结果智能体", created_by=self.user.id)
        self.db.add(agent)
        self.db.commit()
        self.db.refresh(agent)
        plan_summary = {
            "total": 4,
            "completed": 1,
            "failed": 0,
            "blocked": 3,
            "skipped": 0,
            "pending": 0,
            "in_progress": 0,
            "terminalized": True,
            "all_completed": False,
        }
        result = {
            "answer": "已有证据支持有限结论，三个计划步骤仍受阻。",
            "completion_status": "completed_with_issues",
            "completion_issues": ["计划存在阻塞步骤：步骤二、步骤三、步骤四"],
            "plan_summary": plan_summary,
            "reasoning": "不得通过实时 end 事件泄漏",
        }

        with patch.object(jobs, "SessionLocal", self.Session):
            job_id = jobs.enqueue(
                self.user.id,
                agent.id,
                "chat",
                {
                    "session_id": "thread-limited",
                    "inputs": {"query": "执行四步计划"},
                },
            )
            claimed = jobs.claim_next("limited-worker")
            self.assertEqual(claimed.id, job_id)
            jobs.append_event(
                job_id,
                claimed.worker_id,
                claimed.lease_token,
                "loop.stopped",
                {
                    "reason": "successful_tool_budget",
                    "iteration": 8,
                    "successful_tools": 8,
                    "max_successful_calls": 8,
                    "max_iterations": 12,
                },
            )
            jobs.append_event(
                job_id,
                claimed.worker_id,
                claimed.lease_token,
                "plan.closeout.completed",
                {
                    "reason": "successful_tool_budget",
                    "revision": 3,
                    "applied": True,
                    "resolved": False,
                    "terminalized": True,
                    "all_completed": False,
                    "outcome": "completed_with_issues",
                    "unfinished_steps": [],
                    "status_counts": {
                        "completed": 1,
                        "failed": 0,
                        "blocked": 3,
                        "skipped": 0,
                        "pending": 0,
                        "in_progress": 0,
                    },
                },
            )
            terminal_queue = jobs.subscribe(job_id)
            self.assertTrue(jobs.finish(
                job_id,
                claimed.worker_id,
                claimed.lease_token,
                result,
            ))
            view = jobs.view(job_id, self.user.id)
            terminal_event = terminal_queue.get_nowait()
            jobs.unsubscribe(job_id, terminal_queue)

        self.db.expire_all()
        row = self.db.get(Job, job_id)
        turn = self.db.get(Turn, job_id)
        self.assertEqual(row.status, jobs.DONE)
        self.assertEqual(row.progress, "已完成（有未完成项）")
        self.assertEqual(view.result["completion_status"], "completed_with_issues")
        self.assertEqual(turn.status, "completed_with_issues")
        self.assertEqual(turn.final_output, result["answer"])

        event_names = [
            value for (value,) in self.db.query(Item.name)
            .filter(Item.turn_id == job_id)
            .order_by(Item.sequence)
            .all()
        ]
        self.assertIn("turn.completed_with_issues", event_names)
        self.assertIn("task.completed_with_issues", event_names)
        self.assertNotIn("task.completed", event_names)

        process = _run_processes(self.db, [row])[job_id]
        self.assertEqual(process["task_status"], "completed_with_issues")
        process_by_type = {
            item["event_type"]: item for item in process["events"]
        }
        self.assertEqual(
            process_by_type["loop.stopped"]["payload"]["successful_tools"], 8
        )
        self.assertEqual(
            process_by_type["plan.closeout.completed"]["payload"]["outcome"],
            "completed_with_issues",
        )
        with patch.object(chat_api, "SessionLocal", self.Session):
            replay = _persisted_events_after(job_id, 0)
        replay_by_type = {item["event_type"]: item for item in replay}
        self.assertEqual(
            replay_by_type["loop.stopped"]["payload"]["max_successful_calls"], 8
        )
        self.assertEqual(
            replay_by_type["plan.closeout.completed"]["payload"]["status_counts"]["blocked"],
            3,
        )
        history = {
            item["query"]: item
            for item in my_conversations(None, False, self.user, self.db)
        }
        self.assertEqual(
            history["执行四步计划"]["status"], "completed_with_issues"
        )
        self.assertEqual(history["执行四步计划"]["answer"], result["answer"])
        agent_status = agent_chat_statuses(self.user, self.db)
        terminal = next(
            item for item in agent_status["items"] if item["agent_id"] == agent.id
        )
        self.assertEqual(terminal["terminal_status"], "completed_with_issues")

        end = json.loads(_end_line(view))
        self.assertEqual(end["status"], jobs.DONE)
        self.assertEqual(end["task_status"], "completed_with_issues")
        self.assertEqual(end["completion_status"], "completed_with_issues")
        self.assertEqual(end["plan_summary"]["blocked"], 3)
        self.assertNotIn("reasoning", view.result)
        self.assertEqual(terminal_event["type"], "end")
        self.assertEqual(
            terminal_event["result"]["completion_status"],
            "completed_with_issues",
        )
        self.assertNotIn("reasoning", terminal_event["result"])

    def test_process_infers_old_done_job_with_non_success_plan_as_limited(self):
        thread = task_store.ensure_thread(
            self.db,
            thread_id="thread-legacy-limited",
            owner_id=self.user.id,
            agent_id=None,
        )
        turn = task_store.create_turn(
            self.db,
            thread=thread,
            input_text="历史任务",
            payload={},
            turn_id="legacy-limited-job",
        )
        task_store.finish_turn(
            self.db, turn, answer="历史有限答复", status="completed"
        )
        row = Job(
            id=turn.id,
            owner_id=self.user.id,
            kind="chat",
            payload="{}",
            result=json.dumps({"answer": "历史有限答复"}, ensure_ascii=False),
            status=jobs.DONE,
        )
        self.db.add(row)
        task_store.append_runtime_item(self.db, turn.id, "plan.updated", {
            "revision": 3,
            "steps": [
                {"id": "step_1", "step": "核实资料", "status": "completed"},
                {"id": "step_2", "step": "形成结论", "status": "blocked"},
            ],
        })
        task_store.append_runtime_item(
            self.db, turn.id, "task.completed", {"status": "completed"}
        )
        self.db.commit()

        process = _run_processes(self.db, [row])[row.id]
        self.assertEqual(process["status"], jobs.DONE)
        self.assertEqual(process["task_status"], "completed_with_issues")
        with patch.object(chat_api, "SessionLocal", self.Session), patch.object(
            jobs, "SessionLocal", self.Session
        ):
            old_end = json.loads(_end_line(
                SimpleNamespace(
                    status=jobs.DONE,
                    result={"answer": "历史有限答复"},
                    error="",
                ),
                task_id=row.id,
            ))
        self.assertEqual(old_end["task_status"], "completed_with_issues")
        self.assertEqual(old_end["event_type"], "task.completed_with_issues")
        # 历史追加式事件保持原样；修复只纠正读取时的语义，不改写旧事实。
        self.assertEqual(
            self.db.query(Item).filter_by(
                turn_id=turn.id, name="task.completed_with_issues"
            ).count(),
            0,
        )

    def test_cross_process_stream_replays_finalizing_before_partial_and_terminal_before_end(self):
        """独立 Worker 只通过 DB 通知 API 进程时，事件顺序仍必须可信。"""
        job_id = "cross-process-stream"
        state = {"terminal": False}
        replay_revisions = []
        running = SimpleNamespace(
            status=jobs.RUNNING,
            result=None,
            error="",
            payload={},
        )
        terminal_result = {
            "answer": "有限答复",
            "completion_status": "completed_with_issues",
            "completion_issues": ["计划存在阻塞步骤：步骤二"],
            "plan_summary": {
                "total": 2,
                "completed": 1,
                "failed": 0,
                "blocked": 1,
                "skipped": 0,
                "pending": 0,
                "in_progress": 0,
                "terminalized": True,
                "all_completed": False,
            },
        }
        terminal = SimpleNamespace(
            status=jobs.DONE,
            result=terminal_result,
            error="",
            payload={},
        )
        persisted = [
            {
                "task_id": job_id,
                "event_id": "event-finalizing",
                "timestamp": "2026-08-13T00:00:00+00:00",
                "revision": 11,
                "event_type": "task.status",
                "payload": {"status": "finalizing", "reason": "answer_generation"},
            },
            {
                "task_id": job_id,
                "event_id": "event-terminal",
                "timestamp": "2026-08-13T00:00:01+00:00",
                "revision": 12,
                "event_type": "task.completed_with_issues",
                "payload": {
                    "status": "completed_with_issues",
                    **terminal_result,
                },
            },
        ]

        def replay(_task_id, revision):
            replay_revisions.append(revision)
            if not state["terminal"]:
                return []
            return [item for item in persisted if item["revision"] > revision]

        async def timeout_after_worker_commit(awaitable, timeout):
            del timeout
            # q.get() 未被真正等待，显式关闭避免测试泄漏 coroutine。
            awaitable.close()
            state["terminal"] = True
            raise asyncio.TimeoutError

        def view(_job_id, _user_id):
            return terminal if state["terminal"] else running

        request = SimpleNamespace(headers={}, cookies={}, query_params={})

        async def collect():
            response = await chat_api.stream_chat_job(job_id, request)
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return [
                json.loads(line)
                for chunk in chunks
                for line in str(chunk).splitlines()
                if line.strip()
            ]

        with patch.object(chat_api, "_user_id_from_request", return_value=self.user.id), \
                patch.object(jobs, "view", side_effect=view), \
                patch.object(jobs, "subscribe", side_effect=lambda _job_id: asyncio.Queue()), \
                patch.object(jobs, "unsubscribe"), \
                patch.object(jobs, "get_partial", side_effect=lambda _job_id: (
                    "有限答复" if state["terminal"] else ""
                )), \
                patch.object(jobs, "latest_event_meta", return_value=persisted[-1]), \
                patch.object(chat_api, "_persisted_events_after", side_effect=replay), \
                patch.object(chat_api.asyncio, "wait_for", new=timeout_after_worker_commit):
            events = asyncio.run(collect())

        finalizing_index = next(
            index for index, event in enumerate(events)
            if event.get("event_type") == "task.status"
            and event.get("payload", {}).get("status") == "finalizing"
        )
        delta_index = next(
            index for index, event in enumerate(events)
            if event.get("type") == "delta"
        )
        terminal_index = next(
            index for index, event in enumerate(events)
            if event.get("type") == "runtime"
            and event.get("event_type") == "task.completed_with_issues"
        )
        end_index = next(
            index for index, event in enumerate(events)
            if event.get("type") == "end"
        )
        self.assertLess(finalizing_index, delta_index)
        self.assertLess(terminal_index, end_index)
        self.assertEqual(
            sum(event.get("event_id") == "event-finalizing" for event in events),
            1,
        )
        self.assertEqual(
            sum(event.get("event_id") == "event-terminal" and event.get("type") == "runtime"
                for event in events),
            1,
        )
        self.assertIn(12, replay_revisions)

    def test_project_access_is_scoped_to_owner_and_delete_detaches_threads(self):
        own = create_project(ProjectCreate(name="自己的项目"), self.user, self.db)
        foreign = Project(user_id=self.other.id, name="别人的项目")
        self.db.add(foreign)
        thread = task_store.ensure_thread(
            self.db, thread_id="thread-b", owner_id=self.user.id,
            agent_id=None, project_id=own["id"],
        )
        turn = task_store.create_turn(
            self.db, thread=thread, input_text="问题", payload={}
        )
        task_store.finish_turn(self.db, turn, answer="答复")
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            update_thread(
                "thread-b",
                ThreadUpdate(project_id=foreign.id),
                self.user,
                self.db,
            )
        self.assertEqual(ctx.exception.status_code, 404)

        delete_project(own["id"], self.user, self.db)
        self.assertIsNone(
            self.db.get(Thread, "thread-b").project_id
        )

    def test_project_delete_detaches_legacy_conversation_foreign_key(self):
        project = create_project(ProjectCreate(name="旧数据项目"), self.user, self.db)
        self.db.execute(text(
            "CREATE TABLE conversations ("
            "id INTEGER PRIMARY KEY, user_id INTEGER, project_id INTEGER, "
            "FOREIGN KEY(project_id) REFERENCES projects(id))"
        ))
        self.db.execute(
            text(
                "INSERT INTO conversations (id, user_id, project_id) "
                "VALUES (1, :user_id, :project_id)"
            ),
            {"user_id": self.user.id, "project_id": project["id"]},
        )
        self.db.commit()

        delete_project(project["id"], self.user, self.db)

        legacy_project_id = self.db.execute(text(
            "SELECT project_id FROM conversations WHERE id = 1"
        )).scalar_one()
        self.assertIsNone(legacy_project_id)
        self.assertIsNone(self.db.get(Project, project["id"]))

    def test_project_can_be_pinned_archived_and_restored(self):
        first = create_project(ProjectCreate(name="普通项目"), self.user, self.db)
        second = create_project(ProjectCreate(name="置顶项目"), self.user, self.db)

        update_project(second["id"], ProjectUpdate(pinned=True), self.user, self.db)
        active = my_projects(self.user, self.db)
        self.assertEqual([row["id"] for row in active], [second["id"], first["id"]])
        self.assertTrue(active[0]["pinned"])

        update_project(second["id"], ProjectUpdate(archived=True), self.user, self.db)
        self.assertEqual([row["id"] for row in my_projects(self.user, self.db)], [first["id"]])
        archived = my_projects(self.user, self.db, archived=True)
        self.assertEqual([row["id"] for row in archived], [second["id"]])
        self.assertFalse(archived[0]["pinned"])

        update_project(second["id"], ProjectUpdate(archived=False), self.user, self.db)
        self.assertEqual(len(my_projects(self.user, self.db)), 2)

    def test_default_project_bootstrap_is_idempotent(self):
        first = ensure_default_project(self.user, self.db)
        second = ensure_default_project(self.user, self.db)

        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["default"])
        self.assertEqual(
            self.db.query(Project).filter(Project.user_id == self.user.id).count(),
            1,
        )

    def test_existing_default_project_does_not_hold_sqlite_writer_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "project-lock.db"
            engine = create_engine(
                f"sqlite:///{db_path.as_posix()}",
                connect_args={"check_same_thread": False, "timeout": 0.1},
            )

            @event.listens_for(engine, "connect")
            def sqlite_pragmas(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=100")
                cursor.close()

            Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine)
            request_db = session_factory()
            audit_db = session_factory()
            try:
                user = User(username="lock-owner", password_hash="x", role="user")
                request_db.add(user)
                request_db.commit()
                request_db.refresh(user)
                request_db.add(Project(
                    user_id=user.id,
                    name="默认项目",
                    is_default=True,
                ))
                request_db.commit()

                result = ensure_default_project(user, request_db)
                self.assertTrue(result["default"])

                # 刻意保持请求会话打开以复现审计中间件时序；第二连接仍须
                # 能够写入 append-only 审计记录。
                audit_db.add(AuditLog(
                    user_id=user.id,
                    username=user.username,
                    role=user.role,
                    method="POST",
                    path="/api/v1/projects/default",
                    status_code=201,
                    ip="127.0.0.1",
                ))
                audit_db.commit()
                self.assertEqual(audit_db.query(AuditLog).count(), 1)
            finally:
                audit_db.close()
                request_db.close()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
