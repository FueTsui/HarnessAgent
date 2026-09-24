"""Exercise a real packaged Agent turn against a local, deterministic model fixture."""
from __future__ import annotations
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import httpx

ANSWER = "本地独立 Agent 验收通过。"


class ModelFixture(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        ModelFixture.calls += 1
        if body.get("stream"):
            chunks = [
                {"id": "local-fixture", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ANSWER}, "finish_reason": None}]},
                {"id": "local-fixture", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}},
            ]
            data = ("".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
            mime = "text/event-stream"
        else:
            data = json.dumps({"id": "local-fixture", "object": "chat.completion", "model": "local-fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}, ensure_ascii=False).encode()
            mime = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    env = dict(line.split("=", 1) for line in (args.home / ".env").read_text(encoding="utf-8-sig").splitlines() if "=" in line and not line.startswith("#"))
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=args.url.rstrip("/"), trust_env=False, timeout=15) as client:
            response = client.post("/api/v1/auth/login", json={"username": env["ROOT_USERNAME"], "password": env["ROOT_PASSWORD"]})
            response.raise_for_status()
            response = client.post("/api/v1/providers", json={"name": "本地验收模型-" + str(int(time.time())), "base_url": f"http://127.0.0.1:{model.server_port}/v1", "model_id": "local-fixture", "model_name": "本地模拟模型（仅测试）", "auth_type": "none", "max_retries": 0, "stream_max_retries": 0})
            response.raise_for_status()
            provider = response.json()
            response = client.post("/api/v1/agents", json={"name": "独立运行验收", "provider_id": provider["id"], "system_prompt": "直接回答用户，不调用工具。", "builtin_tools": [], "skill_ids": [], "mcp_ids": [], "memory_enabled": False})
            response.raise_for_status()
            agent = response.json()
            response = client.post("/api/v1/chat", data={"agent_id": str(agent["id"]), "query": "请返回一句测试确认。"})
            response.raise_for_status()
            submitted = response.json()
            job_id = submitted.get("turn_id") or submitted.get("job_id") or submitted.get("run_id")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                response = client.get(f"/api/v1/chat/turns/{job_id}")
                response.raise_for_status()
                job = response.json()
                if job.get("status") in {"done", "failed", "cancelled", "dead_letter"}:
                    break
                time.sleep(.3)
            encoded = json.dumps(job, ensure_ascii=False)
            assert job.get("status") == "done", encoded
            assert ANSWER in encoded, encoded
            assert ModelFixture.calls > 0
            result = {"passed": True, "kind": "packaged-service-real-turn-local-model-fixture", "external_provider_tested": False, "agent_id": agent["id"], "job_id": job_id, "turn_id": submitted.get("turn_id"), "status": job["status"], "answer": ANSWER, "provider_requests": ModelFixture.calls}
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False))
    finally:
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    main()
