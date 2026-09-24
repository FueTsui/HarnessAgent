"""Real stdio transport and root-only launch-configuration regressions."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api import mcp as mcp_api
from backend.database import Base
from backend.llm import mcp_client
from backend.models import McpServer, User
from backend.schemas import McpServerCreate, McpServerUpdate


SERVER = r'''
import json, os, subprocess, sys, time
mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
child = None
if mode == "spawn":
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
initialized = False
for raw in sys.stdin:
    body = json.loads(raw)
    method = body.get("method")
    if method == "notifications/initialized":
        initialized = True
        continue
    if method == "initialize":
        result = {"protocolVersion": "invalid" if mode == "unsupported" else "2025-11-25", "serverInfo": {"name": "fixture", "version": "1"}, "capabilities": {"tools": {}}}
    elif method == "tools/list":
        assert initialized
        if mode == "invalid":
            print("this is not json", flush=True)
            continue
        result = {"tools": [{"name": "echo", "description": "echo tool", "inputSchema": {"type": "object"}}]}
        if not body.get("params", {}).get("cursor"):
            result["nextCursor"] = "page-2"
        else:
            result = {"tools": [{"name": "second", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        args = body.get("params", {}).get("arguments", {})
        if args.get("hang"):
            time.sleep(120)
        if args.get("rpc_error"):
            print(json.dumps({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32603, "message": "do-not-leak-secret"}}), flush=True)
            continue
        if args.get("stderr"):
            sys.stderr.write("x" * 50000)
            sys.stderr.flush()
        if args.get("ping"):
            print(json.dumps({"jsonrpc": "2.0", "id": "server-ping", "method": "ping"}), flush=True)
            response = json.loads(sys.stdin.readline())
            assert response["id"] == "server-ping" and response["result"] == {}
        result = {"content": [{"type": "text", "text": "中文结果"}], "structuredContent": {"arguments": args, "explicit": os.environ.get("EXPLICIT"), "leaked": os.environ.get("MCP_TOP_SECRET"), "child_pid": child.pid if child else None}, "isError": bool(args.get("error"))}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}, ensure_ascii=False), flush=True)
    if method == "tools/call" and body.get("params", {}).get("arguments", {}).get("exit_parent"):
        sys.exit(0)
'''


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


class StdioTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mcp-stdio-")
        self.script = Path(self.directory.name) / "server.py"
        self.script.write_text(SERVER, encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def server(self, mode="normal", **kwargs):
        return SimpleNamespace(name="fixture", transport="stdio", command=sys.executable,
                               args=["-u", str(self.script), mode], env={"EXPLICIT": "configured"},
                               cwd=self.directory.name, stdio_authorized=kwargs.get("authorized", True))

    async def test_real_initialize_pagination_call_utf8_and_environment(self):
        with patch.dict(os.environ, {"MCP_TOP_SECRET": "must-not-inherit"}):
            async with mcp_client.McpConnection(self.server()) as connection:
                self.assertEqual([item["name"] for item in await connection.list_tools()], ["echo", "second"])
                result = await connection.call_tool_result("echo", {"text": "参数", "ping": True, "stderr": True})
                self.assertIsNone(result["structuredContent"]["leaked"])
                self.assertEqual(result["structuredContent"]["explicit"], "configured")
                self.assertEqual(result["structuredContent"]["arguments"]["text"], "参数")
                self.assertIn("中文结果", mcp_client.flatten_tool_result(result))
                self.assertLessEqual(len(connection._session.stderr_tail), mcp_client._STDIO_STDERR_LIMIT)
                pid = connection._session.process.pid
            self.assertFalse(process_running(pid))

    async def test_timeout_cleans_parent_and_descendant(self):
        async with mcp_client.McpConnection(self.server("spawn")) as connection:
            child = (await connection.call_tool_result("echo", {}))["structuredContent"]["child_pid"]
            parent = connection._session.process.pid
            with patch.object(mcp_client, "MCP_TIMEOUT", 0.2):
                with self.assertRaisesRegex(RuntimeError, "超时"):
                    await connection.call_tool_result("echo", {"hang": True})
            self.assertFalse(process_running(parent))
            self.assertFalse(process_running(child))

    async def test_cancel_cleans_parent_and_descendant(self):
        async with mcp_client.McpConnection(self.server("spawn")) as connection:
            child = (await connection.call_tool_result("echo", {}))["structuredContent"]["child_pid"]
            parent = connection._session.process.pid
            request = asyncio.create_task(connection.call_tool_result("echo", {"hang": True}))
            await asyncio.sleep(0.08)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertFalse(process_running(parent))
            self.assertFalse(process_running(child))

    async def test_invalid_stdout_and_unsupported_version_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "协议版本"):
            async with mcp_client.McpConnection(self.server("unsupported")):
                pass
        async with mcp_client.McpConnection(self.server("invalid")) as connection:
            pid = connection._session.process.pid
            with self.assertRaisesRegex(RuntimeError, "无效 JSON-RPC"):
                await connection.list_tools()
            self.assertFalse(process_running(pid))

    async def test_unapproved_server_never_spawns(self):
        with patch.object(subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "root"):
                await mcp_client.list_tools(self.server(authorized=False))
            spawn.assert_not_called()

    async def test_rpc_errors_do_not_echo_secrets(self):
        async with mcp_client.McpConnection(self.server()) as connection:
            with self.assertRaises(RuntimeError) as failure:
                await connection.call_tool_result("echo", {"rpc_error": True})
            self.assertNotIn("do-not-leak-secret", str(failure.exception))

    async def test_descendant_is_cleaned_after_parent_exits(self):
        async with mcp_client.McpConnection(self.server("spawn")) as connection:
            child = (await connection.call_tool_result("echo", {"exit_parent": True}))["structuredContent"]["child_pid"]
            await asyncio.to_thread(connection._session.process.wait, timeout=3)
        self.assertFalse(process_running(child))


class StdioManagementTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.root = User(username="stdio-root", password_hash="unused", role="root", is_active=True)
        self.admin = User(username="stdio-admin", password_hash="unused", role="admin", is_active=True)
        self.db.add_all([self.root, self.admin])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def body(self, **changes):
        return McpServerCreate(**{"name": "local", "transport": "stdio", "command": sys.executable, "args": ["server.py"], "env": {"API_TOKEN": "secret-value"}, **changes})

    def test_only_root_may_create_modify_and_authorize_stdio(self):
        with self.assertRaises(HTTPException) as blocked:
            mcp_api.create_server(self.body(), self.admin, self.db)
        self.assertEqual(blocked.exception.status_code, 403)
        created = mcp_api.create_server(self.body(), self.root, self.db)
        self.assertTrue(created.stdio_authorized)
        self.assertEqual(created.env["API_TOKEN"], "********")
        with self.assertRaises(HTTPException) as blocked:
            mcp_api.update_server(created.id, McpServerUpdate(command="anything"), self.admin, self.db)
        self.assertEqual(blocked.exception.status_code, 403)
        self.assertIn("root", blocked.exception.detail)

    def test_env_mask_preservation_and_shared_out_redaction(self):
        created = mcp_api.create_server(self.body(), self.root, self.db)
        changed = mcp_api.update_server(created.id, McpServerUpdate(env={"API_TOKEN": "********"}, is_public=True), self.root, self.db)
        server = self.db.get(McpServer, changed.id)
        self.assertEqual(json.loads(server.env)["API_TOKEN"], "secret-value")
        shared = mcp_api._to_out(server, self.admin, self.db)
        self.assertFalse(shared.can_manage)
        self.assertEqual(shared.command, "")
        self.assertEqual(shared.args, [])
        self.assertNotIn("secret-value", json.dumps(shared.model_dump()))
        exported = mcp_api._mcp_dict(server, self.root)
        self.assertNotIn("secret-value", json.dumps(exported))
        mcp_api.update_server(created.id, McpServerUpdate(env={}), self.root, self.db)
        self.assertEqual(json.loads(server.env), {})

    def test_import_standard_mcpservers_and_deny_mixed_nonroot_batch(self):
        payload = {"mcpServers": {"fixture": {"command": sys.executable, "args": ["server.py"], "env": {"TOKEN": "private"}}}}
        file = UploadFile(filename="mcp.json", file=io.BytesIO(json.dumps(payload).encode()))
        result = asyncio.run(mcp_api.import_servers(file, self.root, self.db))
        self.assertEqual(result["imported"], 1)
        payload = [{"name": "remote", "url": "https://example.com/mcp"}, self.body(name="forbidden").model_dump()]
        file = UploadFile(filename="mcp.json", file=io.BytesIO(json.dumps(payload).encode()))
        with self.assertRaises(HTTPException):
            asyncio.run(mcp_api.import_servers(file, self.admin, self.db))
        self.assertIsNone(self.db.query(McpServer).filter_by(name="remote").first())

    def test_http_still_works_and_requires_url(self):
        created = mcp_api.create_server(McpServerCreate(name="remote", url="https://example.com/mcp"), self.admin, self.db)
        self.assertEqual(created.transport, "http")
        updated = mcp_api.update_server(created.id, McpServerUpdate(command="", args=[], env={}, cwd="", description="metadata"), self.admin, self.db)
        self.assertEqual(updated.description, "metadata")
        with self.assertRaises(HTTPException):
            mcp_api.create_server(McpServerCreate(name="missing"), self.admin, self.db)

    def test_protocol_switch_preserves_inactive_fields_without_launch_authority(self):
        created = mcp_api.create_server(self.body(), self.root, self.db)
        remote = mcp_api.update_server(created.id, McpServerUpdate(transport="http", url="https://example.com/mcp"), self.root, self.db)
        self.assertFalse(remote.stdio_authorized)
        self.assertEqual(remote.command, sys.executable)
        self.assertEqual(remote.env["API_TOKEN"], "********")
        restored = mcp_api.update_server(created.id, McpServerUpdate(transport="stdio"), self.root, self.db)
        self.assertTrue(restored.stdio_authorized)
        self.assertEqual(restored.args, ["server.py"])
        self.assertEqual(json.loads(self.db.get(McpServer, created.id).env)["API_TOKEN"], "secret-value")

    def test_root_npx_parameters_are_preserved_and_env_is_explicit(self):
        mcp_client.validate_stdio_config("npx", ["-y", "@example/server"], {}, "")
        mcp_client.validate_stdio_config("npx", ["--no-install", "installed-package"], {}, "")
        created = mcp_api.create_server(self.body(command="npx", args=["-y", "@example/server"]), self.root, self.db)
        self.assertEqual(created.args, ["-y", "@example/server"])
        self.assertTrue(created.stdio_authorized)
        with patch.dict(os.environ, {"APP_SECRET": "hidden", "EXPLICIT_SECRET": "chosen"}):
            env = mcp_client.stdio_environment({"TOKEN": "${EXPLICIT_SECRET}"})
            self.assertNotIn("APP_SECRET", env)
            self.assertNotIn("EXPLICIT_SECRET", env)
            self.assertEqual(env["TOKEN"], "chosen")

    def test_windows_npm_wrappers_resolve_to_node_without_shell_or_argument_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            node = base / "node.exe"
            node.touch()
            npm_bin = base / "node_modules" / "npm" / "bin"
            npm_bin.mkdir(parents=True)
            for executable, entry in (("npx", "npx-cli.js"), ("npm", "npm-cli.js")):
                wrapper = base / f"{executable}.cmd"
                wrapper.touch()
                (npm_bin / entry).touch()
                args = ["-y", "@example/server", "two words", "&literal"]
                with patch.object(mcp_client.os, "name", "nt"), patch.object(mcp_client.shutil, "which", return_value=str(wrapper)), patch.object(mcp_client.subprocess, "Popen") as spawn:
                    argv = mcp_client.resolve_stdio_argv(executable, args, {"PATH": directory})
                    self.assertEqual(argv, [str(node), str(npm_bin / entry), *args])
                    spawn.assert_not_called()
            with patch.object(mcp_client.os, "name", "nt"), patch.object(mcp_client.shutil, "which", return_value=str(base / "other.cmd")):
                with self.assertRaisesRegex(RuntimeError, "不通过命令解释器"):
                    mcp_client.resolve_stdio_argv("other.cmd", [], {"PATH": directory})
            (npm_bin / "npx-cli.js").unlink()
            with patch.object(mcp_client.os, "name", "nt"), patch.object(mcp_client.shutil, "which", return_value=str(base / "npx.cmd")):
                with self.assertRaisesRegex(RuntimeError, "node.exe.*完整路径"):
                    mcp_client.resolve_stdio_argv("npx", ["-y", "@example/server"], {"PATH": directory})

    def test_launch_fields_are_in_execution_snapshot_and_versions_only_hash_them(self):
        from backend.api.chat import _mcp_snapshot
        created = mcp_api.create_server(self.body(), self.root, self.db)
        server = self.db.get(McpServer, created.id)
        frozen = _mcp_snapshot(server)
        self.assertTrue(frozen["stdio_authorized"])
        self.assertEqual(frozen["command"], sys.executable)
        self.assertEqual(json.loads(frozen["env"])["API_TOKEN"], "secret-value")
        versions = mcp_api.list_server_versions(server.id, self.root, self.db)
        self.assertNotIn("secret-value", json.dumps(versions))
        self.assertIn("connection_sha256", json.dumps(versions))


if __name__ == "__main__":
    unittest.main()
