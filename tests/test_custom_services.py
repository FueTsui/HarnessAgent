"""Custom service execution, authorization and process lifecycle boundaries."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import approvals, custom_services, guardrails, guardrail_policies
from backend.api.services import router
from backend.api.users import delete_user
from backend.database import Base, get_db
from backend.models import Agent, User
from backend.memory_store import MemoryEntry
from backend.guardrail_models import GuardrailPolicy, GuardrailBlocklist
from backend.runtime import builtin_tools
from backend.service_models import CustomService, ServiceRun
from backend.net_guard import OutboundBlocked
from backend.security import get_current_user


def process_running(pid):
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, 0, pid)
        if not handle:
            return False
        code = ctypes.c_uint32()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False

ECHO = "import json,os,sys; x=json.load(sys.stdin); print(json.dumps({'sum':x.get('n',0)+2,'text':'中文','ambient':os.environ.get('SERVICE_AMBIENT_SECRET'),'credential':os.environ.get('SERVICE_CREDENTIAL')},ensure_ascii=False))"


class CustomServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _): connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.patches = [patch.object(module, "SessionLocal", self.sessions) for module in (custom_services, approvals, guardrails, guardrail_policies)]
        for item in self.patches: item.start()
        with self.sessions() as db:
            db.add_all([User(id=1, username="root", role="root", password_hash="x"), User(id=2, username="admin", role="admin", password_hash="x", permissions='["services"]'), User(id=3, username="user", role="user", password_hash="x", permissions='["services"]')])
            db.flush()
            db.add_all([Agent(id=1, name="owned", created_by=1, enabled=True), Agent(id=2, name="other", created_by=2, enabled=True)])
            db.commit()
        self.user = User(id=1, username="root", role="root")
        app = FastAPI(); app.include_router(router)
        def db_session():
            with self.sessions() as db: yield db
        app.dependency_overrides[get_db] = db_session
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches): item.stop()
        self.engine.dispose(); self.temp.cleanup()

    def program_body(self, code=ECHO, **changes):
        return {"name": "JSON processor", "kind": "program", "config": {"command": sys.executable, "args": ["-X", "utf8", "-c", code], "timeout_seconds": 5}, "agent_ids": [1], **changes}

    def create(self, body=None):
        response = self.client.post("/api/v1/services", json=body or self.program_body())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def configure(self, **changes):
        with self.sessions() as db:
            current = guardrails.read_config(db)
            guardrails.save_config(db, guardrails.GuardrailConfig(**{**current["config"].model_dump(), **changes}), current["revision"])

    def test_real_python_json_service_unicode_and_execution_data(self):
        row = self.create()
        response = self.client.post(f"/api/v1/services/{row['id']}/run", json={"input": {"n": 5}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["sum"], 7)
        self.assertEqual(response.json()["result"]["text"], "中文")
        runs = self.client.get("/api/v1/services/runs").json()
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(json.loads(runs[0]["result"])["sum"], 7)

    def test_credentials_are_encrypted_masked_preserved_and_not_returned_by_service(self):
        body = self.program_body()
        secret = "test-service-credential-value"
        body["config"]["env"] = {"SERVICE_CREDENTIAL": secret}
        with patch.dict(os.environ, {"SERVICE_AMBIENT_SECRET": "host-only-value"}):
            row = self.create(body)
            self.assertEqual(row["config"]["env"]["SERVICE_CREDENTIAL"], "********")
            response = self.client.post(f"/api/v1/services/{row['id']}/run", json={"input": {}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["result"]["ambient"])
        self.assertEqual(response.json()["result"]["credential"], "********")
        with self.engine.connect() as connection:
            raw = connection.execute(text("SELECT config FROM custom_services")).scalar()
            self.assertNotIn(secret, raw)
        body.update(config=row["config"], revision=row["revision"])
        self.assertEqual(self.client.put(f"/api/v1/services/{row['id']}", json=body).status_code, 200)
        with self.sessions() as db:
            self.assertEqual(json.loads(db.get(CustomService, row["id"]).config)["env"]["SERVICE_CREDENTIAL"], secret)
            self.assertNotIn(secret, db.query(ServiceRun).first().result)

    def test_only_root_registers_programs_and_shared_users_cannot_read_config(self):
        row = self.create(self.program_body(is_public=True))
        for user_id, role in ((2, "admin"), (3, "user")):
            self.user = User(id=user_id, username=role, role=role, permissions='["services"]')
            self.assertEqual(self.client.post("/api/v1/services", json=self.program_body()).status_code, 403)
            shared = self.client.get("/api/v1/services").json()[0]
            self.assertFalse(shared["can_manage"])
            self.assertNotIn("command", shared["config"])
            self.assertEqual(self.client.delete(f"/api/v1/services/{row['id']}").status_code, 404)
            self.assertEqual(self.client.post(f"/api/v1/services/{row['id']}/run", json={"input": {}}).status_code, 200)
            self.assertEqual(len(self.client.get("/api/v1/services/runs").json()), 1)

    def test_cas_rejects_stale_updates_and_preserves_latest(self):
        body = self.program_body(); row = self.create(body)
        body.update(revision=row["revision"], name="new revision")
        self.assertEqual(self.client.put(f"/api/v1/services/{row['id']}", json=body).status_code, 200)
        body["name"] = "stale edit"
        self.assertEqual(self.client.put(f"/api/v1/services/{row['id']}", json=body).status_code, 409)
        self.assertEqual(self.client.get("/api/v1/services").json()[0]["name"], "new revision")

    def test_bound_agent_and_resource_acl_rechecked_for_execution(self):
        row = self.create()
        self.assertEqual(len(custom_services.service_definitions(1, 1)), 1)
        self.assertEqual(custom_services.service_definitions(1, 2), [])
        with self.assertRaises(HTTPException):
            asyncio.run(custom_services.execute_service(row["id"], {}, 1, 2, approval_policy="full_access"))
        with self.assertRaises(HTTPException):
            asyncio.run(custom_services.execute_service(row["id"], {}, 2, 2, approval_policy="full_access"))
        result = asyncio.run(custom_services.execute_service(row["id"], {"n": 1}, 1, 1, approval_policy="full_access"))
        self.assertEqual(result["sum"], 3)
        with self.sessions() as db:
            db.get(CustomService, row["id"]).enabled = False; db.commit()
        self.assertEqual(custom_services.service_definitions(1, 1), [])
        with self.assertRaises(HTTPException):
            asyncio.run(custom_services.execute_service(row["id"], {}, 1, 1, approval_policy="full_access"))

    def test_inputs_validate_before_any_program_starts(self):
        row = self.create(self.program_body(input_fields=[{"name": "n", "type": "number", "required": True}]))
        with patch.object(custom_services, "_program", new_callable=AsyncMock) as program:
            for value in ({}, {"n": True}, {"n": "3"}, {"n": 1, "extra": 1}):
                response = self.client.post(f"/api/v1/services/{row['id']}/run", json={"input": value})
                self.assertEqual(response.status_code, 400)
            program.assert_not_awaited()

    def test_global_blocks_and_forced_approval_apply_to_playground(self):
        row = self.create()
        with patch.object(custom_services, "_program", new_callable=AsyncMock) as program:
            for rule in ({"block_mutating_tools": True}, {"blocked_tools": [f"service:{row['id']}"]}, {"blocked_tools": ["service_call"]}, {"require_external_approval": True}):
                with self.sessions() as db:
                    current = guardrails.read_config(db)
                    guardrails.save_config(db, guardrails.GuardrailConfig(**rule), current["revision"])
                response = self.client.post(f"/api/v1/services/{row['id']}/run", json={"input": {}})
                self.assertEqual(response.status_code, 403, response.text)
            program.assert_not_awaited()

    def test_agent_approval_is_single_use_and_bound_to_exact_service(self):
        row = self.create()
        with self.assertRaises(approvals.ApprovalRequired) as raised:
            asyncio.run(custom_services.execute_service(row["id"], {}, 1, 1, run_id="test-run"))
        self.assertEqual(raised.exception.scope, f"service:{row['id']}")
        token = approvals.issue("test-run", 1, 1, f"service:{row['id']}")
        result = asyncio.run(custom_services.execute_service(row["id"], {}, 1, 1, run_id="test-run", approval_tokens=[token]))
        self.assertEqual(result["sum"], 2)
        with self.assertRaises(approvals.ApprovalRequired):
            asyncio.run(custom_services.execute_service(row["id"], {}, 1, 1, run_id="test-run", approval_tokens=[token]))

    def test_builtin_service_approval_round_trip_has_no_double_approval_loop(self):
        row = self.create()
        ctx = builtin_tools.BuiltinToolContext(root=self.temp_path, user_id=1, agent_id=1, run_id="builtin-service-run", enabled_tools={"service_call"})
        args = {"service_id": row["id"], "input": {"n": 7}, "confirm": True}
        with self.assertRaises(approvals.ApprovalRequired) as raised:
            asyncio.run(builtin_tools.execute("service_call", args, ctx))
        self.assertEqual(raised.exception.scope, f"service:{row['id']}")
        ctx.approval_tokens = [approvals.issue(ctx.run_id, 1, 1, raised.exception.scope)]
        result = json.loads(asyncio.run(builtin_tools.execute("service_call", args, ctx)))
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["sum"], 9)

    def test_named_input_and_output_content_guardrails_are_enforced(self):
        row = self.create()
        async def block_input(point, *_args, **_kwargs):
            if point == "tool_input": raise guardrail_policies.ContentBlocked(point)
        with patch.object(custom_services, "enforce_content", side_effect=block_input), patch.object(custom_services, "_program", new_callable=AsyncMock) as program:
            self.assertEqual(self.client.post(f"/api/v1/services/{row['id']}/run", json={"input": {}}).status_code, 403)
            program.assert_not_awaited()
        async def block_output(point, *_args, **_kwargs):
            if point == "tool_output": raise guardrail_policies.ContentBlocked(point)
        with patch.object(custom_services, "enforce_content", side_effect=block_output):
            response = self.client.post(f"/api/v1/services/{row['id']}/run", json={"input": {}})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('"sum"', response.text)

    def test_http_ssrf_blocks_before_network_and_redirects_are_never_followed(self):
        with patch("backend.net_guard.settings.SSRF_ALLOW_PRIVATE", False), patch("backend.net_guard.settings.SSRF_ALLOWLIST", ""), patch.object(httpx, "AsyncClient") as client:
            with self.assertRaises(OutboundBlocked):
                asyncio.run(custom_services._http({"url": "http://127.0.0.1/private"}, {}))
            client.assert_not_called()
        calls = []
        def remote(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        real_client = httpx.AsyncClient
        with patch.object(custom_services, "validate_outbound_url"), patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(remote), **kwargs)):
            with self.assertRaisesRegex(ValueError, "302"):
                asyncio.run(custom_services._http({"url": "https://service.example/run"}, {}))
        self.assertEqual(calls, ["https://service.example/run"])

    def test_http_json_execution_and_output_bound(self):
        body = {"name": "web lookup", "kind": "http", "config": {"url": "https://service.example/run", "method": "GET"}, "agent_ids": [1]}
        row = self.create(body)
        real_client = httpx.AsyncClient
        def remote(request):
            self.assertEqual(request.url.params["topic"], "Phoenix")
            return httpx.Response(200, json={"answer": "Friday"})
        with patch.object(custom_services, "validate_outbound_url"), patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(remote), **kwargs)):
            result = asyncio.run(custom_services.execute_service(row["id"], {"topic": "Phoenix"}, 1, 1))
        self.assertEqual(result["answer"], "Friday")
        with patch.object(custom_services, "validate_outbound_url"), patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * (custom_services.MAX_IO + 1))), **kwargs)):
            with self.assertRaisesRegex(ValueError, "1 MB"):
                asyncio.run(custom_services._http(body["config"], {}))

    def test_program_output_limit_stderr_drain_and_invalid_json(self):
        for code, error in (("import sys;sys.stdout.write('x'*1000001)", "1 MB"), ("print('invalid json')", "JSON"), ("import sys;sys.stderr.write('secret failure');sys.exit(4)", "退出码 4")):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, error):
                asyncio.run(custom_services._program(self.program_body(code)["config"], {}))
        code = "import sys;sys.stderr.write('x'*100000); print('{\"ok\":true}')"
        self.assertEqual(asyncio.run(custom_services._program(self.program_body(code)["config"], {})), {"ok": True})

    def child_program(self, parent_exits=False):
        pid_file = self.temp_path / ("exit-pids.json" if parent_exits else "pids.json")
        code = ("import os,subprocess,sys,time,json,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0); "
                f"pathlib.Path({str(pid_file)!r}).write_text(json.dumps([os.getpid(),p.pid])); "
                "print('{}',flush=True); " + ("sys.exit(0)" if parent_exits else "time.sleep(120)"))
        return pid_file, self.program_body(code)

    async def wait_pids(self, pid_file):
        for _ in range(200):
            if pid_file.exists():
                try: return json.loads(pid_file.read_text())
                except json.JSONDecodeError: pass
            await asyncio.sleep(.01)
        self.fail("fixture process did not publish its PID")

    def assert_stopped(self, pids):
        for _ in range(100):
            if not any(process_running(pid) for pid in pids): return
            time.sleep(.01)
        self.fail(f"process tree still alive: {pids}")

    def test_timeout_and_cancellation_kill_program_tree_and_record_real_status(self):
        for cancel in (False, True):
            pid_file, body = self.child_program()
            if pid_file.exists(): pid_file.unlink()
            body["config"]["timeout_seconds"] = 1
            row = self.create(body)
            async def exercise():
                task = asyncio.create_task(custom_services.execute_service(row["id"], {}, 1, playground=True))
                pids = await self.wait_pids(pid_file)
                if cancel: task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else ValueError): await task
                return pids
            self.assert_stopped(asyncio.run(exercise()))
            with self.sessions() as db:
                latest = db.query(ServiceRun).order_by(ServiceRun.id.desc()).first()
                self.assertEqual(latest.status, "cancelled" if cancel else "failed")

    def test_parent_exit_does_not_leave_detached_child_running(self):
        pid_file, body = self.child_program(parent_exits=True)
        self.assertEqual(asyncio.run(custom_services._program(body["config"], {})), {})
        self.assert_stopped(json.loads(pid_file.read_text()))

    def test_user_deletion_reports_new_protected_dependencies(self):
        with self.sessions() as db:
            db.add_all([CustomService(name="kept", created_by=2), ServiceRun(service_id=1, service_name="kept", owner_id=2, status="completed"), GuardrailPolicy(name="policy", created_by=2), GuardrailBlocklist(name="list", created_by=2)])
            db.commit()
            with self.assertRaises(HTTPException) as raised:
                delete_user(2, current=db.get(User, 1), db=db)
            self.assertEqual(raised.exception.status_code, 409)
            for label in ("自定义服务 1", "服务运行记录 1", "护栏策略 1", "护栏阻止列表 1"):
                self.assertIn(label, raised.exception.detail)
            self.assertIsNotNone(db.get(User, 2))

    def test_user_deletion_cascades_only_its_explicit_memories(self):
        with self.sessions() as db:
            db.add(User(id=4, username="memory-owner", password_hash="x", role="user")); db.flush()
            db.add_all([MemoryEntry(owner_id=4, scope="global", title="private", content="delete me"), MemoryEntry(owner_id=1, scope="global", title="root-private", content="keep me")]); db.commit()
            delete_user(4, current=db.get(User, 1), db=db)
            self.assertIsNone(db.get(User, 4))
            self.assertEqual(db.query(MemoryEntry).filter(MemoryEntry.owner_id == 4).count(), 0)
            self.assertEqual(db.query(MemoryEntry).filter(MemoryEntry.owner_id == 1).count(), 1)


if __name__ == "__main__":
    unittest.main()
