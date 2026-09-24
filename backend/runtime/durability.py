"""Private checkpoints and a write-ahead tool ledger, fenced by the Job lease.

The ledger deliberately does not promise exactly-once external effects. A tool
that crashed after starting has an unknown outcome; only declared reads can be
retried automatically. Completed observations are replayed without dispatch.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from ..database import SessionLocal
from ..models import Job, RuntimeCheckpoint, ToolInvocation


class RuntimeLeaseLost(RuntimeError):
    pass


class CheckpointConflict(RuntimeError):
    pass


class InvocationConflict(RuntimeError):
    pass


class UnknownToolOutcome(RuntimeError):
    def __init__(self, invocation):
        self.tool = invocation.tool_name
        self.call_id = invocation.call_id
        self.invocation_id = invocation.id
        super().__init__(f"工具 {self.tool} 的执行结果不确定；已停止自动重试，请核对实际结果后创建新任务。")


_LEASE: ContextVar[tuple | None] = ContextVar("runtime_execution_lease", default=None)


@contextmanager
def bind_execution_lease(run_id, user_id, worker_id, lease_token, *, session_factory=None):
    token = _LEASE.set((run_id, user_id, worker_id, lease_token, session_factory))
    try:
        yield
    finally:
        _LEASE.reset(token)


def canonical_arguments(arguments: dict) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def arguments_digest(arguments: dict) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


def _identity(*parts) -> str:
    return hashlib.sha256(canonical_arguments(list(parts)).encode("utf-8")).hexdigest()[:32]


@dataclass
class Invocation:
    id: str
    call_id: str
    tool_name: str
    arguments_digest: str
    capability_revision: str
    effect: str
    state: str
    result: dict

    @property
    def binding(self) -> dict:
        return {"invocation_id": self.id, "arguments_digest": self.arguments_digest,
                "capability_revision": self.capability_revision}


def _view(row: ToolInvocation) -> Invocation:
    return Invocation(row.id, row.call_id, row.tool_name, row.arguments_digest,
                      row.capability_revision, row.effect, row.state,
                      json.loads(row.result or "{}"))


def execution_store(run_id: str, user_id: int | None, execution_key: str = "root") -> ExecutionStore | None:
    """Enable persistence for worker-owned runs only, without touching direct-call fixtures."""
    lease = _LEASE.get()
    if not lease or not user_id or lease[0] != run_id or lease[1] != user_id:
        return None
    return ExecutionStore(run_id, user_id, execution_key, session_factory=lease[4])


class ExecutionStore:
    def __init__(self, run_id: str, user_id: int, execution_key: str = "root", *, session_factory=None):
        if not run_id or not user_id or not execution_key or len(execution_key) > 160:
            raise ValueError("执行持久化需要有效的任务、所有者和执行标识")
        self.run_id, self.user_id, self.execution_key = run_id, user_id, execution_key
        self.sessions = session_factory or SessionLocal
        self.lease = _LEASE.get()
        if self.lease and (self.lease[0] != run_id or self.lease[1] != user_id):
            raise RuntimeLeaseLost("运行标识与执行租约不匹配")
        self.checkpoint_id = _identity(run_id, execution_key)

    def _owned(self, db, *, writing=False):
        query = db.query(Job).filter(Job.id == self.run_id, Job.owner_id == self.user_id)
        if self.lease:
            query = query.filter(Job.status == "running", Job.worker_id == self.lease[2],
                                 Job.lease_token == self.lease[3], Job.cancel_requested.is_(False))
        if writing:
            # Conditional no-op UPDATE acquires the database row/write lock. A
            # worker cannot commit ledger state after another owner takes over.
            if query.update({Job.updated_at: Job.updated_at}, synchronize_session=False) != 1:
                raise RuntimeLeaseLost("执行租约已失效或任务不属于当前用户")
        elif query.first() is None:
            raise RuntimeLeaseLost("执行租约已失效或任务不属于当前用户")

    def load_checkpoint(self) -> dict | None:
        with self.sessions() as db:
            self._owned(db)
            row = db.get(RuntimeCheckpoint, self.checkpoint_id)
            if row is None:
                return None
            if row.owner_id != self.user_id:
                raise RuntimeLeaseLost("检查点所有者不匹配")
            return {"revision": row.revision, "state": json.loads(row.state)}

    def save_checkpoint(self, state: dict, expected_revision: int = 0) -> int:
        encoded = canonical_arguments(state)
        with self.sessions() as db:
            self._owned(db, writing=True)
            if expected_revision == 0:
                if db.get(RuntimeCheckpoint, self.checkpoint_id) is not None:
                    raise CheckpointConflict("检查点已存在，请重新载入")
                db.add(RuntimeCheckpoint(id=self.checkpoint_id, run_id=self.run_id,
                                         owner_id=self.user_id, execution_key=self.execution_key,
                                         revision=1, state=encoded))
            else:
                updated = db.query(RuntimeCheckpoint).filter(
                    RuntimeCheckpoint.id == self.checkpoint_id,
                    RuntimeCheckpoint.owner_id == self.user_id,
                    RuntimeCheckpoint.revision == expected_revision,
                ).update({RuntimeCheckpoint.revision: expected_revision + 1,
                          RuntimeCheckpoint.state: encoded,
                          RuntimeCheckpoint.updated_at: datetime.datetime.now(datetime.timezone.utc)},
                         synchronize_session=False)
                if updated != 1:
                    raise CheckpointConflict("检查点版本已改变，请重新载入")
            try:
                db.commit()
            except IntegrityError as exc:
                raise CheckpointConflict("检查点已被另一个执行器写入") from exc
        return expected_revision + 1

    def prepare(self, call_id: str, tool_name: str, arguments: dict, capability_revision: str,
                effect: str = "unknown") -> Invocation:
        if not call_id or len(call_id) > 160 or not tool_name or len(tool_name) > 160 or not capability_revision:
            raise ValueError("调用标识、工具名称和能力版本不能为空或超长")
        identifier = _identity(self.run_id, self.execution_key, call_id)
        digest = arguments_digest(arguments)
        with self.sessions() as db:
            self._owned(db, writing=True)
            row = db.get(ToolInvocation, identifier)
            if row is None:
                row = ToolInvocation(id=identifier, run_id=self.run_id, owner_id=self.user_id,
                                     execution_key=self.execution_key, call_id=call_id,
                                     tool_name=tool_name, arguments_digest=digest,
                                     capability_revision=capability_revision, effect=effect,
                                     state="prepared", arguments=canonical_arguments(arguments), result="{}")
                db.add(row)
                db.flush()
            elif (row.owner_id != self.user_id or row.tool_name != tool_name
                  or row.arguments_digest != digest or row.capability_revision != capability_revision
                  or row.effect != effect):
                raise InvocationConflict("同一调用标识的工具、参数或能力版本已改变，拒绝重放")
            result = _view(row)
            db.commit()
            return result

    def _row(self, db, invocation):
        row = db.get(ToolInvocation, invocation.id)
        if (row is None or row.run_id != self.run_id or row.owner_id != self.user_id
                or row.execution_key != self.execution_key
                or row.arguments_digest != invocation.arguments_digest
                or row.capability_revision != invocation.capability_revision):
            raise InvocationConflict("调用记录与当前执行上下文不匹配")
        return row

    def begin(self, invocation: Invocation) -> bool:
        """True dispatches; False replays invocation.result. Unknown writes raise."""
        unknown = False
        with self.sessions() as db:
            self._owned(db, writing=True)
            row = self._row(db, invocation)
            if row.state == "completed":
                invocation.state, invocation.result = row.state, json.loads(row.result)
                return False
            if row.state in {"running", "unknown_outcome"} and row.effect not in {"read", "read_only"}:
                row.state = "unknown_outcome"
                unknown = True
            else:
                row.state = "running"
                row.lease_token = self.lease[3] if self.lease else ""
            invocation.state = row.state
            db.commit()
        if unknown:
            raise UnknownToolOutcome(invocation)
        return True

    def awaiting_approval(self, invocation: Invocation) -> None:
        self._transition(invocation, "awaiting_approval")

    def await_approval(self, invocation: Invocation) -> None:
        self.awaiting_approval(invocation)

    def complete(self, invocation: Invocation, result: dict) -> None:
        self._transition(invocation, "completed", result)

    def mark_unknown(self, invocation: Invocation) -> None:
        self._transition(invocation, "unknown_outcome")

    def _transition(self, invocation: Invocation, state: str, result: dict | None = None) -> None:
        with self.sessions() as db:
            self._owned(db, writing=True)
            row = self._row(db, invocation)
            if row.state == "completed":
                if state == "completed" and canonical_arguments(result) == canonical_arguments(json.loads(row.result)):
                    return
                raise InvocationConflict("已完成调用不可覆盖")
            if row.state != "running":
                raise InvocationConflict("只有已经开始的调用可以提交执行结果")
            row.state = state
            if result is not None:
                row.result = canonical_arguments(result)
                invocation.result = result
            invocation.state = state
            db.commit()
