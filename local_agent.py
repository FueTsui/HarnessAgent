"""Portable local entry point; imports the server only after isolating its home."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import threading
import time

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 17650


def default_home() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local/share")) / "HarnessAgent"


@contextlib.contextmanager
def instance_lock(home: Path):
    """The OS releases this lock even if the process crashes; no stale PID kills."""
    home.mkdir(parents=True, exist_ok=True)
    with (home / ".instance.lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("此数据目录的 Agent 已在运行，请使用原控制窗口。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_config(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def prepare_home(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    config_file = home / ".env"
    if not config_file.exists():
        if (home / "data/app.db").exists():
            raise RuntimeError("数据目录存在数据库但缺少 .env；请恢复原密钥配置，不能生成替代密钥。")
        values = {
            "APP_ENV": "development",
            "JWT_SECRET": secrets.token_urlsafe(48),
            "SECRET_MASTER_KEY": secrets.token_urlsafe(48),
            "ROOT_USERNAME": "root",
            "ROOT_PASSWORD": secrets.token_urlsafe(18),
            "ALLOW_INSECURE_DEFAULTS": "false",
            "AUTH_COOKIE_SECURE": "false",
            "SHELL_TOOL_ENABLED": "false",
            "SSRF_ALLOW_PRIVATE": "false",
            # Allow local model servers explicitly; other private addresses stay blocked.
            "SSRF_ALLOWLIST": "127.0.0.1,localhost,::1",
            "JOB_WORKER_ENABLED": "true",
            "CRON_SCHEDULER_ENABLED": "true",
        }
        text = "# Harness Agent 本地配置；备份时必须与 data 目录一起保存。\n"
        text += "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
        with config_file.open("x", encoding="utf-8") as stream:
            stream.write(text)
        config_file.chmod(0o600)
    values = read_config(config_file)
    if not (home / "data/app.db").exists() and not (home / "first-run.txt").exists():
        note = (
            "Harness Agent 首次登录\n\n"
            f"账号：{values.get('ROOT_USERNAME', 'root')}\n"
            f"密码：{values.get('ROOT_PASSWORD', '')}\n\n"
            "登录后可在账户设置中修改密码。修改后本文件中的初始密码失效。\n"
            "请勿分享 .env、此文件或包含私人数据的整个数据目录。\n"
        )
        with (home / "first-run.txt").open("x", encoding="utf-8-sig") as stream:
            stream.write(note)
        (home / "first-run.txt").chmod(0o600)
    for name in ("logs", "data/branding", "data/workspaces"):
        (home / name).mkdir(parents=True, exist_ok=True)
    defaults = APP_DIR / "packaging/defaults/branding"
    if defaults.is_dir():
        for source in defaults.iterdir():
            target = home / "data/branding" / source.name
            if source.is_file() and not target.exists():
                shutil.copyfile(source, target)
    return values


def configure_environment(home: Path, values: dict[str, str]) -> None:
    # A desktop instance never inherits the repository's deployment or model secrets.
    source = (APP_DIR / "backend/config.py").read_text(encoding="utf-8")
    names = re.findall(r'(?:os\.getenv|_env_bool)\(\s*"([A-Z][A-Z0-9_]+)"', source)
    for name in names:
        os.environ.pop(name, None)
    os.environ.update(values)
    data = home / "data"
    os.environ.update({
        "APP_ENV_FILE": str(home / ".env"),
        "APP_DATA_DIR": str(data),
        "AGENT_WORKSPACE_ROOT": str(data / "workspaces"),
        "DATABASE_URL": "sqlite:///" + (data / "app.db").as_posix(),
        "APP_HOST": "127.0.0.1",
        "CORS_ORIGINS": "",
        "TRUST_PROXY_HEADERS": "false",
        "AUTH_COOKIE_NAME": "harness_local_session",
    })


def reserve_socket(port: int) -> socket.socket:
    candidates = [0] if port == 0 else range(port, min(port + 20, 65536))
    for candidate in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", candidate))
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except OSError:
            sock.close()
    raise RuntimeError("本地端口不可用，请用 --port 指定其他起始端口。")


def write_status(path: Path, state: str, url: str = "", **extra) -> None:
    payload = {"state": state, "pid": os.getpid(), "url": url, **extra}
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # A launcher may briefly have the status file open on Windows.
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def status_path(home: Path, supplied: str | Path | None) -> Path:
    # Resolve the containing directory, not the existing leaf. Windows can
    # redirect an AppData file into a package cache even when its directory is
    # not redirected. Publication atomically replaces this leaf; it never
    # follows a link to overwrite the previous target.
    path = Path(os.path.abspath(supplied or (home / "runtime.json")))
    if (not path.is_relative_to(home) or not path.parent.resolve().is_relative_to(home)
            or path.is_symlink() or path.is_junction()):
        raise ValueError("--status-file 必须位于 --home 中")
    return path


async def serve(args, home: Path, status_file: Path) -> int:
    import uvicorn
    import httpx

    sock = reserve_socket(args.port)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    os.environ["APP_PORT"] = str(port)
    config = uvicorn.Config(
        "backend.main:app", host="127.0.0.1", port=port,
        access_log=False, log_level="info", proxy_headers=False,
    )
    server = uvicorn.Server(config)
    stop_requested = threading.Event()

    def read_commands():
        if sys.stdin is None:
            if args.managed:
                stop_requested.set()
            return
        try:
            for line in sys.stdin:
                if line.strip().lower() == "stop":
                    stop_requested.set()
                    return
        finally:
            if args.managed:
                stop_requested.set()

    threading.Thread(target=read_commands, name="local-agent-control", daemon=True).start()
    task = asyncio.create_task(server.serve(sockets=[sock]))
    ready = False
    started_at = time.monotonic()
    failure = ""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while not task.done():
                if stop_requested.is_set():
                    server.should_exit = True
                    write_status(status_file, "stopping", url)
                    break
                if server.started and not ready:
                    try:
                        response = await client.get(url + "healthz")
                        ready = response.status_code == 200
                    except httpx.HTTPError:
                        pass
                    if ready:
                        write_status(status_file, "ready", url)
                        print(f"Harness Agent ready: {url}", flush=True)
                if not ready and time.monotonic() - started_at > 90:
                    failure = "启动超过 90 秒仍未就绪，请检查 logs 目录。"
                    server.should_exit = True
                    break
                await asyncio.sleep(0.25)
        await task
        if not ready and not stop_requested.is_set() and not failure:
            failure = "服务启动失败，请检查 logs 目录。"
        write_status(status_file, "error" if failure else "stopped", url, message=failure)
        return 1 if failure else 0
    finally:
        server.should_exit = True
        sock.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Harness Agent 本地独立运行程序")
    parser.add_argument("command", choices=["serve"], nargs="?", default="serve")
    parser.add_argument("--home", type=Path, default=default_home())
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--managed", action="store_true", help="控制窗口管道关闭时自动停止")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    home = args.home.expanduser().resolve()
    try:
        status_file = status_path(home, args.status_file)
    except ValueError as exc:
        parser.error(str(exc))
    status_file.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        with instance_lock(home):
            acquired = True
            write_status(status_file, "starting")
            values = prepare_home(home)
            configure_environment(home, values)
            # Relative service/plugin paths are local to this instance, never the caller cwd.
            os.chdir(home)
            return asyncio.run(serve(args, home, status_file))
    except (Exception, SystemExit) as exc:
        if acquired:
            write_status(status_file, "error", message="启动失败，请查看日志。")
        # Do not print environment values or exception strings that can contain credentials.
        print(f"Harness Agent startup failed ({type(exc).__name__}). Check local logs/configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
