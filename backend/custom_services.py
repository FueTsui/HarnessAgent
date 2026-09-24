"""Execute fixed service definitions with bounded input/output and owner checks."""
import asyncio
import json
import os
import signal
import subprocess
import time
import httpx
from fastapi import HTTPException
from .database import SessionLocal
from .models import Agent, User
from .service_models import CustomService, ServiceRun
from .security import can_access_agent, can_use, has_module_access
from .net_guard import validate_outbound_url
from . import approvals, guardrails
from .guardrail_policies import ContentBlocked, enforce_content

MAX_IO = 1_000_000


def validate_input(fields, values):
    try:
        encoded = json.dumps(values, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("输入必须为有效 JSON，不能包含 NaN 或 Infinity") from exc
    if not isinstance(values, dict) or len(encoded) > 100_000:
        raise ValueError("输入须为不超过 100,000 字符的 JSON 对象")
    known = {field["name"] for field in fields}
    if fields and set(values) - known:
        raise ValueError("输入包含未声明字段")
    types = {"string":str,"number":(int,float),"boolean":bool,"object":dict,"array":list}
    for field in fields:
        key = field["name"]
        if field.get("required") and key not in values:
            raise ValueError(f"缺少必填字段：{key}")
        if key in values and (not isinstance(values[key],types[field["type"]]) or field["type"] == "number" and isinstance(values[key],bool)):
            raise ValueError(f"字段类型不正确：{key}")


async def _read_limited(stream):
    chunks, size = [], 0
    while chunk := await asyncio.to_thread(stream.read1, 16384):
        size += len(chunk)
        if size > MAX_IO:
            raise ValueError("服务输出超过 1 MB")
        chunks.append(chunk)
    return b"".join(chunks)


async def _program(config, values):
    """Fixed JSON-in/JSON-out program; owns the entire process tree.

    Blocking pipe IO runs in worker threads, including on Windows Selector loops
    where asyncio subprocess transports are unavailable.
    """
    from .llm.mcp_client import attach_windows_process_tree
    env = {k:v for k,v in os.environ.items() if k.upper() in {"PATH","SYSTEMROOT","WINDIR","TEMP","TMP","PATHEXT","COMSPEC"}}
    env.update(config.get("env",{}))
    kwargs = {"creationflags":subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session":True}
    proc = subprocess.Popen([config["command"], *config.get("args",[])],
        cwd=config.get("cwd") or None, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs)
    process_tree = None
    readers = []
    def write_input():
        try:
            proc.stdin.write(json.dumps(values,ensure_ascii=False,allow_nan=False).encode()+b"\n")
            proc.stdin.flush()
        finally:
            proc.stdin.close()
    async def exchange():
        readers.extend([asyncio.create_task(_read_limited(proc.stdout)), asyncio.create_task(_read_limited(proc.stderr)), asyncio.create_task(asyncio.to_thread(write_input))])
        out, _err, _ = await asyncio.gather(*readers)
        await asyncio.to_thread(proc.wait)
        if proc.returncode:
            raise ValueError(f"编程服务退出码 {proc.returncode}；请检查已注册程序")
        try:
            result = json.loads(out.decode("utf-8"))
            json.dumps(result, allow_nan=False)
            return result
        except (UnicodeError,json.JSONDecodeError) as exc:
            raise ValueError("编程服务必须向 stdout 输出一个有效 JSON 值") from exc
    try:
        process_tree = attach_windows_process_tree(proc)
        return await asyncio.wait_for(exchange(),config.get("timeout_seconds",30))
    finally:
        # Always close the tree, even if the parent has already exited normally.
        if process_tree is not None:
            process_tree.close()
        elif os.name != "nt":
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        if proc.poll() is None:
            proc.kill()
        await asyncio.to_thread(proc.wait)
        for task in readers:
            if not task.done(): task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            await asyncio.to_thread(stream.close)


async def _http(config, values):
    await asyncio.to_thread(validate_outbound_url,config["url"])
    async with httpx.AsyncClient(timeout=config.get("timeout_seconds",30),follow_redirects=False) as client:
        method=config.get("method","POST")
        async with client.stream(method,config["url"],headers=config.get("headers",{}),
                                 **({"params":values} if method=="GET" else {"json":values})) as response:
            if response.status_code >= 300:
                raise ValueError(f"网络服务返回 HTTP {response.status_code}")
            chunks,size=[],0
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                if size>MAX_IO: raise ValueError("服务输出超过 1 MB")
                chunks.append(chunk)
            raw=b"".join(chunks).decode("utf-8",errors="replace")
            try: return json.loads(raw)
            except json.JSONDecodeError: return {"text":raw}


def service_definitions(user_id,agent_id):
    with SessionLocal() as db:
        user,agent=db.get(User,user_id),db.get(Agent,agent_id)
        if user is None or not user.is_active or not can_access_agent(user,agent): return []
        return [{"id":row.id,"name":row.name,"description":row.description,"input_fields":json.loads(row.input_fields)}
                for row in db.query(CustomService).filter(CustomService.enabled.is_(True)).all()
                if can_use(user,row) and agent_id in json.loads(row.agent_ids)]


def _redact_result(value, config):
    secrets = sorted({secret for key in ("headers", "env") for secret in config.get(key, {}).values() if secret}, key=len, reverse=True)
    def redact(item):
        if isinstance(item, str):
            for secret in secrets: item = item.replace(secret, "********")
            return item
        if isinstance(item, dict): return {redact(str(key)): redact(part) for key, part in item.items()}
        if isinstance(item, list): return [redact(part) for part in item]
        if isinstance(item, (int, float)) and str(item) in secrets: return "********"
        return item
    return redact(value)


async def execute_service(service_id,values,user_id,agent_id=None,*,playground=False,
                          run_id=None,approval_policy="ask",approval_tokens=None,runtime_event=None,provider_id=None):
    with SessionLocal() as db:
        user,row=db.get(User,user_id),db.get(CustomService,service_id)
        if user is None or not user.is_active or row is None or not row.enabled or not can_use(user,row):
            raise HTTPException(404,"服务不存在或不可用")
        if playground:
            if not has_module_access(user,"services"): raise HTTPException(403,"无服务模块权限")
        elif not can_access_agent(user,db.get(Agent,agent_id)) or agent_id not in json.loads(row.agent_ids):
            raise HTTPException(403,"此服务未分配给当前智能体")
        config=json.loads(row.config)
        fields=json.loads(row.input_fields)
        kind,name=row.kind,row.name
    validate_input(fields,values)
    start=time.monotonic()
    status,result="completed",None
    try:
        identity = dict(user_id=user_id, agent_id=agent_id, provider_id=provider_id, runtime_event=runtime_event)
        await enforce_content("tool_input", values, **identity)
        mutating = kind == "program" or config.get("method", "POST") != "GET"
        decisions = [await asyncio.to_thread(guardrails.runtime_decision,
            kind="mcp", tool_name=tool_name, arguments=values,
            policy="full_access" if playground else approval_policy,
            mutating=mutating, destructive=kind == "program")
            for tool_name in ("service_call", f"service:{service_id}")]
        from .guardrail_reviews import review_tool
        decisions = [await review_tool(d, user_id, agent_id, runtime_event) for d in decisions]
        for decision in decisions:
            if not decision["allowed"]:
                raise HTTPException(403, decision["reason"])
        if any(decision["requires_approval"] for decision in decisions):
            if playground or not run_id:
                raise HTTPException(403, "护栏要求单次审批，请在具备审批上下文的智能体任务中调用此服务")
            scope = f"service:{service_id}"
            approved = await asyncio.to_thread(approvals.consume, approval_tokens or [], run_id=str(run_id), user_id=user_id, agent_id=agent_id, scope=scope)
            if not approved:
                raise approvals.ApprovalRequired(scope, f"运行服务：{name}", agent_id=agent_id,
                    execution_context={"agent_id": agent_id, "execution_id": run_id, "approval_policy": approval_policy})
        result=await asyncio.wait_for(_program(config,values) if kind=="program" else _http(config,values), config.get("timeout_seconds",30))
        result = _redact_result(result, config)
        await enforce_content("tool_output", result, **identity)
        return result
    except asyncio.CancelledError:
        status,result="cancelled","服务执行已取消"
        raise
    except approvals.ApprovalRequired:
        status,result="pending_approval","服务等待单次批准"
        raise
    except (HTTPException, ContentBlocked) as exc:
        status,result="blocked",str(exc)
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        status,result="failed","服务执行超时"
        raise ValueError(result) from exc
    except Exception as exc:
        status,result="failed",_redact_result(str(exc), config) if isinstance(exc,ValueError) else "服务执行失败，请检查连接配置"
        raise ValueError(result) from exc
    finally:
        with SessionLocal() as db:
            db.add(ServiceRun(service_id=service_id,service_name=name,owner_id=user_id,status=status,
                duration_ms=int((time.monotonic()-start)*1000),result=json.dumps(result,ensure_ascii=False)[:20000]))
            db.commit()
