"""Resume real encrypted checkpoints through the agent loop, without paid models."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import approvals
from backend.database import Base
from backend.models import Agent, Job, RuntimeCheckpoint, ToolInvocation, User
from backend.runtime import builtin_tools, orchestrator
from backend.runtime.durability import ExecutionStore, UnknownToolOutcome, bind_execution_lease
from backend.runtime.execution import InvocationPersistenceError


def call(identifier, path):
    return {"id": identifier, "type": "function", "function": {
        "name": "write", "arguments": json.dumps({"path": path, "content": "owned private data", "confirm": True}),
    }}


def tool_message(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


FINAL_MESSAGE = {"role": "assistant", "content": "操作已经完成，文件内容和执行结果均已确认。", "tool_calls": []}


class ScriptedModel:
    context_tokens = 8192

    def __init__(self, responses, *, before_call=None):
        self.responses = list(responses)
        self.calls = 0
        self.before_call = before_call
        self.messages = []

    async def chat_with_tools(self, messages, tools=None, temperature=1):
        self.calls += 1
        self.messages = list(messages)
        if self.before_call:
            self.before_call()
        if not self.responses:
            raise AssertionError("恢复过程不应重新请求模型规划或生成工具参数")
        return self.responses.pop(0)

    async def chat(self, **kwargs):
        raise AssertionError("工具恢复测试不应进入纯文本模型接口")


class RuntimeCheckpointResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SECRET_MASTER_KEY": "isolated-runtime-integration-secret-master-key"})
        self.env.start()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        with self.sessions() as db:
            db.add(User(id=7, username="resume-owner", password_hash="x"))
            db.add(Agent(id=9, name="resume-agent"))
            db.add(Agent(id=13, name="resume-child"))
            db.add(Job(id="resume-run", owner_id=7, agent_id=9, status="running",
                       worker_id="worker1", lease_token="lease1", payload="{}"))
            db.commit()
        self.store = ExecutionStore("resume-run", 7, "resume-run", session_factory=self.sessions)
        self.temp = tempfile.TemporaryDirectory()
        self.events = []
        self.patches = [
            patch.object(approvals, "SessionLocal", self.sessions),
            patch.object(orchestrator, "guard_model_client", side_effect=lambda llm, **kwargs: llm),
            patch.object(orchestrator, "enforce_content", new_callable=AsyncMock),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()
        self.engine.dispose()
        self.env.stop()

    def context(self, tokens=None):
        return builtin_tools.BuiltinToolContext(
            root=Path(self.temp.name), user_id=7, agent_id=9,
            run_id="resume-run", execution_id="resume-run", enabled_tools={"write"},
            approval_tokens=tokens or [],
            runtime_event=lambda name, payload: self.events.append((name, payload)),
        )

    async def run_harness(self, model, *, context=None, store=None, sub_agents=None,
                          guidance=None, runtime_event=None):
        return await orchestrator.run_harness(
            model, "依据实际执行结果回答。", "完成文件操作。",
            builtin_context=context or self.context(), checkpoint_store=store or self.store,
            sub_agents=sub_agents,
            guidance=guidance,
            model_execution={"planning": {"mode": "off"}},
            tool_policy={"profile": "standard", "max_iterations": 5,
                         "max_parallel_calls": 4, "max_successful_calls": 10,
                         "router_enabled": False},
            verification_policy={"required": False, "max_revisions": 0},
            runtime_event=runtime_event or (lambda name, payload: self.events.append((name, payload))),
        )

    async def test_approval_resumes_pending_batch_without_replaying_first_write(self):
        effects, attempts = [], []

        async def execute(name, args, context):
            path = args["path"]
            attempts.append(path)
            if path == "second.txt" and not approvals.consume(
                context.approval_tokens, run_id=context.run_id, user_id=context.user_id,
                agent_id=context.agent_id, scope="write",
            ):
                raise approvals.ApprovalRequired("write", "确认第二个文件操作", agent_id=9)
            effects.append(path)
            return {"ok": True, "path": path, "content": "执行完成"}

        first_model = ScriptedModel([tool_message(call("first-call", "first.txt"), call("second-call", "second.txt"))])
        with patch.object(builtin_tools, "execute", side_effect=execute):
            with self.assertRaises(approvals.ApprovalRequired) as paused:
                await self.run_harness(first_model)
            self.assertEqual(first_model.calls, 1)
            self.assertEqual(effects, ["first.txt"])
            checkpoint = self.store.load_checkpoint()
            self.assertEqual(checkpoint["state"]["phase"], "pending")
            self.assertEqual([item["id"] for item in checkpoint["state"]["pending_message"]["tool_calls"]],
                             ["first-call", "second-call"])
            token = approvals.issue("resume-run", 7, 9, "write", binding=paused.exception.binding)

            def assert_pending_dispatched_before_model():
                self.assertEqual(effects, ["first.txt", "second.txt"])

            resumed_model = ScriptedModel([FINAL_MESSAGE], before_call=assert_pending_dispatched_before_model)
            answer, _ = await self.run_harness(resumed_model, context=self.context([token]))
        self.assertIn("操作已经完成", answer)
        self.assertEqual(resumed_model.calls, 1)
        self.assertEqual(effects, ["first.txt", "second.txt"])
        self.assertEqual(attempts, ["first.txt", "second.txt", "second.txt"])
        with self.sessions() as db:
            self.assertEqual([row.state for row in db.query(ToolInvocation).all()], ["completed", "completed"])
        self.assertTrue(any(name == "invocation.reused" for name, _ in self.events))
        self.assertEqual(self.store.load_checkpoint()["state"]["phase"], "completed")

    async def test_interrupted_write_stops_recovery_before_any_model_or_second_effect(self):
        effects = []

        async def interrupted(name, args, context):
            effects.append(args["path"])
            raise asyncio.CancelledError("模拟外部写入完成后进程中断")

        with patch.object(builtin_tools, "execute", side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await self.run_harness(ScriptedModel([tool_message(call("write-call", "uncertain.txt"))]))
        resumed = ScriptedModel([])
        with patch.object(builtin_tools, "execute", new_callable=AsyncMock) as executor:
            with self.assertRaises(UnknownToolOutcome):
                await self.run_harness(resumed)
            executor.assert_not_awaited()
        self.assertEqual(effects, ["uncertain.txt"])
        self.assertEqual(resumed.calls, 0)
        self.assertTrue(any(name == "recovery.blocked" for name, _ in self.events))

    async def test_completed_checkpoint_returns_result_without_model_or_tools(self):
        with patch.object(builtin_tools, "execute", new_callable=AsyncMock, return_value={"ok": True}):
            expected = await self.run_harness(ScriptedModel([tool_message(call("complete-call", "complete.txt")), FINAL_MESSAGE]))
        fresh_model = ScriptedModel([])
        with patch.object(builtin_tools, "execute", new_callable=AsyncMock) as executor:
            recovered = await self.run_harness(fresh_model)
        self.assertEqual(recovered, expected)
        self.assertEqual(fresh_model.calls, 0)
        executor.assert_not_awaited()

    async def test_write_timeout_blocks_new_model_retry_in_the_same_run(self):
        effects = []

        async def timed_out(name, args, context):
            effects.append(args["path"])
            raise asyncio.TimeoutError("远端已收到请求但响应丢失")

        model = ScriptedModel([
            tool_message(call("uncertain-call", "uncertain.txt")),
            tool_message(call("retry-new-id", "uncertain.txt")),
            FINAL_MESSAGE,
        ])
        with patch.object(builtin_tools, "execute", side_effect=timed_out):
            with self.assertRaises(UnknownToolOutcome):
                await self.run_harness(model)
        self.assertEqual(model.calls, 1)
        self.assertEqual(effects, ["uncertain.txt"])
        self.assertFalse(any(name == "loop.completed" for name, _ in self.events))
        self.assertTrue(any(name == "recovery.blocked" for name, _ in self.events))

    async def test_guidance_is_checkpointed_before_ack_and_duplicate_recovery_does_not_apply_twice(self):
        row = {"id": "durable-guidance-1", "mode": "redirect", "content": "只检查新目标文件。"}
        acknowledgements = []
        pending = [row]

        def on_event(name, payload):
            self.events.append((name, payload))
            if name != "guidance.applied":
                return
            # Emulate worker's durable guidance acknowledgement callback. This
            # read must already observe both intent and transcript before ACK.
            checkpoint = self.store.load_checkpoint()
            acknowledgements.append(checkpoint)
            if len(acknowledgements) == 1:
                raise asyncio.CancelledError("模拟检查点已提交、引导确认尚未提交时中断")
            pending.clear()

        first_model = ScriptedModel([])
        with self.assertRaises(asyncio.CancelledError):
            await self.run_harness(first_model, guidance=lambda: list(pending), runtime_event=on_event)
        self.assertEqual(first_model.calls, 0)
        self.assertEqual(len(acknowledgements), 1)
        self.assertIsNotNone(acknowledgements[0])
        first_state = acknowledgements[0]["state"]
        self.assertEqual(first_state["task_contract"]["objective"], row["content"])
        self.assertEqual(first_state["task_contract"]["applied_guidance_ids"], [row["id"]])
        self.assertEqual(first_state["applied_interactions"], [{"id": row["id"], "mode": "redirect", "applied": True}])
        self.assertEqual(first_state["task_contract"]["revision"], 2)
        self.assertEqual(sum(row["content"] in str(message.get("content", ""))
                             for message in first_state["messages"]), 1)

        resumed_model = ScriptedModel([FINAL_MESSAGE])
        await self.run_harness(resumed_model, guidance=lambda: list(pending), runtime_event=on_event)
        self.assertEqual(len(acknowledgements), 2)
        self.assertEqual(acknowledgements[1]["revision"], acknowledgements[0]["revision"])
        self.assertEqual(acknowledgements[1]["state"]["task_contract"]["revision"], 2)
        completed = self.store.load_checkpoint()["state"]
        self.assertEqual(completed["task_contract"]["revision"], 2)
        self.assertEqual(len(completed["task_contract"]["guidance"]), 1)
        self.assertEqual(len(completed["applied_interactions"]), 1)
        self.assertEqual(sum(row["content"] in str(message.get("content", ""))
                             for message in completed["messages"]), 1)
        self.assertEqual(resumed_model.calls, 1)

    async def test_inline_child_approval_resumes_stable_child_checkpoint_and_binding(self):
        effects, child_execution_ids = [], []

        async def execute(name, args, context):
            self.assertEqual(context.agent_id, 13)
            child_execution_ids.append(context.execution_id)
            if args["path"] == "second.txt" and not approvals.consume(
                context.approval_tokens, run_id=context.run_id, user_id=context.user_id,
                agent_id=context.agent_id, scope="write",
            ):
                raise approvals.ApprovalRequired("write", "确认子智能体操作")
            effects.append(args["path"])
            return {"ok": True, "path": args["path"]}

        child_model = ScriptedModel([
            tool_message(call("first-child-call", "first.txt"), call("second-child-call", "second.txt")),
            FINAL_MESSAGE,
        ])
        child = {
            "id": 13, "name": "worker", "llm": child_model, "system_prompt": "执行文件操作。",
            "builtin_tools": ["write"], "model_execution": {"planning": {"mode": "off"}},
            "tool_policy": {"profile": "standard", "max_parallel_calls": 4, "router_enabled": False},
            "verification_policy": {"required": False, "max_revisions": 0},
        }
        parent_model = ScriptedModel([tool_message({
            "id": "delegate-child", "type": "function", "function": {
                "name": "call_agent__worker", "arguments": '{"query":"完成文件操作。"}',
            },
        }), FINAL_MESSAGE])
        with bind_execution_lease("resume-run", 7, "worker1", "lease1", session_factory=self.sessions):
            store = ExecutionStore("resume-run", 7, "resume-run", session_factory=self.sessions)
            with patch.object(builtin_tools, "execute", side_effect=execute):
                with self.assertRaises(approvals.ApprovalRequired) as paused:
                    await self.run_harness(parent_model, store=store, sub_agents=[child])
                self.assertEqual(paused.exception.agent_id, 13)
                self.assertEqual(effects, ["first.txt"])
                token = approvals.issue("resume-run", 7, 13, "write", binding=paused.exception.binding)
                answer, _ = await self.run_harness(parent_model, store=store, sub_agents=[child], context=self.context([token]))
        self.assertIn("操作已经完成", answer)
        self.assertEqual(effects, ["first.txt", "second.txt"])
        self.assertEqual(child_model.calls, 2)
        self.assertEqual(parent_model.calls, 2)
        self.assertEqual(len(set(child_execution_ids)), 1)
        self.assertNotEqual(child_execution_ids[0], "resume-run")
        with self.sessions() as db:
            self.assertEqual(db.query(RuntimeCheckpoint).count(), 2)
            self.assertEqual([row.state for row in db.query(ToolInvocation).all()], ["completed"] * 3)

    async def test_lease_takeover_during_tool_cannot_commit_completion_or_continue_model(self):
        async def lose_lease(name, args, context):
            with self.sessions() as db:
                job = db.get(Job, "resume-run")
                job.worker_id, job.lease_token = "worker2", "lease2"
                db.commit()
            return {"ok": True}

        model = ScriptedModel([tool_message(call("fenced-call", "fenced.txt"))])
        with bind_execution_lease("resume-run", 7, "worker1", "lease1", session_factory=self.sessions):
            fenced_store = ExecutionStore("resume-run", 7, "resume-run", session_factory=self.sessions)
            with patch.object(builtin_tools, "execute", side_effect=lose_lease):
                with self.assertRaises(InvocationPersistenceError):
                    await self.run_harness(model, store=fenced_store)
        self.assertEqual(model.calls, 1)
        with self.sessions() as db:
            self.assertEqual(db.query(ToolInvocation).one().state, "running")
        self.assertFalse(any(name == "loop.completed" for name, _ in self.events))


if __name__ == "__main__":
    unittest.main()
