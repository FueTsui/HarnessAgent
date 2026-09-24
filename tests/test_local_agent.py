"""Local entry-point boundaries, without importing or opening the deployment DB."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tested_local_agent", ROOT / "local_agent.py")
local_agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_agent)


class LocalAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "独立数据"

    def test_home_initialization_preserves_secrets_and_user_branding(self):
        values = local_agent.prepare_home(self.home)
        self.assertGreaterEqual(len(values["JWT_SECRET"]), 48)
        self.assertNotEqual(values["JWT_SECRET"], values["SECRET_MASTER_KEY"])
        self.assertNotEqual(values["ROOT_PASSWORD"], "Root@123456")
        self.assertEqual(values["ALLOW_INSECURE_DEFAULTS"], "false")
        saved = (self.home / ".env").read_bytes()
        custom_logo = self.home / "data/branding/favicon.svg"
        custom_logo.write_text("user-branding", encoding="utf-8")
        self.assertEqual(local_agent.prepare_home(self.home), values)
        self.assertEqual((self.home / ".env").read_bytes(), saved)
        self.assertEqual(custom_logo.read_text(encoding="utf-8"), "user-branding")
        self.assertTrue((self.home / "first-run.txt").is_file())

    def test_existing_database_without_keys_refuses_to_generate_replacements(self):
        database = self.home / "data/app.db"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"existing database sentinel")
        with self.assertRaisesRegex(RuntimeError, "缺少 .env"):
            local_agent.prepare_home(self.home)
        self.assertFalse((self.home / ".env").exists())
        self.assertEqual(database.read_bytes(), b"existing database sentinel")

    def test_configuration_overrides_ambient_deployment_and_forces_local_storage(self):
        values = local_agent.prepare_home(self.home)
        ambient = {
            "APP_DATA_DIR": str(self.root / "foreign-data"),
            "DATABASE_URL": "sqlite:///foreign.db",
            "JWT_SECRET": "ambient-secret",
            "IMAGE_API_KEY": "ambient-image-key",
            "LLM_API_KEY": "ambient-model-key",
            "APP_HOST": "0.0.0.0", "CORS_ORIGINS": "*",
            "TRUST_PROXY_HEADERS": "true", "APP_ENV_FILE": "foreign.env",
        }
        with patch.dict(os.environ, ambient):
            local_agent.configure_environment(self.home, values)
            self.assertEqual(os.environ["JWT_SECRET"], values["JWT_SECRET"])
            self.assertEqual(os.environ["APP_HOST"], "127.0.0.1")
            self.assertEqual(os.environ["CORS_ORIGINS"], "")
            self.assertEqual(os.environ["TRUST_PROXY_HEADERS"], "false")
            self.assertEqual(Path(os.environ["APP_ENV_FILE"]), self.home / ".env")
            self.assertEqual(Path(os.environ["APP_DATA_DIR"]), self.home / "data")
            self.assertEqual(os.environ["DATABASE_URL"], "sqlite:///" + (self.home / "data/app.db").as_posix())
            self.assertNotIn("IMAGE_API_KEY", os.environ)
            self.assertNotIn("LLM_API_KEY", os.environ)

    def test_configured_paths_cannot_escape_home_via_user_env(self):
        values = local_agent.prepare_home(self.home)
        values.update(APP_DATA_DIR=str(self.root / "other"), DATABASE_URL="sqlite:///other.db",
                      AGENT_WORKSPACE_ROOT=str(self.root / "other-work"), APP_HOST="0.0.0.0")
        with patch.dict(os.environ):
            local_agent.configure_environment(self.home, values)
            self.assertEqual(Path(os.environ["AGENT_WORKSPACE_ROOT"]), self.home / "data/workspaces")
            self.assertEqual(os.environ["APP_HOST"], "127.0.0.1")

    def test_explicit_dotenv_is_loaded_before_data_paths(self):
        configured_data = self.root / "configured-data"
        configured_workspace = self.root / "configured-workspace"
        env_file = self.root / "config.env"
        env_file.write_text(f"APP_DATA_DIR={configured_data}\nAGENT_WORKSPACE_ROOT={configured_workspace}\n", encoding="utf-8-sig")
        environment = dict(os.environ)
        for name in ("APP_DATA_DIR", "AGENT_WORKSPACE_ROOT", "DATABASE_URL"):
            environment.pop(name, None)
        environment["APP_ENV_FILE"] = str(env_file)
        code = (
            "import json,sys;sys.path.insert(0,sys.argv[1]);"
            "from backend.config import DATA_DIR,WORKSPACE_DIR,settings;"
            "print(json.dumps([str(DATA_DIR),str(WORKSPACE_DIR),settings.DATABASE_URL]))"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT)],
                                cwd=self.root, env=environment, capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        data, workspace, database = json.loads(result.stdout)
        self.assertEqual(Path(data), configured_data)
        self.assertEqual(Path(workspace), configured_workspace)
        self.assertEqual(database, "sqlite:///" + (configured_data / "app.db").as_posix())

    def test_instance_lock_is_cross_process_and_reusable_after_release(self):
        code = (
            "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
            "from local_agent import instance_lock;"
            "\nwith instance_lock(Path(sys.argv[2])): print('acquired')\n"
        )
        command = [sys.executable, "-B", "-c", code, str(ROOT), str(self.home)]
        with local_agent.instance_lock(self.home):
            rejected = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertNotIn("acquired", rejected.stdout)
        accepted = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("acquired", accepted.stdout)

    def test_reserved_socket_is_loopback_and_falls_back_from_occupied_port(self):
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        if port == 65535:
            self.skipTest("OS selected final port; fallback range is empty")
        reserved = local_agent.reserve_socket(port)
        self.addCleanup(reserved.close)
        self.assertEqual(reserved.getsockname()[0], "127.0.0.1")
        self.assertGreater(reserved.getsockname()[1], port)
        self.assertLessEqual(reserved.getsockname()[1], min(port + 19, 65535))

    def test_status_cannot_write_outside_instance_home(self):
        outside = self.root / "foreign-status.json"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            local_agent.main(["--home", str(self.home), "--status-file", str(outside)])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(outside.exists())

    def test_status_publication_replaces_complete_json(self):
        self.home.mkdir()
        status = self.home / "runtime.json"
        local_agent.write_status(status, "starting")
        local_agent.write_status(status, "ready", "http://127.0.0.1:12345/")
        self.assertEqual(json.loads(status.read_text(encoding="utf-8"))["state"], "ready")
        self.assertEqual(list(self.home.glob("*.tmp")), [])

    def test_status_path_does_not_follow_a_virtualized_existing_leaf(self):
        self.home.mkdir()
        status = self.home / "runtime.json"
        original = Path.resolve
        def redirected(path, *args, **kwargs):
            if path == status:
                return self.root / "package-cache/runtime.json"
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected):
            self.assertEqual(local_agent.status_path(self.home, status), status)

    def test_status_path_rejects_linked_leaf_and_parent_escape(self):
        self.home.mkdir()
        status = self.home / "runtime.json"
        with patch.object(Path, "is_symlink", lambda path: path == status):
            with self.assertRaises(ValueError): local_agent.status_path(self.home, status)
        with self.assertRaises(ValueError):
            local_agent.status_path(self.home, self.home / "../foreign.json")


if __name__ == "__main__":
    unittest.main()
