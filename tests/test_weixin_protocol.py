"""Offline iLink 2.4.9 protocol fixtures; never contact Tencent or a model."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import weixin_channel as weixin
from backend.database import Base
from backend.models import Agent, Channel, User
from backend.weixin_quote_cache import WeixinQuoteCache, binding_scope, message_identifier, resolve_partial_quote


def text_message(text, message_id="100", *, sender="sender", reference=None):
    item = {"type": 1, "text_item": {"text": text}}
    if reference is not None:
        item["ref_msg"] = reference
    return {"from_user_id": sender, "message_type": 1, "message_id": message_id,
            "context_token": "opaque-context", "item_list": [item]}


class WeixinProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache_path = Path(self.temp.name) / "quotes.sqlite3"
        self.cache = WeixinQuoteCache(self.cache_path)
        self.manager = weixin.WeixinChannelManager(quote_cache=self.cache)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        with self.factory() as db:
            owner = User(username="wx-owner", role="user", password_hash="fixture", permissions='["channels"]')
            other = User(username="wx-other", role="user", password_hash="fixture")
            db.add_all([owner, other])
            db.flush()
            agent = Agent(name="wx-agent", created_by=owner.id, enabled=True)
            db.add(agent)
            db.flush()
            channel = Channel(name="wx", type=weixin.CHANNEL_TYPE, path_key="path-fixture",
                              agent_id=agent.id, created_by=owner.id, account_id="bot",
                              account_user_id="sender", token="bot-token", enabled=True,
                              connection_status="connected", base_url=weixin.DEFAULT_BASE_URL)
            second = Channel(name="other", type=weixin.CHANNEL_TYPE, path_key="other-path",
                             agent_id=agent.id, created_by=other.id, account_id="other-bot",
                             account_user_id="other-sender", token="other-token", enabled=True,
                             connection_status="connected", base_url=weixin.DEFAULT_BASE_URL)
            db.add_all([channel, second])
            db.commit()
            self.channel_id, self.other_id = channel.id, second.id
            self.owner_id, self.agent_id = owner.id, agent.id
            self.scope = binding_scope(channel)
        patcher = patch.object(weixin, "SessionLocal", self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def prepare(self, message, token="bot-token"):
        return self.manager._prepare_inbound(self.channel_id, message, token)

    def test_real_json_parser_keeps_uint64_ids_and_sends_current_protocol_headers(self):
        identifier = 18446744073709551615
        seen = []

        def upstream(request):
            seen.append(request)
            return httpx.Response(200, content=json.dumps({"ret": 0, "msgs": [{
                "message_id": identifier, "item_list": [{"msg_id": identifier - 1,
                "ref_msg": {"svr_id": identifier - 2}, "text_item": {"text": '"message_id": 123'}}],
            }]}))

        async def invoke():
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                self.manager._client = client
                return await self.manager._request_json("POST", weixin.DEFAULT_BASE_URL + "/ilink/bot/getupdates",
                    token="bot-token", body={"get_updates_buf": "unchanged", "base_info": weixin._base_info()})

        result = asyncio.run(invoke())
        message = result["msgs"][0]
        self.assertEqual(weixin._message_id(message), str(identifier))
        self.assertEqual(message["item_list"][0]["ref_msg"]["svr_id"], identifier - 2)
        self.assertEqual(message["item_list"][0]["text_item"]["text"], '"message_id": 123')
        self.assertEqual(seen[0].headers["iLink-App-ClientVersion"], str((2 << 16) | (4 << 8) | 9))
        self.assertEqual(seen[0].headers["Authorization"], "Bearer bot-token")
        self.assertEqual(json.loads(seen[0].content)["base_info"]["channel_version"], "2.4.9")

    def test_item_message_id_fallback_and_no_float_rounding(self):
        message = {"item_list": [{"msg_id": 9007199254740993}]}
        self.assertEqual(weixin._message_id(message), "9007199254740993")
        for invalid in (True, 1.2, 2 ** 64, "-1", "1e20"):
            self.assertEqual(message_identifier(invalid), "")

    def test_inline_quote_compatibility_and_media_boundary(self):
        query, _ = self.prepare(text_message("解释一下", reference={"title": "摘要", "message_item": {
            "type": 1, "text_item": {"text": "旧格式引用"}}}))
        self.assertIn("摘要 | 旧格式引用", query)
        query, _ = self.prepare(text_message("解释图片", reference={"message_item": {
            "type": 2, "image_item": {"media": {"full_url": "https://never-request.example"}}}}))
        self.assertIn("引用的图片尚未读取", query)
        self.assertNotIn("never-request", query)

    def test_id_only_quote_survives_manager_restart(self):
        self.prepare(text_message("这是上一条文字", 18446744073709551615))
        self.manager = weixin.WeixinChannelManager(quote_cache=WeixinQuoteCache(self.cache_path))
        query, _ = self.prepare(text_message("解释一下", "101", reference={"svr_id": "18446744073709551615"}))
        self.assertIn("这是上一条文字", query)

    def test_partial_quote_hash_matches_occurrences_and_preserves_unicode(self):
        full = "前言 起😀点结束，另一段起😀点结束，尾声"
        selected = "起😀点结束"
        partial = {"start": "起", "end": "结束", "startindex": 1, "endindex": 1,
                   "quotemd5": hashlib.md5(selected.encode()).hexdigest()}
        self.assertEqual(resolve_partial_quote(full, partial), selected)
        relative = {**partial, "endindex": 0}
        self.assertEqual(resolve_partial_quote(full, relative), selected)
        self.prepare(text_message(full, "100"))
        query, _ = self.prepare(text_message("只解释所选部分", "101", reference={"svr_id": "100", "partial_text": partial}))
        self.assertTrue(query.endswith(selected))
        self.assertNotIn("前言", query)

    def test_invalid_partial_falls_back_to_full_cached_text(self):
        self.prepare(text_message("完整引用内容", "100"))
        query, _ = self.prepare(text_message("解释", "101", reference={"svr_id": "100", "partial_text": {
            "start": "完整", "end": "内容", "startindex": 0, "endindex": 0, "quotemd5": "wrong"}}))
        self.assertTrue(query.endswith("完整引用内容"))
        self.assertIsNone(resolve_partial_quote("text", {"start": "t", "end": "t", "startindex": 10**9, "endindex": 0}))

    def test_unknown_reference_is_explicitly_unavailable(self):
        query, _ = self.prepare(text_message("解释", reference={"svr_id": "999"}))
        self.assertIn("引用消息内容未缓存", query)

    def test_quote_never_crosses_channel_sender_or_rotated_credentials(self):
        self.prepare(text_message("仅第一个账号可见", "100"))
        query, _ = self.manager._prepare_inbound(self.other_id,
            text_message("解释", "101", sender="other-sender", reference={"svr_id": "100"}), "other-token")
        self.assertNotIn("仅第一个账号可见", query)
        self.assertEqual(self.prepare(text_message("偷看", sender="other-sender", reference={"svr_id": "100"})), ("", ""))
        with self.factory() as db:
            db.get(Channel, self.channel_id).token = "new-token"
            db.commit()
        self.assertEqual(self.prepare(text_message("old delivery")), ("", ""))
        query, _ = self.prepare(text_message("解释", "101", reference={"svr_id": "100"}), token="new-token")
        self.assertNotIn("仅第一个账号可见", query)

    def test_outbound_server_id_becomes_reference_and_context_is_unchanged(self):
        send = AsyncMock(return_value={"ret": 0, "message_id": 18446744073709551615})
        with patch.object(self.manager, "_request_json", send):
            asyncio.run(self.manager._send_text(self.channel_id, "sender", "已发送的答复", "exact-context", "run-id"))
        body = send.await_args.kwargs["body"]
        self.assertEqual(body["msg"]["context_token"], "exact-context")
        self.assertEqual(body["msg"]["run_id"], "run-id")
        self.assertEqual(body["msg"]["message_state"], 2)
        query, _ = self.prepare(text_message("继续", "101", reference={"svr_id": "18446744073709551615"}))
        self.assertIn("已发送的答复", query)

    def test_failed_outbound_is_not_cached(self):
        with patch.object(self.manager, "_request_json", AsyncMock(return_value={"ret": -1, "message_id": "100"})):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.manager._send_text(self.channel_id, "sender", "未发送内容", "context"))
        self.assertIsNone(self.cache.find(self.scope, "sender", "100"))
        with self.factory() as db:
            self.assertIsNone(db.get(Channel, self.channel_id).last_outbound_at)

    def test_rebinding_even_same_identity_clears_old_quotes(self):
        self.prepare(text_message("旧绑定内容", "100"))
        session = SimpleNamespace(channel_id=self.channel_id, owner_id=self.owner_id)
        self.manager._save_connected(session, "bot-token", "bot", "sender", weixin.DEFAULT_BASE_URL)
        self.assertIsNone(self.cache.find(self.scope, "sender", "100"))
        with self.factory() as db:
            self.assertEqual(db.get(Channel, self.channel_id).token, "bot-token")

    def test_disconnect_clears_quotes_and_blocks_an_inflight_reply(self):
        self.prepare(text_message("原消息", "100"))
        asyncio.run(self.manager.disconnect(self.channel_id, self.owner_id))
        self.assertIsNone(self.cache.find(self.scope, "sender", "100"))
        send = AsyncMock()
        with patch.object(self.manager, "_request_json", send):
            asyncio.run(self.manager._send_text(self.channel_id, "sender", "旧任务答复", "context", expected_binding=self.scope))
        send.assert_not_called()

    def test_wrong_recipient_and_replaced_binding_never_receive_old_reply(self):
        with self.factory() as db:
            db.get(Channel, self.channel_id).token = "new-token"
            db.commit()
        send = AsyncMock()
        with patch.object(self.manager, "_request_json", send):
            asyncio.run(self.manager._send_text(self.channel_id, "sender", "old", "context", expected_binding=self.scope))
            asyncio.run(self.manager._send_text(self.channel_id, "wrong-sender", "old", "context"))
        send.assert_not_called()

    def test_replayed_item_id_preserves_queue_idempotency_key(self):
        message = text_message("query")
        message.pop("message_id")
        message["item_list"][0]["msg_id"] = 9007199254740993
        from backend.api import chat
        keys = []

        def enqueue(*args, **kwargs):
            keys.append(kwargs["idempotency_key"])
            return "existing-job"

        with patch.object(chat, "select_chat_provider", return_value=None), \
             patch.object(chat, "build_execution_snapshot", return_value={}), \
             patch.object(weixin.jobs, "enqueue_in_session", side_effect=enqueue), \
             patch.object(weixin.jobs, "view", return_value=SimpleNamespace(status=weixin.jobs.DONE, result={"answer": "ok"})), \
             patch.object(self.manager, "_send_text", AsyncMock()):
            asyncio.run(self.manager._handle_message(self.channel_id, message, "query", self.scope))
            asyncio.run(self.manager._handle_message(self.channel_id, message, "query", self.scope))
        with self.factory() as db:
            expected = weixin._dispatch_key(db.get(Channel, self.channel_id), "9007199254740993", self.scope)
        self.assertEqual(keys, [expected] * 2)

    def test_corrupt_cache_does_not_block_inbound_or_outbound(self):
        self.cache_path.write_bytes(b"not a sqlite database")
        with self.assertLogs("backend.weixin_quote_cache", level="WARNING") as logs:
            query, _ = self.prepare(text_message("secret-test-text", reference={"svr_id": "100"}))
            with patch.object(self.manager, "_request_json", AsyncMock(return_value={"ret": 0, "message_id": "101"})) as send:
                asyncio.run(self.manager._send_text(self.channel_id, "sender", "secret-test-text", "context"))
        self.assertTrue(query.startswith("secret-test-text"))
        send.assert_awaited_once()
        self.assertNotIn("secret-test-text", " ".join(logs.output))
        self.assertNotIn("bot-token", " ".join(logs.output))

    def test_reset_failure_disables_quotes_until_clear_succeeds(self):
        self.prepare(text_message("old", "100"))
        with patch.object(self.cache, "clear_channel", return_value=False), \
             patch.object(self.cache, "find") as find:
            self.manager._reset_quotes(self.channel_id)
            query, _ = self.prepare(text_message("query", "101", reference={"svr_id": "100"}))
        find.assert_not_called()
        self.assertIn("未缓存", query)
        query, _ = self.prepare(text_message("query", "101", reference={"svr_id": "100"}))
        self.assertIn("未缓存", query)
        self.assertNotIn(self.channel_id, self.manager._quote_reset_pending)

    def test_idempotency_scope_reuses_only_proven_same_binding(self):
        legacy = f"weixin:{self.channel_id}:100"
        with self.factory() as db:
            channel = db.get(Channel, self.channel_id)
            same = weixin._dispatch_key(channel, "100", self.scope)
            self.assertEqual(weixin._dispatch_key(channel, "100", self.scope), same)
            different = weixin._dispatch_key(channel, "100", "different-binding")
            self.assertNotEqual(different, same)
            self.assertLessEqual(len(different), 128)
            self.assertNotEqual(same, legacy)
            self.assertNotEqual(weixin._dispatch_key(channel, "101", self.scope), same)
            self.assertNotEqual(weixin._dispatch_key(channel, "long" * 100 + "one", self.scope),
                                weixin._dispatch_key(channel, "long" * 100 + "two", self.scope))
            channel.account_user_id = "other-sender"
            self.assertNotEqual(weixin._dispatch_key(channel, "100", self.scope), same)

    def test_thread_hash_is_stable_within_binding_and_separate_after_rebinding(self):
        original = weixin._session_id(self.channel_id, "sender", self.agent_id, self.scope)
        self.assertEqual(original, weixin._session_id(self.channel_id, "sender", self.agent_id, self.scope))
        self.assertNotEqual(original, weixin._session_id(self.channel_id, "sender", self.agent_id, "new-binding"))
        self.assertNotEqual(original, weixin._session_id(self.channel_id, "sender", self.agent_id))

    def test_old_poll_cannot_overwrite_new_bindings_sync_cursor(self):
        async def upstream(*args, **kwargs):
            with self.factory() as db:
                row = db.get(Channel, self.channel_id)
                row.token = "new-token"
                row.sync_buf = "new-cursor"
                db.commit()
            return {"ret": 0, "get_updates_buf": "old-cursor", "msgs": [text_message("old-account-text")]}

        with patch.object(self.manager, "_request_json", side_effect=upstream), \
             patch.object(self.manager, "_handle_message", AsyncMock()) as handle:
            asyncio.run(self.manager._monitor(self.channel_id))
        handle.assert_not_called()
        with self.factory() as db:
            self.assertEqual(db.get(Channel, self.channel_id).sync_buf, "new-cursor")

    def test_old_poll_error_cannot_reconnect_disconnected_channel(self):
        async def upstream(*args, **kwargs):
            with self.factory() as db:
                row = db.get(Channel, self.channel_id)
                row.connection_status = "unbound"
                row.token = ""
                row.last_error = "explicit disconnect"
                db.commit()
            self.manager._stopping = True
            raise RuntimeError("late old request failed")

        with patch.object(self.manager, "_request_json", side_effect=upstream), \
             patch.object(weixin.asyncio, "sleep", AsyncMock()):
            asyncio.run(self.manager._monitor(self.channel_id))
        with self.factory() as db:
            row = db.get(Channel, self.channel_id)
            self.assertEqual(row.connection_status, "unbound")
            self.assertEqual(row.last_error, "explicit disconnect")

    def test_cancelled_old_monitor_callback_does_not_remove_replacement(self):
        async def monitor(_channel_id):
            await asyncio.Event().wait()

        async def invoke():
            with patch.object(self.manager, "_monitor", side_effect=monitor):
                self.manager.start_monitor(self.channel_id)
                old = self.manager._monitors[self.channel_id]
                self.manager.stop_monitor(self.channel_id)
                self.manager.start_monitor(self.channel_id)
                replacement = self.manager._monitors[self.channel_id]
                await asyncio.gather(old, return_exceptions=True)
                self.assertIs(self.manager._monitors[self.channel_id], replacement)
                self.manager.stop_monitor(self.channel_id)
                await asyncio.gather(replacement, return_exceptions=True)

        asyncio.run(invoke())


class WeixinQuoteStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "quotes.sqlite3"

    def test_encrypted_text_ttl_count_and_text_limits(self):
        cache = WeixinQuoteCache(self.path, retention_seconds=10, max_messages=2, max_text_chars=8)
        for identifier, now in (("1", 100), ("2", 101), ("3", 102)):
            with patch("backend.weixin_quote_cache.time.time", return_value=now):
                self.assertTrue(cache.put("scope", "sender", identifier, "机密文本-very-long", channel_id=1))
        with patch("backend.weixin_quote_cache.time.time", return_value=102):
            self.assertIsNone(cache.find("scope", "sender", "1"))
            self.assertEqual(cache.find("scope", "sender", "3"), "机密文本-ver")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT body FROM quotes").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[0].startswith("enc:v1:") and "机密文本" not in row[0] for row in rows))
        with patch("backend.weixin_quote_cache.time.time", return_value=113):
            self.assertIsNone(cache.find("scope", "sender", "3"))
            cache.put("other", "sender", "4", "new", channel_id=2)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 1)

    def test_locked_cache_returns_without_failing_delivery(self):
        cache = WeixinQuoteCache(self.path)
        cache.put("scope", "sender", "1", "before", channel_id=1)
        with closing(sqlite3.connect(self.path)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertLogs("backend.weixin_quote_cache", level="WARNING"):
                self.assertFalse(cache.put("scope", "sender", "2", "during", channel_id=1))
            self.assertEqual(cache.find("scope", "sender", "1"), "before")

    def test_concurrent_writes_keep_account_bound_and_never_cross_scope(self):
        cache = WeixinQuoteCache(self.path, max_messages=3)
        cache.put("first", "sender", "1", "first", channel_id=1)

        def write(number):
            scope = "first" if number % 2 else "second"
            return cache.put(scope, "sender", str(number + 10), scope, channel_id=1 if number % 2 else 2)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(write, range(20)))
        self.assertTrue(any(results))
        with closing(sqlite3.connect(self.path)) as db:
            groups = db.execute("SELECT scope, COUNT(*) FROM quotes GROUP BY scope").fetchall()
            rows = db.execute("SELECT scope, message_id FROM quotes").fetchall()
        self.assertTrue(all(count <= 3 for _, count in groups))
        for scope, identifier in rows:
            self.assertEqual(cache.find(scope, "sender", identifier), scope)
            self.assertIsNone(cache.find(scope, "wrong-sender", identifier))


if __name__ == "__main__":
    unittest.main()
