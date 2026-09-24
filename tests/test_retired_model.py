"""HTTP 410 is a configuration failure; transient provider errors still retry."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.llm.client import LLMClient


class RetiredModelTests(unittest.TestCase):
    def run_request(self, *, streaming, wire_api="chat_completions", failure_status=410, recover=False):
        self.requests = []
        client = LLMClient(
            base_url="https://example.com/v1", api_key="secret", model_id="retired-model",
            wire_api=wire_api, max_retries=3, stream_max_retries=3,
        )

        async def handler(request):
            self.requests.append(request)
            if not recover or len(self.requests) == 1:
                return httpx.Response(failure_status, json={"error": {"message": "Model endpoint retired"}})
            if streaming:
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    content='data: {"choices":[{"delta":{"content":"恢复成功"}}]}\n\ndata: [DONE]\n\n'.encode(),
                )
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "恢复成功"}}]})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as fake:
                with patch("backend.llm.client.get_http_client", return_value=fake), \
                     patch("backend.llm.client.asyncio.sleep", new=AsyncMock()) as sleep:
                    self.sleep = sleep
                    if streaming:
                        return await client.chat_messages_stream([{"role": "user", "content": "hello"}])
                    return (await client.chat_with_tools([{"role": "user", "content": "hello"}]))["content"]

        return asyncio.run(run())

    def test_retired_model_nonstream_fails_after_one_request_for_each_wire_api(self):
        for wire_api in ("chat_completions", "responses", "messages"):
            with self.subTest(wire_api=wire_api):
                with self.assertRaises(RuntimeError) as caught:
                    self.run_request(streaming=False, wire_api=wire_api)
                self.assertEqual(len(self.requests), 1)
                self.sleep.assert_not_called()
                message = str(caught.exception)
                self.assertIn("410", message)
                self.assertIn("下线", message)
                self.assertIn("重新识别", message)
                self.assertIn("模型配置", message)
                self.assertIn("Model endpoint retired", message)

    def test_retired_model_stream_fails_after_one_request_for_each_wire_api(self):
        for wire_api in ("chat_completions", "responses", "messages"):
            with self.subTest(wire_api=wire_api):
                with self.assertRaises(RuntimeError) as caught:
                    self.run_request(streaming=True, wire_api=wire_api)
                self.assertEqual(len(self.requests), 1)
                self.sleep.assert_not_called()
                self.assertIn("410", str(caught.exception))
                self.assertIn("模型配置", str(caught.exception))
                self.assertIn("Model endpoint retired", str(caught.exception))

    def test_transient_nonstream_provider_errors_still_retry_and_recover(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.assertEqual(self.run_request(streaming=False, failure_status=status, recover=True), "恢复成功")
                self.assertEqual(len(self.requests), 2)
                self.sleep.assert_awaited_once()

    def test_transient_stream_provider_errors_still_retry_and_recover(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.assertEqual(self.run_request(streaming=True, failure_status=status, recover=True), "恢复成功")
                self.assertEqual(len(self.requests), 2)
                self.sleep.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
