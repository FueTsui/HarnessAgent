"""任务 worker（M1）：领取并执行 `jobs` 队列中的任务。

两种运行方式（共用同一套逻辑）：
- 进程内：应用启动时作为 asyncio 后台任务运行（JOB_WORKER_ENABLED=true，默认）；单机部署够用。
- 独立进程：`python run.py worker`，可与 API 进程分离、按需多开，为横向扩展铺路（配合 Postgres）。

执行模型：claim_next 原子领取 → 为每个任务起一个受监督的 asyncio 任务（并发受 JOB_WORKER_CONCURRENCY
限制）→ 周期心跳续租；心跳检测到 cancel_requested 即取消该任务 → 终态写回（done/failed/cancelled）。
"""
import asyncio
import json
import logging
import os
import re
import socket
from typing import Awaitable, Callable, Optional

import httpx
from sqlalchemy.exc import OperationalError

from . import jobs
from .approvals import ApprovalRequired
from .config import settings
from .runtime.evaluation import evaluation_summary
from .guest_access import is_guest, reject_guest_resources, validate_guest_execution

logger = logging.getLogger(__name__)


class JobCancelled(Exception):
    """协作式取消信号：任务在检查点（progress 回调）感知 cancel_requested 时抛出。"""


# kind -> handler(view) -> result dict
HANDLERS: "dict[str, Callable[[jobs.JobView], Awaitable[dict]]]" = {}
_BACKGROUND_TITLE_TASKS: "set[asyncio.Task]" = set()


def conversation_title_fallback(query: str, limit: int = 24) -> str:
    """无需模型的稳定回退标题，保证首轮提交时侧栏立即有可读名称。"""
    value = re.sub(r"\s+", " ", str(query or "")).strip()
    value = value.strip('"\'“”《》「」 .。!！?？').strip()
    return (value[:limit] or "新任务")


async def _persist_conversation_title(
    thread_id: str,
    agent_id: int,
    query: str,
    answer: str,
    fallback: str,
) -> None:
    """使用独立 Session 限时生成标题；不得持有主任务事务或改变 Job 结果。"""
    from .api.chat import make_conversation_title
    from .database import SessionLocal
    from .models import Agent, Thread

    db = SessionLocal()
    try:
        thread = db.get(Thread, thread_id)
        agent = db.get(Agent, agent_id)
        if thread is None or agent is None:
            return
        # 后台执行期间用户可能已经手动改名；只允许替换空标题或本次回退标题。
        if (thread.title or "").strip() not in {"", fallback}:
            return
        title = await asyncio.wait_for(
            make_conversation_title(db, agent, query, answer),
            timeout=max(0.1, settings.TITLE_GENERATION_TIMEOUT_SECONDS),
        )
        if not title:
            return
        db.refresh(thread)
        if (thread.title or "").strip() not in {"", fallback}:
            return
        thread.title = title
        db.commit()
    except asyncio.TimeoutError:
        logger.info(
            "会话标题后台生成超时（thread=%s，timeout=%ss），保留回退标题",
            thread_id,
            settings.TITLE_GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - 标题为派生数据，不影响主任务
        db.rollback()
        logger.warning("会话标题后台写入失败（thread=%s）：%s", thread_id, exc)
    finally:
        db.close()


def _schedule_conversation_title(
    thread_id: str,
    agent_id: int,
    query: str,
    answer: str,
    fallback: str,
) -> asyncio.Task:
    """调度非阻塞标题任务并消费异常，避免后台 Task 泄漏告警。"""
    task = asyncio.create_task(
        _persist_conversation_title(thread_id, agent_id, query, answer, fallback),
        name=f"conversation-title:{thread_id}",
    )
    _BACKGROUND_TITLE_TASKS.add(task)

    def _done(completed: asyncio.Task) -> None:
        _BACKGROUND_TITLE_TASKS.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception as exc:  # pragma: no cover - 内层已兜底，保留最后防线
            logger.warning("会话标题后台任务异常：%s", exc)

    task.add_done_callback(_done)
    return task


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _invoke(view: jobs.JobView) -> dict:
    from .guardrail_reviews import bind_review_context
    fn = HANDLERS.get(view.kind)
    if fn is None:
        raise RuntimeError(f"未知任务类型：{view.kind}")
    if view.owner_id is None:
        return await fn(view)
    from .token_usage import bind_usage_context
    from .runtime.durability import bind_execution_lease
    with bind_review_context(view.id), bind_usage_context(
        view.owner_id, run_id=view.id, agent_id=view.agent_id
    ), bind_execution_lease(view.id, view.owner_id, view.worker_id, view.lease_token,
                           session_factory=jobs.SessionLocal):
        return await fn(view)


async def _supervise(view: jobs.JobView, wid: str) -> None:
    """监督单个任务：并发执行 + 周期心跳 + 协作取消 + 终态写回。"""
    task = asyncio.create_task(_invoke(view))
    jobs.register_local(view.id, task)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=settings.JOB_HEARTBEAT_SECONDS)
            if task in done:
                break
            # 心跳续租；返回 False 表示被请求取消（或任务已不归本 worker）→ 取消执行
            if not await asyncio.to_thread(
                jobs.heartbeat, view.id, wid, view.lease_token
            ):
                task.cancel()
        try:
            result = task.result()
        except (asyncio.CancelledError, JobCancelled):
            jobs.mark_cancelled(view.id, wid, view.lease_token)
            return
        except ApprovalRequired as exc:
            jobs.wait_for_approval(
                view.id,
                wid,
                view.lease_token,
                exc.scope,
                exc.description,
                approval_agent_id=exc.agent_id,
                execution_context=exc.execution_context,
                binding=exc.binding,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 失败状态对外暴露
            logger.warning("任务 %s 执行失败：%s", view.id, exc)
            if _is_transient_failure(exc):
                jobs.retry_or_dead_letter(
                    view.id,
                    wid,
                    view.lease_token,
                    str(exc),
                    error_class=type(exc).__name__,
                )
            else:
                jobs.fail(
                    view.id,
                    wid,
                    view.lease_token,
                    str(exc),
                    error_class=type(exc).__name__,
                )
            return
        jobs.finish(view.id, wid, view.lease_token, result)
    except asyncio.CancelledError:
        # Worker 排空超时：停止实际执行但保留当前租约，避免尚未完全中止的工具调用
        # 与新 Worker 重叠；租约超时后由 requeue_stale 统一恢复。
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        jobs.unregister_local(view.id)
        jobs.clear_partial(view.id)


def _is_transient_failure(exc: Exception) -> bool:
    """仅重试明确的连接/超时/数据库瞬时错误，避免重复执行永久或有副作用故障。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    if isinstance(exc, OperationalError):
        text = str(exc).lower()
        return any(token in text for token in (
            "database is locked", "deadlock", "connection reset", "connection refused",
            "server closed the connection", "could not connect",
        ))
    return False


async def _run_maintenance(label: str, fn, *args) -> bool:
    """隔离周期维护故障；单项清理失败不得终止整个任务 Worker。"""
    try:
        await asyncio.to_thread(fn, *args)
        return True
    except Exception as exc:  # noqa: BLE001 - 维护任务必须相互隔离
        logger.warning("Worker 周期维护失败（%s）：%s", label, exc)
        return False


async def run_worker(stop_event: asyncio.Event, concurrency: Optional[int] = None) -> None:
    """worker 主循环：直到 stop_event 置位。"""
    wid = worker_id()
    cc = concurrency or settings.JOB_WORKER_CONCURRENCY
    poll = settings.JOB_POLL_SECONDS
    running: "set[asyncio.Task]" = set()
    logger.info("任务 worker 启动：%s（并发=%s）", wid, cc)

    # 启动即回收上次异常退出遗留的陈旧任务
    await asyncio.to_thread(jobs.requeue_stale, settings.JOB_LEASE_SECONDS)
    await asyncio.to_thread(jobs.touch_worker, wid, cc)
    next_worker_heartbeat = (
        asyncio.get_running_loop().time()
        + max(5.0, float(settings.JOB_HEARTBEAT_SECONDS))
    )
    ticks = 0

    while not stop_event.is_set():
        if asyncio.get_running_loop().time() >= next_worker_heartbeat:
            await asyncio.to_thread(jobs.touch_worker, wid, cc)
            next_worker_heartbeat = (
                asyncio.get_running_loop().time()
                + max(5.0, float(settings.JOB_HEARTBEAT_SECONDS))
            )
        # 周期维护：陈旧重入队 + 过期清理（每约 JOB_LEASE_SECONDS 一次）
        ticks += 1
        if ticks % max(1, int(settings.JOB_LEASE_SECONDS / max(poll, 0.1))) == 0:
            await _run_maintenance("requeue_stale", jobs.requeue_stale, settings.JOB_LEASE_SECONDS)
            await _run_maintenance("cleanup_old", jobs.cleanup_old, settings.JOB_TTL_SECONDS)
            await _run_maintenance(
                "cleanup_worker_heartbeats",
                jobs.cleanup_worker_heartbeats,
                max(300, settings.JOB_LEASE_SECONDS * 10),
            )
            await _run_maintenance("cleanup_uploads", jobs.cleanup_uploads, settings.UPLOAD_TTL_SECONDS)
            from .artifacts import cleanup_expired
            await _run_maintenance("cleanup_artifacts", cleanup_expired)
            from .rate_limit import cleanup_expired as cleanup_rate_limit_buckets
            await _run_maintenance(
                "cleanup_rate_limit_buckets", cleanup_rate_limit_buckets,
                settings.RATE_LIMIT_BUCKET_TTL_SECONDS,
            )

        if len(running) >= cc:
            await asyncio.sleep(poll)
            continue
        view = await asyncio.to_thread(jobs.claim_next, wid)
        if view is None:
            await asyncio.sleep(poll)
            continue
        t = asyncio.create_task(_supervise(view, wid))
        running.add(t)
        t.add_done_callback(running.discard)

    # 退出：有界排空；超时后取消本地执行，租约到期再由其它 Worker 接管。
    if running:
        logger.info("worker 停止中，等待 %s 个在跑任务收尾", len(running))
        done, pending = await asyncio.wait(
            running,
            timeout=max(0.1, float(settings.JOB_SHUTDOWN_GRACE_SECONDS)),
        )
        if pending:
            logger.warning("Worker 排空超时，取消 %s 个本地执行并等待租约恢复", len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
    if _BACKGROUND_TITLE_TASKS:
        for task in list(_BACKGROUND_TITLE_TASKS):
            task.cancel()
        await asyncio.gather(*list(_BACKGROUND_TITLE_TASKS), return_exceptions=True)
    await asyncio.to_thread(jobs.remove_worker, wid)
    logger.info("任务 worker 已停止：%s", wid)


# ---------- chat 任务处理器 ----------

@handler("chat")
async def _chat_handler(view: jobs.JobView) -> dict:
    """执行一次 Turn：重建上下文，运行 Harness，并把结果追加到 Item。"""
    from .api.chat import _validate_invocations, deserialize_input, execute_chat
    from .database import SessionLocal
    from .models import Agent, Thread, Turn, User
    from .runtime import task_store
    from .security import can_access_agent

    payload = view.payload
    inputs = deserialize_input(payload)
    user_id = payload.get("user_id")
    agent_id = payload.get("agent_id")
    template_ids = payload.get("template_ids") or []
    dataset_ids = payload.get("dataset_ids") or []
    skill_ids = payload.get("skill_ids") or []
    mcp_ids = payload.get("mcp_ids") or []
    invoked_agent_ids = payload.get("invoked_agent_ids") or []
    provider_id = payload.get("provider_id")
    attachment_images = payload.get("attachment_images") or []
    attachment_docs = payload.get("attachment_docs") or []
    attachment_context = payload.get("attachment_context") or []
    session_id = (payload.get("session_id") or "").strip()
    project_id = payload.get("project_id")
    project_context = payload.get("project_context") or {}

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        user = db.get(User, user_id)
        delegated_access = _delegated_agent_access(db, view, payload, agent_id)
        if (
            user is None
            or not user.is_active
            or is_guest(user)
            or agent is None
            or not agent.enabled
            or not (can_access_agent(user, agent) or delegated_access)
        ):
            raise RuntimeError("智能体不存在或已停用")
        execution_snapshot = payload.get("execution_snapshot")
        if is_guest(user):
            reject_guest_resources(user, skill_ids=skill_ids, mcp_ids=mcp_ids,
                                   agent_ids=invoked_agent_ids, template_ids=template_ids, dataset_ids=dataset_ids)
            validate_guest_execution(db, user, agent, execution_snapshot)
        # 新任务在入队时已完成能力校验并固化完整快照。仅为历史无快照任务保留旧校验。
        if not isinstance(execution_snapshot, dict):
            _validate_invocations(
                db, user, agent, skill_ids, mcp_ids, invoked_agent_ids
            )

        # 多轮对话：取本线程此前各轮问答作为历史；超长由 Harness 运行时按上下文预算压缩。
        history = (
            await asyncio.to_thread(
                task_store.thread_messages,
                db,
                owner_id=user_id,
                thread_id=session_id,
            )
            if session_id else None
        )
        # 当前用户消息已经作为本 Turn 的首个 Item 写入，构建模型历史时移除它。
        if history and history[-1].get("role") == "user" and history[-1].get("content") == inputs.query:
            history = history[:-1]
        turn = db.get(Turn, view.id)
        thread = db.get(Thread, session_id) if session_id else None
        is_first_turn = bool(turn and turn.sequence == 1)
        if thread is not None:
            project_id = thread.project_id

        async def _progress(stage: str):
            # 先持久化并取得统一事件信封，再实时推送；这样实时流与断线回放共享
            # task_id / event_id / timestamp / revision，不会形成两套事件身份。
            cancelled, event_meta = await asyncio.to_thread(
                jobs.set_progress,
                view.id,
                view.worker_id,
                view.lease_token,
                stage,
                include_event=True,
            )
            jobs.publish_progress(view.id, stage, event_meta)
            if cancelled:
                raise JobCancelled()

        def _on_delta(delta: str):
            # 最终答复的流式增量累积到进程内缓冲，供轮询接口边生成边返回（不写 DB）。
            jobs.append_partial(view.id, delta)

        async def _runtime_event(event_type: str, event_payload: dict):
            event_meta = await asyncio.to_thread(
                jobs.append_event,
                view.id,
                view.worker_id,
                view.lease_token,
                event_type,
                event_payload,
            )
            jobs.publish_runtime_event(
                view.id, event_type, event_payload, event_meta
            )

        async def _guidance() -> list[dict]:
            rows = await asyncio.to_thread(
                jobs.take_guidance,
                view.id,
                view.worker_id,
                view.lease_token,
            )
            for row in rows:
                jobs.publish_runtime_event(view.id, "guidance.claimed", {
                    "guidance_id": row["id"],
                    "chars": len(row["content"]),
                })
            return rows

        # 简单/流程智能体都上报进度，前端据此渲染「执行过程」时间线；最终答复流式回传
        answer, export_files, _reasoning, completion_metadata = await execute_chat(
            db, agent, inputs, progress=_progress, template_ids=template_ids,
            user_id=user_id, stream=_on_delta, history=history, dataset_ids=dataset_ids,
            attachment_images=attachment_images, attachment_docs=attachment_docs,
            skill_ids=skill_ids, mcp_ids=mcp_ids, invoked_agent_ids=invoked_agent_ids,
            provider_id=provider_id, runtime_event=_runtime_event,
            guidance=_guidance,
            subagent_depth=int(payload.get("subagent_depth") or 0),
            session_id=session_id, run_id=view.id,
            approval_tokens=payload.get("approval_tokens") or [],
            execution_snapshot=execution_snapshot,
            attachment_context=attachment_context,
            approval_policy=payload.get("approval_policy") or "ask",
            project_context=project_context,
            interaction_context=payload.get("duplex_control") or {},
        )
        if not (answer or "").strip():
            # Chat 任务的持久化硬门禁：任何上游/运行时回归都不能写入空会话，
            # 更不能让 supervisor 把空结果标记为 done。
            raise RuntimeError("模型未返回可展示的最终答复")
        # 标题是派生数据，不得阻塞答案、附件和 Job 终态。主事务先写入确定性
        # 回退标题；模型标题在 commit 后使用独立 Session 限时后台更新。
        fallback_title = ""
        if is_first_turn and thread is not None:
            fallback_title = conversation_title_fallback(inputs.query)
            if not (thread.title or "").strip():
                thread.title = fallback_title
        db.flush()
        from .artifacts import register_many
        register_many(
            db,
            owner_id=user_id,
            run_id=view.id,
            turn_id=view.id,
            filenames=export_files,
        )
        db.commit()
        if is_first_turn and thread is not None and not is_guest(user):
            _schedule_conversation_title(
                str(thread.id), int(agent.id), inputs.query, answer, fallback_title,
            )
        completion_status = str(
            completion_metadata.get("completion_status") or "completed"
        )
        if completion_status not in {"completed", "completed_with_issues"}:
            completion_status = "completed"
        return {
            "answer": answer, "export_files": export_files,
            "turn_id": view.id, "thread_id": session_id,
            "session_id": session_id, "run_id": view.id,
            "harness_version": (
                ((execution_snapshot or {}).get("harness") or {}).get("version")
                or payload.get("harness_version")
            ),
            "completion_status": completion_status,
            "completion_issues": [
                str(value)[:400]
                for value in (completion_metadata.get("completion_issues") or [])[:20]
            ],
            "plan_summary": dict(completion_metadata.get("plan_summary") or {}),
            "evaluation_summary": evaluation_summary(
                completion_metadata.get("evaluation")
            ),
        }
    finally:
        db.close()


def _delegated_agent_access(db, view: jobs.JobView, payload: dict, target_agent_id) -> bool:
    """验证异步子任务来自父 Job 入队快照中的显式绑定，而非伪造直接访问。"""
    if (
        payload.get("source") != "subagent"
        or not payload.get("delegation_authorized")
        or not view.parent_job_id
        or not payload.get("parent_agent_id")
    ):
        return False
    from .models import Job

    parent = db.get(Job, view.parent_job_id)
    if parent is None or parent.owner_id != view.owner_id:
        return False
    try:
        parent_payload = json.loads(parent.payload or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    snapshot = parent_payload.get("execution_snapshot")
    if not isinstance(snapshot, dict):
        return False
    parent_agent_id = int(payload.get("parent_agent_id") or 0)
    target_id = int(target_agent_id or 0)

    def locate(node: dict) -> dict | None:
        agent_data = node.get("agent") if isinstance(node, dict) else None
        if isinstance(agent_data, dict) and int(agent_data.get("id") or 0) == parent_agent_id:
            return node
        for child in node.get("sub_agents") or [] if isinstance(node, dict) else []:
            if isinstance(child, dict):
                found = locate(child)
                if found is not None:
                    return found
        return None

    parent_node = locate(snapshot)
    if parent_node is None:
        return False
    allowed = {parent_agent_id}
    for child in parent_node.get("sub_agents") or []:
        if isinstance(child, dict) and isinstance(child.get("agent"), dict):
            allowed.add(int(child["agent"].get("id") or 0))
    return target_id in allowed


def run_standalone() -> None:
    """独立 worker 进程入口：python run.py worker。"""
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from .logging_utils import configure_secure_logging
    configure_secure_logging()
    # 使用与 API 进程相同的版本化迁移；迁移锁防止多进程并发升级。
    from .migrations import run_schema_migrations
    run_schema_migrations()

    stop = asyncio.Event()

    async def _main():
        task = asyncio.create_task(run_worker(stop))
        try:
            await task
        except KeyboardInterrupt:
            stop.set()
            await task

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("收到中断，worker 退出")
