"""Independent visitor-boundary regression using only a local HTTP fixture."""
import asyncio
import json
import socket
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpcore
import httpx

from backend import guest_access, personal_network
from backend.guest_access import personal_model_client
from backend.llm import client as client_module
from backend.personal_network import PublicHTTPTransport, PublicNetworkBackend, public_addresses


class GuestConnectionBoundaryTests(unittest.TestCase):
    def test_personal_model_dns_rebinding_cannot_reach_loopback(self):
        reached = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                reached.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.dumps({"choices": [{"message": {"content": "local fixture"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        worker = threading.Thread(target=receiver.serve_forever, daemon=True)
        worker.start()
        real_resolve = socket.getaddrinfo
        resolved = []

        def changing_dns(host, port, *args, **kwargs):
            decoded = host.decode() if isinstance(host, bytes) else host
            if decoded == "visitor-rebind.example":
                # Validation sees a globally routable IP. Any independent
                # resolution by the HTTP transport sees our loopback receiver.
                target = "93.184.216.34" if not resolved else "127.0.0.1"
                resolved.append(target)
                return real_resolve(target, port, *args, **kwargs)
            return real_resolve(host, port, *args, **kwargs)

        provider = SimpleNamespace(
            id=9182, base_url=f"http://visitor-rebind.example:{receiver.server_port}/v1",
            api_key="fixture-only", model_id="fixture", model_input='["text"]',
            wire_api="chat_completions", model_reasoning=False,
        )

        async def invoke():
            async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False, timeout=1) as transport:
                with patch.object(socket, "getaddrinfo", side_effect=changing_dns), \
                     patch.object(guest_access, "get_personal_http_client", return_value=transport), \
                     patch("backend.token_usage.ensure_usage_allowed"), \
                     patch("backend.token_usage.record_response_usage"):
                    try:
                        await personal_model_client(provider)._request_json("/chat/completions", {}, retries=0)
                    except (RuntimeError, ValueError, httpx.HTTPError):
                        pass

        try:
            asyncio.run(invoke())
        finally:
            receiver.shutdown()
            receiver.server_close()
            worker.join(timeout=2)
        self.assertFalse(reached, f"Personal endpoint reached loopback after public DNS approval: {resolved}")

    def test_each_new_connection_revalidates_and_never_dials_private_dns(self):
        low_level = SimpleNamespace(connect_tcp=AsyncMock(return_value=SimpleNamespace()))
        backend = PublicNetworkBackend(low_level)
        answers = [
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443))]
            for value in ("93.184.216.34", "127.0.0.1", "10.0.0.8", "169.254.169.254")
        ]

        async def invoke():
            with patch.object(socket, "getaddrinfo", side_effect=answers):
                await backend.connect_tcp("changing.example", 443, timeout=1)
                for _ in range(3):
                    with self.assertRaises(httpcore.ConnectError):
                        await backend.connect_tcp("changing.example", 443, timeout=1)

        asyncio.run(invoke())
        self.assertEqual(low_level.connect_tcp.await_count, 1)
        self.assertEqual(low_level.connect_tcp.await_args.args, ("93.184.216.34", 443))

    def test_mixed_answers_multicast_and_ipv6_translation_are_rejected(self):
        for value in ("127.0.0.1", "224.0.0.1", "::1", "64:ff9b::7f00:1", "2002:7f00:1::"):
            with self.subTest(address=value), patch.object(socket, "getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET6 if ":" in value else socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443)),
            ]):
                with self.assertRaises(ValueError):
                    public_addresses("mixed.example", 443)

    def test_public_ip_is_pinned_while_https_host_sni_and_certificate_checks_remain(self):
        stream = RecordingStream()
        low_level = SimpleNamespace(connect_tcp=AsyncMock(return_value=stream))
        resolver = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        async def invoke():
            transport = PublicHTTPTransport(network_backend=PublicNetworkBackend(low_level))
            async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
                with patch.object(socket, "getaddrinfo", return_value=resolver) as resolve:
                    response = await client.post("https://public-model.example/v1/messages", json={"model": "fixture"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(resolve.call_count, 1)

        asyncio.run(invoke())
        self.assertEqual(low_level.connect_tcp.await_args.args, ("93.184.216.34", 443))
        self.assertEqual(stream.tls["server_hostname"], "public-model.example")
        self.assertEqual(stream.tls["ssl_context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(stream.tls["ssl_context"].check_hostname)
        self.assertIn(b"Host: public-model.example\r\n", b"".join(stream.sent))

    def test_personal_client_ignores_proxy_environment_and_never_follows_redirects(self):
        stream = RecordingStream(status=b"302 Found", extra=b"Location: http://127.0.0.1/admin\r\n")
        low_level = SimpleNamespace(connect_tcp=AsyncMock(return_value=stream))
        transport = PublicHTTPTransport(network_backend=PublicNetworkBackend(low_level))

        async def invoke():
            with patch.object(personal_network, "_client", None), \
                 patch.object(personal_network, "PublicHTTPTransport", return_value=transport), \
                 patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:8123", "HTTPS_PROXY": "http://127.0.0.1:8123"}), \
                 patch.object(socket, "getaddrinfo", return_value=[
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
                 ]):
                client = personal_network.get_personal_http_client()
                try:
                    response = await client.get("http://public-model.example/v1/models")
                finally:
                    await personal_network.aclose_personal_http_client()
                self.assertEqual(response.status_code, 302)
                self.assertFalse(client.follow_redirects)

        asyncio.run(invoke())
        self.assertEqual(low_level.connect_tcp.await_count, 1)
        self.assertEqual(low_level.connect_tcp.await_args.args, ("93.184.216.34", 80))

    def test_personal_json_stream_and_catalog_all_use_the_restricted_client_hook(self):
        requested = []

        def response(request):
            requested.append(request.url.path)
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "fixture"}]})
            if json.loads(request.content).get("stream"):
                data = 'data: {"choices":[{"delta":{"content":"visible answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                return httpx.Response(200, content=data, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "visible answer"}}]})

        provider = SimpleNamespace(
            id=9183, base_url="https://personal.example/v1", api_key="own-fixture-key",
            model_id="fixture", model_input='["text"]', wire_api="chat_completions", model_reasoning=False,
        )

        async def invoke():
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as restricted:
                with patch.object(guest_access, "get_personal_http_client", return_value=restricted), \
                     patch.object(guest_access, "validate_personal_model_url", return_value=provider.base_url), \
                     patch.object(client_module, "get_http_client", side_effect=AssertionError("unrestricted client selected")), \
                     patch("backend.token_usage.ensure_usage_allowed"), \
                     patch("backend.token_usage.record_response_usage"):
                    client = personal_model_client(provider)
                    await client._request_json("/chat/completions", {})
                    answer = await client.chat_messages_stream([{"role": "user", "content": "hello"}])
                    self.assertEqual(answer, "visible answer")
                    self.assertEqual(await client.list_models(), ["fixture"])

        asyncio.run(invoke())
        self.assertEqual(requested, ["/v1/chat/completions", "/v1/chat/completions", "/v1/models"])


class RecordingStream(httpcore.AsyncNetworkStream):
    def __init__(self, status=b"200 OK", extra=b""):
        self.sent = []
        self.tls = {}
        self.response = b"HTTP/1.1 " + status + b"\r\nContent-Length: 2\r\nConnection: close\r\n" + extra + b"\r\n{}"

    async def read(self, max_bytes, timeout=None):
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        self.sent.append(buffer)

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls = {"ssl_context": ssl_context, "server_hostname": server_hostname}
        return self

    def get_extra_info(self, info):
        return None


if __name__ == "__main__":
    unittest.main()
