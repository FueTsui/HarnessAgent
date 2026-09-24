"""持续覆盖架构审计中修复的安全回归。"""
import json
import re
import tempfile
import unittest
import asyncio
import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import BASE_DIR, WORKSPACE_DIR
from backend.database import Base
from backend.models import (
    AuditLog, Agent, Channel, Item, Job, JobGuidance, McpServer, ModelProvider,
    RateLimitBucket, Turn, User,
)
from backend.runtime import task_store
from backend.runtime import builtin_tools
from backend import approvals
from backend import artifacts
from backend import jobs
from backend import worker
from backend import weixin_channel
from backend import harness as harness_registry
from backend import main as app_main
from backend.api.chat import build_execution_snapshot
from backend.api import chat as chat_api


class AuditCompletenessTests(unittest.TestCase):
    def test_all_api_mutations_and_failures_are_audited_with_trusted_ip(self):
        self.assertTrue(app_main._should_audit(
            "POST", "/api/v1/chat/turns/turn-1/cancel"
        ))
        self.assertTrue(app_main._should_audit("DELETE", "/api/v1/audit-logs"))
        self.assertFalse(app_main._should_audit("GET", "/api/v1/chat/turns/turn-1/stream"))

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        request = SimpleNamespace(
            method="PATCH",
            url=SimpleNamespace(path="/api/v1/chat/turns/turn-1/guidance"),
            state=SimpleNamespace(audit_identity=(7, "auditor", "admin")),
            headers={"x-forwarded-for": "203.0.113.99"},
            cookies={},
            client=SimpleNamespace(host="10.0.0.8"),
        )

        async def failing(_request):
            raise RuntimeError("handler failed")

        with patch.object(app_main, "SessionLocal", factory), patch.object(
            app_main.settings, "TRUST_PROXY_HEADERS", False
        ):
            with self.assertRaisesRegex(RuntimeError, "handler failed"):
                asyncio.run(app_main.audit_log_middleware(request, failing))

        db = factory()
        try:
            row = db.query(AuditLog).one()
            self.assertEqual(row.status_code, 500)
            self.assertEqual(row.ip, "10.0.0.8")
            self.assertEqual(row.user_id, 7)
            self.assertEqual(row.path, "/api/v1/chat/turns/turn-1/guidance")
        finally:
            db.close()
            engine.dispose()


class PublicAgentIsolationTests(unittest.TestCase):
    def test_default_workspace_is_not_application_repository(self):
        self.assertNotEqual(WORKSPACE_DIR, BASE_DIR)
        self.assertNotEqual(WORKSPACE_DIR.parent, BASE_DIR.parent)
        self.assertFalse((WORKSPACE_DIR / ".env").exists())

    def test_public_agent_cannot_reenable_host_mutation_tools(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            agent = Agent(
                name="public",
                is_public=True,
                builtin_tools=json.dumps(sorted(builtin_tools.TOOLS)),
            )
            db.add(agent)
            db.commit()
            effective = builtin_tools.effective_tool_names(db, agent)
            self.assertFalse(
                builtin_tools.PUBLIC_AGENT_DENIED_TOOLS & effective
            )
            self.assertIn("web_search", effective)
        finally:
            db.close()
            engine.dispose()


class RunWorkspaceIsolationTests(unittest.TestCase):
    def test_workspaces_are_unique_per_user_and_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            with patch.object(builtin_tools, "WORKSPACE_DIR", root):
                first = builtin_tools.workspace_for_run(1, "run-a")
                second = builtin_tools.workspace_for_run(1, "run-b")
                other_user = builtin_tools.workspace_for_run(2, "run-a")
            self.assertEqual(len({first, second, other_user}), 3)
            self.assertTrue(all(path.is_dir() for path in (first, second, other_user)))
            self.assertTrue(all(path.is_relative_to(root) for path in (first, second, other_user)))

    def test_workspace_boundary_includes_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            with patch.object(builtin_tools, "WORKSPACE_DIR", root):
                first = builtin_tools.workspace_for_run(7, "same-run", 11)
                other_agent = builtin_tools.workspace_for_run(7, "same-run", 12)
            self.assertNotEqual(first, other_agent)
            self.assertIn("user_7", first.parts)
            self.assertIn("agent_11", first.parts)


class PersonalWeixinIsolationTests(unittest.TestCase):
    def test_local_token_list_excludes_unbound_webhook_placeholder(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        db = factory()
        try:
            db.add_all([
                Channel(
                    name="未绑定", type=weixin_channel.CHANNEL_TYPE,
                    path_key="unbound", token="random-webhook-placeholder",
                    connection_status="unbound", account_id="", created_by=7,
                ),
                Channel(
                    name="已连接", type=weixin_channel.CHANNEL_TYPE,
                    path_key="connected", token="tencent-issued-token",
                    connection_status="connected", account_id="wx-bot", created_by=7,
                ),
            ])
            db.commit()
            with patch.object(weixin_channel, "SessionLocal", factory):
                self.assertEqual(
                    weixin_channel.manager._local_tokens(7),
                    ["tencent-issued-token"],
                )
        finally:
            db.close()
            engine.dispose()

    def test_thread_keys_are_stable_but_isolated_by_channel_sender_and_agent(self):
        key = weixin_channel._session_id(1, "owner@im.wechat", 9)
        self.assertEqual(key, weixin_channel._session_id(1, "owner@im.wechat", 9))
        self.assertNotEqual(key, weixin_channel._session_id(2, "owner@im.wechat", 9))
        self.assertNotEqual(key, weixin_channel._session_id(1, "other@im.wechat", 9))
        self.assertNotEqual(key, weixin_channel._session_id(1, "owner@im.wechat", 10))

    def test_only_official_https_api_hosts_are_accepted(self):
        self.assertEqual(
            weixin_channel._allowed_base_url("https://ilinkai.weixin.qq.com/"),
            "https://ilinkai.weixin.qq.com",
        )
        with self.assertRaises(ValueError):
            weixin_channel._allowed_base_url("http://ilinkai.weixin.qq.com")
        with self.assertRaises(ValueError):
            weixin_channel._allowed_base_url("https://weixin.qq.com.attacker.example")

    def test_inbound_parser_uses_text_and_voice_transcript_only(self):
        message = {"item_list": [
            {"type": 1, "text_item": {"text": "第一段"}},
            {"type": 3, "voice_item": {"text": "语音转写"}},
            {"type": 4, "file_item": {"file_name": "secret.txt"}},
        ]}
        self.assertEqual(weixin_channel._extract_text(message), "第一段\n语音转写")

    def test_personal_channel_response_never_exposes_bot_token(self):
        from backend.api.channels import _channel_out

        channel = SimpleNamespace(
            id=4, name="我的微信", type=weixin_channel.CHANNEL_TYPE, agent_id=9,
            path_key="internal", token="secret-bot-token", app_id="", app_secret="",
            enabled=True, created_by=7, account_id="bot-account",
            connection_status="connected", last_error="", last_inbound_at=None,
            last_outbound_at=None,
        )
        user = SimpleNamespace(id=7, role="user")
        with patch("backend.api.channels.can_manage", return_value=True):
            output = _channel_out(channel, "个人助手", user)
        self.assertEqual(output.token, "")
        self.assertEqual(output.webhook_url, "")
        self.assertEqual(output.workspace_key, "user_7/agent_9")

    def test_sensitive_paths_are_denied_inside_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            context = builtin_tools.BuiltinToolContext(root=Path(temp))
            result = json.loads(
                __import__("asyncio").run(
                    builtin_tools.execute("read", {"path": ".env"}, context)
                )
            )
            self.assertFalse(result["ok"])
            self.assertIn("敏感路径", result["error"])

    def test_shell_requires_external_os_sandbox(self):
        with tempfile.TemporaryDirectory() as temp:
            context = builtin_tools.BuiltinToolContext(root=Path(temp))
            with (
                patch.object(builtin_tools.settings, "SHELL_TOOL_ENABLED", True),
                patch.object(builtin_tools.settings, "SHELL_SANDBOX_COMMAND", ""),
            ):
                result = json.loads(
                    __import__("asyncio").run(
                        builtin_tools.execute(
                            "shell",
                            {
                                "shell": "powershell",
                                "script": "Get-ChildItem",
                                "confirm": True,
                            },
                            context,
                        )
                    )
                )
            self.assertFalse(result["ok"])
            self.assertIn("OS 级低权限沙箱", result["error"])


class OneTimeApprovalTests(unittest.TestCase):
    def setUp(self):
        from backend import guardrail_policies, guardrails
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        for module in (guardrails, guardrail_policies):
            patcher = patch.object(module, "SessionLocal", self.factory)
            patcher.start()
            self.addCleanup(patcher.stop)
        db = self.factory()
        from backend.models import User
        db.add(User(id=7, username="approval-user", password_hash="x"))
        db.add(Agent(id=9, name="approval-agent"))
        db.add(Agent(id=13, name="approval-child-agent"))
        db.commit()
        db.close()

    def tearDown(self):
        self.engine.dispose()

    def test_mutation_pauses_until_server_token_then_consumes_once(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            approvals, "SessionLocal", self.factory
        ):
            context = builtin_tools.BuiltinToolContext(
                root=Path(temp),
                user_id=7,
                agent_id=9,
                run_id="run-approval",
            )
            args = {"path": "ok.txt", "content": "safe", "confirm": True}
            with self.assertRaises(approvals.ApprovalRequired):
                asyncio.run(builtin_tools.execute("write", args, context))

            token = approvals.issue("run-approval", 7, 9, "write")
            context.approval_tokens = [token]
            result = json.loads(asyncio.run(builtin_tools.execute("write", args, context)))
            self.assertTrue(result["ok"])

            args["path"] = "second.txt"
            with self.assertRaises(approvals.ApprovalRequired):
                asyncio.run(builtin_tools.execute("write", args, context))

    def test_waiting_job_issues_approval_for_actual_inline_child_agent(self):
        with patch.object(approvals, "SessionLocal", self.factory), patch.object(
            jobs, "SessionLocal", self.factory
        ):
            job_id = jobs.enqueue(7, 9, "chat", {
                "inputs": {"query": "需要子智能体处理"},
                "session_id": "approval-child-session",
            })
            claimed = jobs.claim_next("approval-worker")
            self.assertEqual(claimed.id, job_id)
            self.assertTrue(jobs.wait_for_approval(
                job_id,
                "approval-worker",
                claimed.lease_token,
                "browser_click",
                "子智能体点击操作",
                approval_agent_id=13,
                execution_context={
                    "parent_run_id": job_id,
                    "child_run_id": "inline-child-run",
                },
            ))
            self.assertTrue(jobs.approve_waiting(job_id, 7))
            payload = jobs.view(job_id, 7).payload
            token = payload["approval_tokens"][-1]
            self.assertFalse(approvals.consume(
                [token], run_id=job_id, user_id=7, agent_id=9,
                scope="browser_click",
            ))
            self.assertTrue(approvals.consume(
                [token], run_id=job_id, user_id=7, agent_id=13,
                scope="browser_click",
            ))
            resumed = jobs.claim_next("approval-worker-2")
            self.assertEqual(resumed.id, job_id)
            self.assertTrue(jobs.finish(
                job_id,
                "approval-worker-2",
                resumed.lease_token,
                {"answer": "公开结果", "reasoning": "不得持久化的内部推理"},
            ))
            self.assertNotIn("reasoning", jobs.view(job_id, 7).result)
            self.assertEqual(jobs.view(job_id, 7).progress, "已完成")


class ExecutionSnapshotTests(unittest.TestCase):
    def test_enqueued_snapshot_does_not_follow_later_agent_changes(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            from backend.models import Skill, User
            user = User(username="snapshot-user", password_hash="x")
            skill = Skill(
                name="snapshot-skill",
                description="before",
                instructions="original instructions",
                resources="[]",
                enabled=True,
            )
            db.add_all([user, skill])
            db.flush()
            agent = Agent(
                name="snapshot-agent",
                created_by=user.id,
                skill_ids=json.dumps([skill.id]),
                builtin_tools=json.dumps(["web_search"]),
            )
            db.add(agent)
            db.flush()
            first = harness_registry.create_version(
                db,
                agent,
                system_prompt="immutable v1",
                created_by=user.id,
                publish=True,
            )
            db.commit()

            snapshot = build_execution_snapshot(db, agent, "question")
            self.assertEqual(snapshot["harness"]["id"], first.id)
            self.assertEqual(snapshot["harness"]["system_prompt"], "immutable v1")
            self.assertEqual(snapshot["skills"][0]["instructions"], "original instructions")

            harness_registry.create_version(
                db,
                agent,
                system_prompt="new v2",
                created_by=user.id,
                publish=True,
            )
            skill.instructions = "mutated after enqueue"
            agent.builtin_tools = "[]"
            db.commit()

            self.assertEqual(snapshot["harness"]["system_prompt"], "immutable v1")
            self.assertEqual(snapshot["skills"][0]["instructions"], "original instructions")
            self.assertEqual(snapshot["builtin_tools"], ["web_search"])
        finally:
            db.close()
            engine.dispose()


class QueueConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory.begin() as db:
            db.add_all([
                User(id=1, username="queue-owner-1", password_hash="x", is_active=True),
                User(id=2, username="queue-owner-2", password_hash="x", is_active=True),
            ])
        self.patch = patch.object(jobs, "SessionLocal", self.factory)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.engine.dispose()

    def test_idempotency_lease_and_session_serialisation(self):
        payload = {"session_id": "thread-1", "query": "a"}
        first = jobs.enqueue(
            1, 1, "chat", payload, idempotency_key="request-1"
        )
        duplicate = jobs.enqueue(
            1, 1, "chat", payload, idempotency_key="request-1"
        )
        self.assertEqual(first, duplicate)

        second = jobs.enqueue(1, 1, "chat", payload)
        claimed_first = jobs.claim_next("worker-a")
        self.assertEqual(claimed_first.id, first)
        self.assertIsNone(jobs.claim_next("worker-b"))

        self.assertFalse(
            jobs.finish(first, "worker-a", "stale-token", {"answer": "bad"})
        )
        self.assertTrue(
            jobs.finish(
                first,
                claimed_first.worker_id,
                claimed_first.lease_token,
                {"answer": "ok"},
            )
        )
        claimed_second = jobs.claim_next("worker-b")
        self.assertEqual(claimed_second.id, second)

        db = self.factory()
        try:
            sequences = [
                value for (value,) in db.query(Item.sequence)
                .filter_by(turn_id=first)
                .order_by(Item.sequence)
                .all()
            ]
            self.assertEqual(sequences, sorted(set(sequences)))
        finally:
            db.close()

    def test_stale_attempts_end_in_dead_letter(self):
        job_id = jobs.enqueue(1, 1, "chat", {"query": "retry"})
        view = jobs.claim_next("worker-a")
        db = self.factory()
        try:
            row = db.get(Job, job_id)
            row.attempt_count = row.max_attempts
            row.heartbeat_at = None
            db.commit()
        finally:
            db.close()
        self.assertEqual(jobs.requeue_stale(1), 1)
        final = jobs.view(job_id)
        self.assertEqual(final.status, jobs.DEAD_LETTER)
        self.assertIn("最大重试", final.error)
        self.assertFalse(
            jobs.finish(job_id, view.worker_id, view.lease_token, {"answer": "late"})
        )

    def test_parent_cancel_cascades_to_subagent_tree(self):
        parent = jobs.enqueue(1, 1, "chat", {"session_id": "parent"})
        child = jobs.enqueue(1, 2, "chat", {
            "session_id": "child",
            "source": "subagent",
            "parent_run_id": parent,
        })
        grandchild = jobs.enqueue(1, 3, "chat", {
            "session_id": "grandchild",
            "source": "subagent",
            "parent_run_id": child,
        })
        self.assertTrue(asyncio.run(jobs.request_cancel(parent, 1)))
        self.assertEqual(jobs.view(parent).status, jobs.CANCELLED)
        self.assertEqual(jobs.view(child).status, jobs.CANCELLED)
        self.assertEqual(jobs.view(grandchild).status, jobs.CANCELLED)

    def test_private_async_child_requires_parent_snapshot_binding(self):
        parent_payload = {
            "execution_snapshot": {
                "agent": {"id": 10},
                "sub_agents": [{"agent": {"id": 20}, "sub_agents": []}],
            }
        }
        parent = jobs.enqueue(1, 10, "chat", parent_payload)
        child_payload = {
            "source": "subagent",
            "parent_run_id": parent,
            "parent_agent_id": 10,
            "delegation_authorized": True,
        }
        child = jobs.enqueue(1, 20, "chat", child_payload)
        db = self.factory()
        try:
            view = jobs._view(db.get(Job, child))
            self.assertTrue(worker._delegated_agent_access(
                db, view, view.payload, 20
            ))
            self.assertFalse(worker._delegated_agent_access(
                db, view, view.payload, 30
            ))
            forged = dict(view.payload)
            forged["parent_agent_id"] = 99
            self.assertFalse(worker._delegated_agent_access(db, view, forged, 20))
        finally:
            db.close()

    def test_transient_retry_waits_and_then_dead_letters(self):
        job_id = jobs.enqueue(1, 1, "chat", {"session_id": "retry"})
        first = jobs.claim_next("worker-a")
        with patch.object(jobs.settings, "JOB_RETRY_BASE_SECONDS", 60):
            self.assertEqual(
                jobs.retry_or_dead_letter(
                    job_id, first.worker_id, first.lease_token, "timeout"
                ),
                jobs.PENDING,
            )
        self.assertIsNone(jobs.claim_next("worker-b"))
        db = self.factory()
        try:
            row = db.get(Job, job_id)
            row.next_attempt_at = None
            row.attempt_count = row.max_attempts - 1
            db.commit()
        finally:
            db.close()
        last = jobs.claim_next("worker-b")
        self.assertEqual(
            jobs.retry_or_dead_letter(
                job_id, last.worker_id, last.lease_token, "timeout again"
            ),
            jobs.DEAD_LETTER,
        )
        self.assertEqual(jobs.view(job_id).status, jobs.DEAD_LETTER)

    def test_user_quota_and_fair_claiming(self):
        with patch.object(jobs.settings, "JOB_MAX_INFLIGHT_PER_USER", 2), \
             patch.object(jobs.settings, "JOB_MAX_RUNNING_PER_USER", 1), \
             patch.object(jobs.settings, "JOB_MAX_QUEUED_GLOBAL", 10):
            first = jobs.enqueue(
                1, 1, "chat", {"session_id": "a1"},
                idempotency_key="same-request",
            )
            self.assertEqual(
                jobs.enqueue(
                    1, 1, "chat", {"session_id": "a1"},
                    idempotency_key="same-request",
                ),
                first,
            )
            jobs.enqueue(1, 1, "chat", {"session_id": "a2"})
            with self.assertRaises(jobs.JobQuotaExceeded):
                jobs.enqueue(1, 1, "chat", {"session_id": "a3"})
            other = jobs.enqueue(2, 1, "chat", {"session_id": "b1"})
            self.assertEqual(jobs.claim_next("worker-a").id, first)
            self.assertEqual(jobs.claim_next("worker-b").id, other)

    def test_priority_precedes_age_without_breaking_claiming(self):
        low = jobs.enqueue(
            1, 1, "chat", {"session_id": "web", "source": "web"}
        )
        high = jobs.enqueue(
            2, 1, "chat", {"session_id": "cron", "source": "cron"}
        )
        claimed = jobs.claim_next("priority-worker")
        self.assertEqual(claimed.id, high)
        self.assertEqual(claimed.priority, 20)
        self.assertEqual(jobs.view(low).status, jobs.PENDING)

    def test_partial_stream_is_visible_across_processes(self):
        job_id = jobs.enqueue(1, 1, "chat", {"query": "stream"})
        view = jobs.claim_next("worker-a")
        with patch.object(jobs.settings, "JOB_PARTIAL_FLUSH_CHARS", 1):
            jobs.append_partial(job_id, "跨进程部分答案")
        # 模拟另一个 API 进程：没有 Worker 的进程内缓冲，只能从 DB 读取。
        jobs._partials.pop(job_id, None)
        jobs._partial_flush.pop(job_id, None)
        self.assertEqual(jobs.get_partial(job_id), "跨进程部分答案")
        self.assertTrue(jobs.finish(
            job_id, view.worker_id, view.lease_token, {"answer": "最终答案"}
        ))
        self.assertEqual(jobs.get_partial(job_id), "")

    def test_runtime_events_have_identity_and_cursor_replay(self):
        job_id = jobs.enqueue(1, 1, "chat", {"query": "event stream"})
        view = jobs.claim_next("worker-a")
        cancelled, progress_meta = jobs.set_progress(
            job_id,
            view.worker_id,
            view.lease_token,
            "正在规划",
            include_event=True,
        )
        self.assertFalse(cancelled)
        for key in ("task_id", "event_id", "timestamp", "revision"):
            self.assertTrue(progress_meta.get(key) is not None)
        plan_meta = jobs.append_event(
            job_id,
            view.worker_id,
            view.lease_token,
            "plan.created",
            {
                "revision": 1,
                "steps": [
                    {"id": "step_1", "step": "规划", "status": "in_progress"},
                    {"id": "step_2", "step": "执行", "status": "pending"},
                ],
            },
        )
        self.assertGreater(plan_meta["revision"], progress_meta["revision"])
        with patch.object(chat_api, "SessionLocal", self.factory):
            replay = chat_api._persisted_events_after(
                job_id, progress_meta["revision"]
            )
        self.assertEqual([event["event_type"] for event in replay], ["plan.created"])
        self.assertEqual(replay[0]["event_id"], plan_meta["event_id"])

    def test_running_job_guidance_can_be_edited_cancelled_and_claimed_once(self):
        job_id = jobs.enqueue(1, 1, "chat", {"session_id": "guided"})
        view = jobs.claim_next("worker-a")
        first = jobs.add_guidance(job_id, 1, "先保留结论")
        edited = jobs.update_guidance(first["id"], 1, "先保留结论，再补充依据")
        self.assertEqual(edited["content"], "先保留结论，再补充依据")

        second = jobs.add_guidance(job_id, 1, "这条撤回")
        self.assertTrue(jobs.cancel_guidance(second["id"], 1))
        with patch.object(jobs.settings, "JOB_GUIDANCE_GRACE_SECONDS", 0):
            claimed = jobs.take_guidance(job_id, view.worker_id, view.lease_token)
        self.assertEqual(claimed, [{
            "id": first["id"], "content": "先保留结论，再补充依据",
        }])
        with patch.object(jobs.settings, "JOB_GUIDANCE_GRACE_SECONDS", 0):
            self.assertEqual(jobs.take_guidance(
                job_id, view.worker_id, view.lease_token
            ), [])
        self.assertEqual(jobs.pending_guidance(job_id, 1), [])

    def test_redirect_interrupt_preserves_old_turn_and_queues_auditable_successor(self):
        job_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "duplex-thread",
            "inputs": {"query": "查询深圳政策"},
            "approval_policy": "auto",
        })
        running = jobs.claim_next("worker-duplex")
        pending = jobs.add_guidance(job_id, 1, "补充旧目标")

        result = asyncio.run(jobs.redirect_job(job_id, 1, "改为查询广东省政策"))
        successor = result["successor"]
        self.assertEqual(result["interrupted_turn_id"], job_id)
        self.assertEqual(successor.status, jobs.PENDING)
        self.assertEqual(successor.payload["session_id"], "duplex-thread")
        self.assertEqual(successor.payload["continuation_of_turn_id"], job_id)
        self.assertEqual(successor.payload["duplex_control"], {
            "mode": "redirect", "interrupted_turn_id": job_id,
        })
        self.assertEqual(successor.payload["approval_policy"], "auto")
        self.assertEqual(successor.payload["approval_tokens"], [])
        self.assertTrue(jobs.view(job_id, 1).cancel_requested)

        db = self.factory()
        try:
            self.assertEqual(db.get(JobGuidance, pending["id"]).status, jobs.GUIDANCE_CANCELLED)
            old_events = [
                row.name for row in db.query(Item).filter(Item.turn_id == job_id).all()
            ]
            new_events = [
                row.name for row in db.query(Item).filter(Item.turn_id == successor.id).all()
            ]
            self.assertIn("interaction.interrupt.received", old_events)
            self.assertIn("interaction.redirect.queued", old_events)
            self.assertIn("interaction.redirect.created", new_events)
        finally:
            db.close()

        # 同线程串行：旧 Turn 真正进入取消终态后，后继 Turn 才能被领取。
        self.assertIsNone(jobs.claim_next("worker-too-early"))
        self.assertTrue(jobs.mark_cancelled(
            job_id, running.worker_id, running.lease_token
        ))
        claimed = jobs.claim_next("worker-successor")
        self.assertEqual(claimed.id, successor.id)

    def test_redirect_interrupt_cannot_cross_user_boundary(self):
        job_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "private-thread", "inputs": {"query": "私有任务"},
        })
        with self.assertRaises(LookupError):
            asyncio.run(jobs.redirect_job(job_id, 2, "尝试越权重定向"))
        self.assertFalse(jobs.view(job_id, 1).cancel_requested)

    def test_pending_guidance_can_be_promoted_to_current_goal(self):
        job_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "integrated-control",
            "inputs": {"query": "原目标"},
        })
        running = jobs.claim_next("worker-integrated")
        guidance = jobs.add_guidance(job_id, 1, "新的当前目标")

        result = asyncio.run(jobs.redirect_staged_message(
            "guidance", guidance["id"], job_id, 1,
        ))
        successor = result["successor"]
        self.assertEqual(result["source_kind"], "guidance")
        self.assertEqual(successor.payload["inputs"]["query"], "新的当前目标")
        self.assertEqual(successor.payload["session_id"], "integrated-control")
        self.assertTrue(jobs.view(job_id, 1).cancel_requested)

        db = self.factory()
        try:
            self.assertEqual(
                db.get(JobGuidance, guidance["id"]).status,
                jobs.GUIDANCE_CANCELLED,
            )
        finally:
            db.close()

        self.assertTrue(jobs.mark_cancelled(
            job_id, running.worker_id, running.lease_token,
        ))
        self.assertEqual(jobs.claim_next("worker-successor").id, successor.id)

    def test_pending_queue_can_be_promoted_without_losing_audit_history(self):
        job_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "integrated-queue",
            "inputs": {"query": "原目标"},
        })
        jobs.claim_next("worker-integrated")
        queued_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "integrated-queue",
            "inputs": {"query": "排队的新目标"},
        })

        result = asyncio.run(jobs.redirect_staged_message(
            "queue", queued_id, job_id, 1,
        ))
        self.assertEqual(result["source_kind"], "queue")
        self.assertEqual(result["successor"].payload["inputs"]["query"], "排队的新目标")
        self.assertEqual(jobs.view(queued_id, 1).status, jobs.CANCELLED)

        db = self.factory()
        try:
            source_turn = db.get(Turn, queued_id)
            self.assertEqual(source_turn.status, "cancelled")
            cancelled = db.query(Item).filter(
                Item.turn_id == queued_id,
                Item.name == "turn.cancelled",
            ).one()
            self.assertEqual(
                json.loads(cancelled.payload)["reason"],
                "converted_to_redirect",
            )
        finally:
            db.close()

    def test_structured_queue_cannot_be_promoted_to_current_goal(self):
        job_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "structured-redirect",
            "inputs": {"query": "原目标"},
        })
        jobs.claim_next("worker-integrated")
        queued_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "structured-redirect",
            "inputs": {"query": "带附件的新目标"},
            "attachment_docs": ["important.pdf"],
        })

        with self.assertRaises(RuntimeError):
            asyncio.run(jobs.redirect_staged_message(
                "queue", queued_id, job_id, 1,
            ))
        self.assertEqual(jobs.view(queued_id, 1).status, jobs.PENDING)
        self.assertFalse(jobs.view(job_id, 1).cancel_requested)

    def test_queued_message_cannot_be_promoted_across_conversations(self):
        job_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "current-conversation",
            "inputs": {"query": "原目标"},
        })
        jobs.claim_next("worker-integrated")
        queued_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "another-conversation",
            "inputs": {"query": "其他对话的消息"},
        })

        with self.assertRaises(LookupError):
            asyncio.run(jobs.redirect_staged_message(
                "queue", queued_id, job_id, 1,
            ))
        self.assertEqual(jobs.view(queued_id, 1).status, jobs.PENDING)
        self.assertFalse(jobs.view(job_id, 1).cancel_requested)

    def test_new_guidance_has_an_editable_grace_window_before_claim(self):
        job_id = jobs.enqueue(1, 1, "chat", {"session_id": "grace"})
        view = jobs.claim_next("worker-a")
        guidance = jobs.add_guidance(job_id, 1, "先让我编辑")

        with patch.object(jobs.settings, "JOB_GUIDANCE_GRACE_SECONDS", 3):
            self.assertEqual(
                jobs.take_guidance(job_id, view.worker_id, view.lease_token),
                [],
            )
            db = self.factory()
            try:
                db.get(JobGuidance, guidance["id"]).created_at = (
                    datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(seconds=4)
                )
                db.commit()
            finally:
                db.close()
            self.assertEqual(
                jobs.take_guidance(job_id, view.worker_id, view.lease_token),
                [{"id": guidance["id"], "content": "先让我编辑"}],
            )

    def test_claimed_guidance_is_replayed_after_stale_worker_requeue(self):
        job_id = jobs.enqueue(1, 1, "chat", {"session_id": "replay"})
        first_view = jobs.claim_next("worker-a")
        guidance = jobs.add_guidance(job_id, 1, "不要丢失这条引导")
        with patch.object(jobs.settings, "JOB_GUIDANCE_GRACE_SECONDS", 0):
            self.assertEqual(
                jobs.take_guidance(job_id, first_view.worker_id, first_view.lease_token),
                [{"id": guidance["id"], "content": "不要丢失这条引导"}],
            )
        db = self.factory()
        try:
            self.assertEqual(db.get(JobGuidance, guidance["id"]).status, jobs.GUIDANCE_CLAIMED)
            db.get(Job, job_id).heartbeat_at = None
            db.commit()
        finally:
            db.close()

        self.assertEqual(jobs.requeue_stale(1), 1)
        second_view = jobs.claim_next("worker-b")
        with patch.object(jobs.settings, "JOB_GUIDANCE_GRACE_SECONDS", 0):
            self.assertEqual(
                jobs.take_guidance(job_id, second_view.worker_id, second_view.lease_token),
                [{"id": guidance["id"], "content": "不要丢失这条引导"}],
            )

    def test_late_guidance_is_promoted_when_job_finishes(self):
        job_id = jobs.enqueue(1, 1, "chat", {"session_id": "late", "inputs": {"query": "first"}})
        view = jobs.claim_next("worker-a")
        guidance = jobs.add_guidance(job_id, 1, "改成后续排队消息")
        self.assertTrue(jobs.finish(
            job_id, view.worker_id, view.lease_token, {"answer": "done"}
        ))

        db = self.factory()
        try:
            self.assertEqual(db.get(JobGuidance, guidance["id"]).status, jobs.GUIDANCE_QUEUED)
            followup = db.query(Job).filter(Job.status == jobs.PENDING).one()
            payload = json.loads(followup.payload)
            self.assertEqual(payload["session_id"], "late")
            self.assertEqual(payload["inputs"]["query"], "改成后续排队消息")
        finally:
            db.close()

    def test_queued_edit_preserves_context_and_path_mismatch_is_non_mutating(self):
        running_id = jobs.enqueue(1, 1, "chat", {"session_id": "edit"})
        jobs.claim_next("worker-a")
        guidance = jobs.add_guidance(running_id, 1, "原始引导")
        with self.assertRaises(LookupError):
            jobs.update_guidance(
                guidance["id"], 1, "不应写入", job_id="different-job"
            )
        self.assertEqual(jobs.pending_guidance(running_id, 1)[0]["content"], "原始引导")

        queued_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "edit",
            "inputs": {"query": "old"},
            "attachment_docs": ["kept.txt"],
            "skill_ids": [7],
        })
        jobs.update_queued_message(queued_id, 1, "new")
        payload = jobs.view(queued_id, 1).payload
        self.assertEqual(payload["inputs"]["query"], "new")
        self.assertEqual(payload["attachment_docs"], ["kept.txt"])
        self.assertEqual(payload["skill_ids"], [7])

    def test_structured_queue_to_guidance_failure_keeps_original_job(self):
        target_id = jobs.enqueue(1, 1, "chat", {"session_id": "atomic"})
        jobs.claim_next("worker-a")
        source_id = jobs.enqueue(1, 1, "chat", {
            "session_id": "atomic",
            "inputs": {"query": "带附件消息"},
            "attachment_docs": ["important.pdf"],
        })
        with self.assertRaises(RuntimeError):
            jobs.convert_queued_message_to_guidance(source_id, target_id, 1)
        self.assertEqual(jobs.view(source_id, 1).status, jobs.PENDING)
        self.assertEqual(jobs.pending_guidance(target_id, 1), [])


class ArtifactLifecycleTests(unittest.TestCase):
    def test_corrupt_raster_is_rejected_before_registration(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            with tempfile.TemporaryDirectory() as temp, patch.object(
                artifacts, "EXPORT_DIR", Path(temp)
            ):
                (Path(temp) / "broken.png").write_bytes(b"not-a-real-image")
                with self.assertRaisesRegex(ValueError, "无法解码"):
                    artifacts.register_many(
                        db,
                        owner_id=1,
                        run_id=None,
                        turn_id=None,
                        filenames=["broken.png"],
                    )
        finally:
            db.close()
            engine.dispose()

    def test_tool_artifact_is_registered_downloadable_and_run_cleanup_is_complete(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        with tempfile.TemporaryDirectory() as temp, patch.object(
            builtin_tools, "EXPORT_DIR", Path(temp)
        ), patch.object(artifacts, "EXPORT_DIR", Path(temp)), patch.object(
            jobs, "SessionLocal", factory
        ):
            context = builtin_tools.BuiltinToolContext(root=Path(temp))
            result = json.loads(asyncio.run(builtin_tools.execute(
                "html_generate",
                {"title": "report", "html": "<main>ok</main>", "confirm": True},
                context,
            )))
            self.assertEqual(context.artifacts, [result["file"]])

            db = factory()
            try:
                from backend.models import Artifact, User
                user = User(username="artifact-user", password_hash="x")
                db.add(user)
                db.commit()
                run_id = jobs.enqueue(user.id, None, "chat", {"query": "artifact"})
                rows = artifacts.register_many(
                    db,
                    owner_id=user.id,
                    run_id=run_id,
                    turn_id=run_id,
                    filenames=context.artifacts,
                )
                db.commit()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].owner_id, user.id)

                row = db.get(Job, run_id)
                row.status = jobs.DONE
                row.updated_at = datetime.datetime(2000, 1, 1)
                db.commit()
            finally:
                db.close()

            self.assertEqual(jobs.cleanup_old(1), 1)
            db = factory()
            try:
                from backend.models import Artifact
                self.assertGreater(db.query(Item).filter_by(turn_id=run_id).count(), 0)
                self.assertIsNotNone(db.get(Turn, run_id))
                artifact = db.query(Artifact).one()
                self.assertIsNone(artifact.run_id)
                self.assertTrue((Path(temp) / artifact.filename).is_file())
            finally:
                db.close()
        engine.dispose()


class EnvelopeEncryptionTests(unittest.TestCase):
    def test_sensitive_orm_fields_and_job_snapshots_are_encrypted_at_rest(self):
        from backend.secret_store import PREFIX

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        db = factory()
        try:
            provider = ModelProvider(
                name="secure-provider",
                api_key="provider-secret",
                auth_extra='{"refresh_token":"oauth-secret"}',
                custom_headers='{"Authorization":"Bearer header-secret"}',
            )
            mcp = McpServer(
                name="secure-mcp",
                url="https://example.com/mcp",
                headers='{"Authorization":"Bearer mcp-secret"}',
            )
            channel = Channel(
                name="secure-channel",
                path_key="unique-channel-path",
                token="channel-token",
                app_secret="channel-app-secret",
            )
            job = Job(id="encrypted-job", payload='{"api_key":"snapshot-secret"}')
            db.add_all([provider, mcp, channel, job])
            db.commit()

            self.assertEqual(provider.api_key, "provider-secret")
            self.assertEqual(channel.token, "channel-token")
            with engine.connect() as connection:
                raw_provider = connection.execute(
                    text("SELECT api_key, auth_extra, custom_headers FROM model_providers")
                ).one()
                raw_mcp = connection.execute(
                    text("SELECT headers FROM mcp_servers")
                ).scalar_one()
                raw_channel = connection.execute(
                    text("SELECT token, app_secret FROM channels")
                ).one()
                raw_job = connection.execute(
                    text("SELECT payload FROM jobs")
                ).scalar_one()
            for stored in [*raw_provider, raw_mcp, *raw_channel, raw_job]:
                self.assertTrue(stored.startswith(PREFIX))
                self.assertNotIn("secret", stored)
        finally:
            db.close()
            engine.dispose()

    def test_plaintext_upgrade_is_idempotent_and_readable(self):
        from backend.secret_store import PREFIX, migrate_plaintext_secrets

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO model_providers "
                "(id,name,provider_type,base_url,api_key,model_id,model_name,"
                "model_reasoning,model_input,context_window,auth_extra,wire_api,auth_type,auth_header,api_version,"
                "api_version_mode,custom_headers,extra_body,model_list_path,"
                "reasoning_effort,max_tokens,max_tokens_param,timeout_ms,"
                "max_retries,stream_max_retries,stream_idle_timeout_ms,"
                "supports_temperature,enabled,is_public,created_at) VALUES "
                "(1,'legacy','openai','','legacy-key','model','','0','[\"text\"]',0,'','chat_completions',"
                "'bearer','','','none','{}','{}','/models','',8192,'auto',120000,"
                "3,3,300000,1,1,0,CURRENT_TIMESTAMP)"
            ))
        self.assertGreater(migrate_plaintext_secrets(engine), 0)
        self.assertEqual(migrate_plaintext_secrets(engine), 0)
        with engine.connect() as connection:
            stored = connection.execute(
                text("SELECT api_key FROM model_providers WHERE id=1")
            ).scalar_one()
        self.assertTrue(stored.startswith(PREFIX))
        db = sessionmaker(bind=engine)()
        try:
            self.assertEqual(db.get(ModelProvider, 1).api_key, "legacy-key")
        finally:
            db.close()
            engine.dispose()


class JwtRevocationTests(unittest.TestCase):
    def test_jti_and_token_version_revoke_existing_access_tokens(self):
        import jwt
        from fastapi import HTTPException
        from backend.config import settings
        from backend.security import create_token, resolve_access_token

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        db = factory()
        try:
            user = __import__("backend.models", fromlist=["User"]).User(
                username="jwt-user", password_hash="x"
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            token = create_token(user)
            claims = jwt.decode(
                token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
            )
            self.assertEqual(claims["typ"], "access")
            self.assertTrue(claims["jti"])
            self.assertEqual(claims["ver"], 0)
            self.assertEqual(resolve_access_token(token, db).id, user.id)

            user.token_version += 1
            db.commit()
            with self.assertRaises(HTTPException):
                resolve_access_token(token, db)
        finally:
            db.close()
            engine.dispose()

    def test_production_security_profile_rejects_shared_keys_and_insecure_cookie(self):
        from backend.config import security_config_problems, settings

        strong = "j" * 48
        with patch.multiple(
            settings,
            APP_ENV="production",
            JWT_SECRET=strong,
            SECRET_MASTER_KEY=strong,
            AUTH_COOKIE_SECURE=False,
            ALLOW_INSECURE_DEFAULTS=True,
            CORS_ORIGINS="*",
        ):
            problems = security_config_problems(False)
            with self.assertRaises(RuntimeError):
                app_main._enforce_security_config(False)
        self.assertTrue(any("必须与 JWT_SECRET 分离" in item for item in problems))
        self.assertTrue(any("AUTH_COOKIE_SECURE" in item for item in problems))
        self.assertTrue(any("CORS_ORIGINS" in item for item in problems))

        with patch.multiple(
            settings,
            APP_ENV="production",
            JWT_SECRET="j" * 48,
            SECRET_MASTER_KEY="m" * 48,
            AUTH_COOKIE_SECURE=True,
            ALLOW_INSECURE_DEFAULTS=False,
            CORS_ORIGINS="https://console.example.com",
        ):
            self.assertEqual(security_config_problems(False), [])


class CookieAuthenticationTests(unittest.TestCase):
    def test_login_sets_httponly_cookie_and_cookie_authenticates(self):
        from http.cookies import SimpleCookie
        from fastapi import Response
        from starlette.requests import Request
        from backend.api import auth as auth_api
        from backend.models import User
        from backend.schemas import LoginRequest
        from backend.security import get_current_user, hash_password, resolve_session_token

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            user = User(
                username="cookie-user",
                password_hash=hash_password("secret-pass"),
            )
            db.add(user)
            db.commit()
            request = Request({
                "type": "http",
                "method": "POST",
                "scheme": "https",
                "path": "/api/v1/auth/login",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 443),
                "query_string": b"",
            })
            response = Response()
            with patch.object(auth_api, "enforce"), patch.object(auth_api, "reset"):
                identity = auth_api.login(
                    LoginRequest(username="cookie-user", password="secret-pass"),
                    request,
                    response,
                    db,
                )
            cookie = response.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)
            self.assertIn("Secure", cookie)
            self.assertNotIn("Max-Age", cookie)
            self.assertNotIn("expires=", cookie.lower())
            self.assertNotIn("access_token", identity.model_dump())
            parsed = SimpleCookie()
            parsed.load(cookie)
            token = parsed["gca_session"].value
            cookie_request = Request({
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "path": "/api/v1/auth/me",
                "headers": [(
                    b"cookie",
                    f"gca_session={token}".encode(),
                )],
                "client": ("127.0.0.1", 1),
                "server": ("test", 443),
                "query_string": b"",
            })
            self.assertEqual(
                get_current_user(cookie_request, None, db).id,
                user.id,
            )
            logout_response = Response()
            auth_api.logout(cookie_request, logout_response, db)
            self.assertIn("Max-Age=0", logout_response.headers["set-cookie"])
            with self.assertRaises(auth_api.HTTPException):
                resolve_session_token(token, db)
        finally:
            db.close()
            engine.dispose()

    def test_frontend_contains_no_jwt_storage_or_inline_script_handlers(self):
        root = Path(__file__).resolve().parents[1] / "frontend"
        text_value = "\n".join(
            path.read_text(encoding="utf-8")
            for path in root.rglob("*")
            if path.suffix in {".html", ".js"}
        )
        # Upgrading clients may remove the legacy credential, but must never
        # read or write it again. Keep every other reference disallowed.
        without_legacy_cleanup = re.sub(
            r"(?:localStorage|sessionStorage)\s*\.\s*removeItem\s*\(\s*(['\"])gca_token\1\s*\)",
            "", text_value,
        )
        self.assertNotIn("gca_token", without_legacy_cleanup)
        self.assertNotIn("onclick=", text_value)
        self.assertNotIn("<script>", text_value)


class AlembicMigrationTests(unittest.TestCase):
    def test_codex_chatgpt_provider_migration_repairs_legacy_route_and_model(self):
        import importlib

        migration = importlib.import_module(
            "migrations.versions.0012_repair_codex_chatgpt_models"
        )
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE model_providers ("
                "id INTEGER PRIMARY KEY, name VARCHAR(128), provider_type VARCHAR(32), "
                "base_url VARCHAR(512), wire_api VARCHAR(32), text_model VARCHAR(128))"
            ))
            connection.execute(text(
                "INSERT INTO model_providers VALUES "
                "(1, 'legacy-codex', 'openai', "
                "'https://chatgpt.com/backend-api/codex/', 'chat_completions', 'gpt-5')"
            ))

        with engine.begin() as connection:
            migration._repair_codex_chatgpt_providers(connection)
            repaired = connection.execute(text(
                "SELECT provider_type, wire_api, text_model FROM model_providers "
                "WHERE name='legacy-codex'"
            )).one()
            self.assertEqual(tuple(repaired), ("chatgpt", "responses", "gpt-5.6-sol"))
        engine.dispose()

    def test_legacy_conversation_backfill_is_visible_and_idempotent(self):
        import importlib

        migration = importlib.import_module(
            "migrations.versions.0011_audit_remediation"
        )
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users "
                "(id, username, password_hash, role, is_active, permissions, token_version, created_at) "
                "VALUES (1, 'legacy-owner', 'x', 'user', 1, '', 0, CURRENT_TIMESTAMP)"
            ))
            connection.execute(text(
                "CREATE TABLE conversations ("
                "id INTEGER PRIMARY KEY, user_id INTEGER, agent_id INTEGER, "
                "source VARCHAR(16), query TEXT, answer TEXT, created_at DATETIME, "
                "export_files TEXT, session_id VARCHAR(40), title VARCHAR(80), "
                "project_id INTEGER, is_pinned BOOLEAN, is_archived BOOLEAN)"
            ))
            connection.execute(text(
                "INSERT INTO conversations VALUES "
                "(18, 1, NULL, 'web', '旧问题', '旧回答', CURRENT_TIMESTAMP, "
                "'[]', 'legacy-session', '历史对话', NULL, 1, 0)"
            ))
            self.assertEqual(migration._backfill_legacy_conversations(connection), 1)
            self.assertEqual(migration._backfill_legacy_conversations(connection), 0)
            self.assertEqual(
                connection.execute(text(
                    "SELECT COUNT(*) FROM threads WHERE id='legacy-session'"
                )).scalar_one(),
                1,
            )
            self.assertEqual(
                connection.execute(text(
                    "SELECT COUNT(*) FROM turns WHERE thread_id='legacy-session'"
                )).scalar_one(),
                1,
            )
            roles = connection.execute(text(
                "SELECT role FROM items WHERE thread_id='legacy-session' ORDER BY sequence"
            )).scalars().all()
            self.assertEqual(roles, ["user", "assistant"])
        engine.dispose()

    def test_fresh_and_legacy_databases_reach_versioned_head_idempotently(self):
        from backend import migrations as migration_runtime

        with tempfile.TemporaryDirectory() as temp:
            for legacy in (False, True):
                db_path = Path(temp) / f"{'legacy' if legacy else 'fresh'}.db"
                engine = create_engine(f"sqlite:///{db_path.as_posix()}")
                if legacy:
                    with engine.begin() as connection:
                        connection.execute(text(
                            "CREATE TABLE users ("
                            "id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL,"
                            "password_hash VARCHAR(256) NOT NULL, role VARCHAR(16) NOT NULL,"
                            "is_active BOOLEAN NOT NULL, permissions TEXT NOT NULL,"
                            "created_at DATETIME NOT NULL)"
                        ))
                        connection.execute(text(
                            "INSERT INTO users VALUES "
                            "(1,'legacy-user','x','user',1,'',CURRENT_TIMESTAMP)"
                        ))
                with patch.object(migration_runtime, "engine", engine), patch.object(
                    migration_runtime, "DATA_DIR", Path(temp)
                ):
                    migration_runtime.run_schema_migrations()
                    migration_runtime.run_schema_migrations()
                inspector = __import__("sqlalchemy").inspect(engine)
                self.assertIn("alembic_version", inspector.get_table_names())
                self.assertIn(
                    "token_version",
                    {column["name"] for column in inspector.get_columns("users")},
                )
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar_one(),
                        migration_runtime.EXPECTED_SCHEMA_REVISION,
                    )
                engine.dispose()

    def test_image_capability_migration_preserves_referenced_provider(self):
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = importlib.import_module(
            "migrations.versions.0015_provider_image_capabilities"
        )
        engine = create_engine("sqlite:///:memory:")

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE model_providers ("
                "id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL)"
            ))
            connection.execute(text(
                "CREATE TABLE agents (id INTEGER PRIMARY KEY, provider_id INTEGER, "
                "FOREIGN KEY(provider_id) REFERENCES model_providers(id))"
            ))
            connection.execute(text(
                "INSERT INTO model_providers (id, name) VALUES (4, 'referenced')"
            ))
            connection.execute(text(
                "INSERT INTO agents (id, provider_id) VALUES (1, 4)"
            ))
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()

            self.assertEqual(
                connection.execute(text(
                    "SELECT name FROM model_providers WHERE id=4"
                )).scalar_one(),
                "referenced",
            )
            columns = {
                row[1] for row in connection.execute(
                    text("PRAGMA table_info(model_providers)")
                )
            }
            self.assertTrue({
                "image_generation_mode", "image_model", "supports_image_edit"
            }.issubset(columns))
        engine.dispose()

    def test_general_model_migration_drops_legacy_fields_without_breaking_foreign_keys(self):
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = importlib.import_module(
            "migrations.versions.0016_general_model_interface"
        )
        engine = create_engine("sqlite:///:memory:")

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE model_providers ("
                "id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
                "text_model VARCHAR(128), vision_model VARCHAR(128), "
                "image_generation_mode VARCHAR(24), image_model VARCHAR(128), "
                "supports_image_edit BOOLEAN, context_tokens INTEGER, "
                "max_output_tokens INTEGER)"
            ))
            connection.execute(text(
                "CREATE TABLE agents (id INTEGER PRIMARY KEY, provider_id INTEGER, "
                "FOREIGN KEY(provider_id) REFERENCES model_providers(id))"
            ))
            connection.execute(text(
                "INSERT INTO model_providers VALUES "
                "(4, 'referenced', 'old-text', 'old-vision', 'responses', "
                "'old-image', 1, 8192, 4096)"
            ))
            connection.execute(text(
                "INSERT INTO agents (id, provider_id) VALUES (1, 4)"
            ))
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()

            columns = {
                row[1] for row in connection.execute(
                    text("PRAGMA table_info(model_providers)")
                )
            }
            self.assertTrue(set(migration.NEW_COLUMNS).issubset(columns))
            self.assertTrue(set(migration.REMOVED_COLUMNS).isdisjoint(columns))
            self.assertEqual(
                connection.execute(text(
                    "SELECT model_id, model_input FROM model_providers WHERE id=4"
                )).one(),
                ("", '["text"]'),
            )
            self.assertEqual(
                connection.execute(text(
                    "SELECT provider_id FROM agents WHERE id=1"
                )).scalar_one(),
                4,
            )
        engine.dispose()


class HealthReadinessTests(unittest.TestCase):
    def test_health_checks_schema_worker_and_queue(self):
        from backend import migrations as migration_runtime
        from backend.health import collect_health

        with tempfile.TemporaryDirectory() as temp:
            engine = create_engine(
                f"sqlite:///{(Path(temp) / 'health.db').as_posix()}"
            )
            with patch.object(migration_runtime, "engine", engine), patch.object(
                migration_runtime, "DATA_DIR", Path(temp)
            ):
                migration_runtime.run_schema_migrations()
            factory = sessionmaker(bind=engine)
            with patch.object(jobs, "SessionLocal", factory):
                jobs.touch_worker("health-worker", 3)
            snapshot, ready = collect_health(
                engine, factory, require_worker=True
            )
            self.assertTrue(ready)
            self.assertTrue(snapshot["database"]["ok"])
            self.assertTrue(snapshot["migration"]["ok"])
            self.assertEqual(snapshot["workers"]["capacity"], 3)
            self.assertIn("deployment", snapshot)
            from backend.config import settings
            self.assertNotIn(settings.JWT_SECRET, json.dumps(snapshot))
            with patch.object(jobs, "SessionLocal", factory):
                jobs.remove_worker("health-worker")
            snapshot, ready = collect_health(
                engine, factory, require_worker=True
            )
            self.assertFalse(ready)
            self.assertEqual(snapshot["status"], "unavailable")
            engine.dispose()

    def test_health_requires_scheduler_and_exposes_dispatch_failure(self):
        from backend import migrations as migration_runtime
        from backend.health import collect_health
        from backend.models import SchedulerHeartbeat

        with tempfile.TemporaryDirectory() as temp:
            engine = create_engine(
                f"sqlite:///{(Path(temp) / 'scheduler-health.db').as_posix()}"
            )
            with patch.object(migration_runtime, "engine", engine), patch.object(
                migration_runtime, "DATA_DIR", Path(temp)
            ):
                migration_runtime.run_schema_migrations()
            factory = sessionmaker(bind=engine)
            snapshot, ready = collect_health(
                engine, factory, require_worker=False, require_scheduler=True
            )
            self.assertFalse(ready)
            self.assertEqual(snapshot["scheduler"]["active"], 0)

            db = factory()
            db.add(SchedulerHeartbeat(
                scheduler_id="scheduler-test",
                last_seen=datetime.datetime.now(datetime.timezone.utc),
                last_error="RuntimeError: poison schedule",
            ))
            db.commit()
            db.close()
            snapshot, ready = collect_health(
                engine, factory, require_worker=False, require_scheduler=True
            )
            self.assertTrue(ready)
            self.assertEqual(snapshot["status"], "degraded")
            self.assertEqual(snapshot["scheduler"]["active"], 1)
            self.assertIn("poison schedule", snapshot["scheduler"]["last_error"])
            engine.dispose()


class SharedEdgeStateTests(unittest.TestCase):
    def test_rate_limits_and_channel_dedupe_are_database_shared(self):
        from fastapi import HTTPException
        from backend import rate_limit
        from backend.api.channels import _dispatch_message
        from backend.models import User

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        with patch.object(rate_limit, "SessionLocal", factory):
            rate_limit.enforce("login", "same-client", 1, 60)
            with self.assertRaises(HTTPException):
                rate_limit.enforce("login", "same-client", 1, 60)
            rate_limit.reset("login", "same-client")
            rate_limit.enforce("login", "same-client", 1, 60)
            stale = RateLimitBucket(
                bucket_key="stale",
                count=1,
                window_start=datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=2),
            )
            db_for_limit = factory()
            db_for_limit.add(stale)
            db_for_limit.commit()
            db_for_limit.close()
            self.assertEqual(rate_limit.cleanup_expired(86400), 1)

        from backend import worker

        def broken_maintenance():
            raise RuntimeError("cleanup failed")

        self.assertFalse(asyncio.run(worker._run_maintenance(
            "test", broken_maintenance
        )))

        db = factory()
        try:
            owner = User(username="channel-owner", password_hash="x")
            db.add(owner)
            db.flush()
            agent = Agent(
                name="channel-agent", enabled=True, created_by=owner.id,
                builtin_tools="[]",
            )
            db.add(agent)
            db.flush()
            channel = Channel(
                name="wechat", type="wechat_mp", agent_id=agent.id,
                path_key="wechat-path", token="token", created_by=owner.id,
            )
            db.add(channel)
            db.commit()
            with patch.object(jobs, "SessionLocal", factory):
                first, first_push = _dispatch_message(
                    db, "message-123", channel, agent, "hello", claim_push=True
                )
                second, second_push = _dispatch_message(
                    db, "message-123", channel, agent, "hello", claim_push=True
                )
            self.assertEqual(first, second)
            self.assertTrue(first_push)
            self.assertFalse(second_push)
            self.assertEqual(db.query(Job).count(), 1)
        finally:
            db.close()
            engine.dispose()


class ImprovementGateTests(unittest.TestCase):
    def test_evaluation_is_computed_signed_and_separates_approval(self):
        from fastapi import HTTPException
        from backend.api.improvement import (
            approve_proposal, create_proposal, record_evaluation,
        )
        from backend.models import ImprovementProposal, User

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            creator = User(username="creator", password_hash="x", role="admin")
            reviewer = User(username="reviewer", password_hash="x", role="root")
            db.add_all([creator, reviewer])
            db.flush()
            agent = Agent(
                name="gate-agent", enabled=True, created_by=creator.id,
                builtin_tools="[]",
            )
            db.add(agent)
            db.flush()
            harness_registry.create_version(
                db, agent, system_prompt="baseline", change_summary="base",
                created_by=creator.id, publish=True,
            )
            baseline = Job(
                id="baseline-run",
                owner_id=creator.id,
                agent_id=agent.id,
                status=jobs.DONE,
                payload=json.dumps({
                    "harness_version": 1,
                    "inputs": {"query": "固定回归问题"},
                }),
                result=json.dumps({"answer": "基线答案"}),
            )
            db.add(baseline)
            thread = task_store.ensure_thread(
                db, thread_id="baseline-thread", owner_id=creator.id, agent_id=agent.id
            )
            baseline_turn = task_store.create_turn(
                db, thread=thread, input_text="固定回归问题",
                payload={"harness_version": 1}, turn_id=baseline.id,
            )
            task_store.finish_turn(db, baseline_turn, answer="基线答案")
            task_store.append_runtime_item(
                db, baseline.id, "verification.completed", {"passed": True}
            )
            db.commit()

            created = create_proposal({
                "agent_id": agent.id,
                "hypothesis": "更清晰",
                "system_prompt": "candidate",
                "evidence": [baseline.id],
            }, creator, db)
            proposal_id = created["id"]
            started = record_evaluation(
                proposal_id, {"outcome": "passed"}, creator, db
            )
            self.assertEqual(started["status"], "evaluating")
            candidate_id = started["evaluation"]["candidate_run_ids"][0]
            candidate = db.get(Job, candidate_id)
            candidate.status = jobs.DONE
            candidate.result = json.dumps({"answer": "候选答案"})
            task_store.append_runtime_item(
                db, candidate.id, "verification.completed", {"passed": True}
            )
            db.commit()

            evaluated = record_evaluation(
                proposal_id, {"outcome": "failed"}, creator, db
            )
            self.assertEqual(evaluated["status"], "evaluated")
            self.assertEqual(evaluated["evaluation"]["outcome"], "passed")
            self.assertEqual(len(evaluated["evaluation"]["signature"]), 64)
            with self.assertRaises(HTTPException):
                approve_proposal(proposal_id, creator, db)
            approved = approve_proposal(proposal_id, reviewer, db)
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approved["approved_by"], reviewer.id)
            self.assertEqual(db.get(ImprovementProposal, proposal_id).approved_by,
                             reviewer.id)
        finally:
            db.close()
            engine.dispose()


class OpenApiQueueTests(unittest.TestCase):
    def test_public_channel_open_api_and_subagent_preserve_limited_completion(self):
        from backend.api.channels import _job_payload
        from backend.api.open_api import _open_result

        summary = {
            "total": 2,
            "completed": 1,
            "failed": 0,
            "blocked": 1,
            "skipped": 0,
            "pending": 0,
            "in_progress": 0,
            "terminalized": True,
            "all_completed": False,
        }
        view = SimpleNamespace(
            id="limited-public-run",
            agent_id=9,
            status=jobs.DONE,
            progress="已完成（有未完成项）",
            result={
                "answer": "有限答复",
                "export_files": ["report.md"],
                "turn_id": "limited-public-run",
                "thread_id": "limited-public-thread",
                "completion_status": "completed_with_issues",
                "completion_issues": ["计划存在阻塞步骤：形成完整结论"],
                "plan_summary": summary,
            },
            error="",
            error_class="",
            payload={"session_id": "limited-public-thread"},
        )

        channel_payload = _job_payload(view)
        self.assertEqual(channel_payload["status"], jobs.DONE)
        self.assertEqual(
            channel_payload["task_status"], "completed_with_issues"
        )
        self.assertEqual(
            channel_payload["completion_status"], "completed_with_issues"
        )
        self.assertEqual(channel_payload["plan_summary"]["blocked"], 1)

        open_payload = _open_result(view).model_dump()
        self.assertEqual(open_payload["status"], jobs.DONE)
        self.assertEqual(open_payload["task_status"], "completed_with_issues")
        self.assertEqual(
            open_payload["completion_status"], "completed_with_issues"
        )
        self.assertEqual(open_payload["completion_issues"], [
            "计划存在阻塞步骤：形成完整结论"
        ])

        child_payload = builtin_tools._job_summary(view)
        self.assertEqual(child_payload["status"], jobs.DONE)
        self.assertEqual(
            child_payload["completion_status"], "completed_with_issues"
        )
        self.assertEqual(child_payload["plan_summary"]["blocked"], 1)

    def test_open_api_enqueues_snapshot_instead_of_running_harness_inline(self):
        from backend.api.open_api import _enqueue_open_chat
        from backend.models import ApiKey, User
        from backend.runtime import TaskInput

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            owner = User(username="api-owner", password_hash="x", role="root")
            db.add(owner)
            db.flush()
            agent = Agent(
                name="api-agent", enabled=True, created_by=owner.id,
                builtin_tools="[]",
            )
            db.add(agent)
            db.flush()
            harness_registry.create_version(
                db, agent, system_prompt="api", change_summary="base",
                created_by=owner.id, publish=True,
            )
            key = ApiKey(
                name="integration", key_hash="hash", prefix="sk-test",
                created_by=owner.id,
            )
            db.add(key)
            db.commit()
            job_id = _enqueue_open_chat(
                db, key, agent, TaskInput(query="queued request"),
                idempotency_key="open:test:1",
            )
            row = db.get(Job, job_id)
            payload = json.loads(row.payload)
            self.assertEqual(row.status, jobs.PENDING)
            self.assertEqual(payload["source"], "open_api")
            self.assertEqual(payload["inputs"]["query"], "queued request")
            self.assertEqual(payload["execution_snapshot"]["harness"]["version"], 1)
            items = (
                db.query(Item).filter_by(turn_id=job_id)
                .order_by(Item.sequence).all()
            )
            self.assertEqual(len(items), 3)
            self.assertEqual(items[-1].name, "task.queued")
        finally:
            db.close()
            engine.dispose()


class BrowserNetworkIsolationTests(unittest.TestCase):
    def test_profiles_are_disposable_and_browser_click_requires_approval(self):
        from backend.runtime import browser_cdp
        with tempfile.TemporaryDirectory() as temp, patch.object(
            browser_cdp, "DATA_DIR", Path(temp)
        ):
            first = Path(browser_cdp._new_profile())
            second = Path(browser_cdp._new_profile())
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir() and second.is_dir())
        self.assertTrue(builtin_tools.TOOLS["browser_click"].mutating)
        self.assertFalse(builtin_tools.TOOLS["browser_close"].mutating)

    def test_mcp_client_validates_every_http_request(self):
        from backend.llm import mcp_client
        request = __import__("httpx").Request("GET", "https://example.com/next")
        with patch.object(mcp_client, "validate_outbound_url") as validate:
            asyncio.run(mcp_client._validate_request(request))
        validate.assert_called_once_with("https://example.com/next")

    def test_browser_pump_checks_and_blocks_each_request(self):
        from backend.runtime import browser_cdp

        class FakeWebSocket:
            def __init__(self):
                self.sent = []

            async def send(self, message):
                self.sent.append(json.loads(message))

        class FakeProcess:
            def poll(self):
                return None

        async def exercise():
            session = browser_cdp.BrowserSession(
                "session", FakeProcess(), FakeWebSocket(), "profile", "1:run"
            )
            with patch.object(
                browser_cdp, "validate_outbound_url",
                side_effect=[None, ValueError("private target")],
            ) as validate:
                await browser_cdp._handle_paused_request(session, {
                    "requestId": "allowed",
                    "request": {"url": "https://example.com/app.js"},
                })
                await browser_cdp._handle_paused_request(session, {
                    "requestId": "blocked",
                    "request": {"url": "http://127.0.0.1/admin"},
                })
            self.assertEqual(validate.call_count, 2)
            self.assertEqual(
                [row["method"] for row in session.websocket.sent],
                ["Fetch.continueRequest", "Fetch.failRequest"],
            )
            self.assertEqual(
                session.websocket.sent[1]["params"]["errorReason"],
                "BlockedByClient",
            )

        asyncio.run(exercise())

    def test_browser_sessions_are_cleaned_by_run_owner(self):
        from backend.runtime import browser_cdp

        first = SimpleNamespace(owner_key="1:run")
        second = SimpleNamespace(owner_key="2:run")
        with patch.object(browser_cdp, "SESSIONS", {"first": first, "second": second}), \
             patch.object(browser_cdp, "close", new=AsyncMock()) as close:
            asyncio.run(browser_cdp.close_owner("1:run"))
        close.assert_awaited_once_with(first)


if __name__ == "__main__":
    unittest.main()
