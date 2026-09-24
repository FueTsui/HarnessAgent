"""Bounded, encrypted text-only iLink quote cache, independent of the task DB.

A cache failure must never fail normal message delivery. Keys include the
platform owner, channel incarnation, bot identity, credential and sender, so
an upstream message ID alone can never cross a binding boundary.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
from contextlib import closing

from .secret_store import SecretStoreError, decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)
RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_MESSAGES = 10_000
MAX_TEXT_CHARS = 16_000


def binding_scope(channel) -> str:
    values = [channel.created_by, channel.id, channel.path_key, channel.account_id,
              channel.account_user_id, channel.token]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def message_identifier(value) -> str:
    # Python's JSON decoder preserves uint64 integers. Never round through float.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return ""
    text = str(value).strip()
    if not text or len(text) > 20 or not text.isascii() or not text.isdigit():
        return ""
    number = int(text)
    return str(number) if 0 < number <= (1 << 64) - 1 else ""


def resolve_partial_quote(full_text: str, partial) -> str | None:
    """Match the two iLink occurrence-index conventions; MD5 disambiguates them."""
    if not isinstance(partial, dict):
        return None
    start, end = partial.get("start"), partial.get("end")
    first, last = partial.get("startindex"), partial.get("endindex")
    if (not isinstance(start, str) or not start or not isinstance(end, str) or not end
            or type(first) is not int or type(last) is not int
            or not 0 <= first <= len(full_text) or not 0 <= last <= len(full_text)):
        return None

    def nth(value, count, offset=0):
        for _ in range(count + 1):
            found = full_text.find(value, offset)
            if found < 0:
                return -1
            offset = found + len(value)
        return found

    begin = nth(start, first)
    if begin < 0:
        return None
    candidates = []
    for offset in (0, begin + len(start)):
        finish = nth(end, last, offset)
        if finish >= begin:
            candidate = full_text[begin:finish + len(end)]
            if candidate not in candidates:
                candidates.append(candidate)
    expected = partial.get("quotemd5") or ""
    if not isinstance(expected, str):
        return None
    for candidate in candidates:
        if not expected or hashlib.md5(candidate.encode("utf-8"), usedforsecurity=False).hexdigest() == expected.lower():
            return candidate
    return None


class WeixinQuoteCache:
    def __init__(self, path: Path, *, retention_seconds=RETENTION_SECONDS,
                 max_messages=MAX_MESSAGES, max_text_chars=MAX_TEXT_CHARS):
        self.path = Path(path)
        self.retention_seconds = max(1, int(retention_seconds))
        self.max_messages = max(1, int(max_messages))
        self.max_text_chars = max(1, int(max_text_chars))

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=0.05)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS quotes (
                scope TEXT NOT NULL, sender TEXT NOT NULL, message_id TEXT NOT NULL,
                channel_id INTEGER NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (scope, sender, message_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS ix_quotes_expiry ON quotes(created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_quotes_channel ON quotes(channel_id)")
            return db
        except Exception:
            db.close()
            raise

    def put(self, scope: str, sender: str, message_id, body: str, *, channel_id: int) -> bool:
        identifier = message_identifier(message_id)
        if not identifier or not body or not scope or not sender:
            return False
        try:
            # Wrap before encryption so user text resembling an envelope cannot
            # be mistaken for already encrypted storage by encrypt_secret().
            encrypted = encrypt_secret(json.dumps({"text": body[:self.max_text_chars]}, ensure_ascii=False))
            now = time.time()
            with closing(self._open()) as db, db:
                db.execute("DELETE FROM quotes WHERE created_at < ?", (now - self.retention_seconds,))
                db.execute("""INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope, sender, message_id) DO UPDATE SET
                    body=excluded.body, created_at=excluded.created_at""",
                           (scope, sender, identifier, channel_id, encrypted, now))
                db.execute("""DELETE FROM quotes WHERE scope=? AND rowid NOT IN
                    (SELECT rowid FROM quotes WHERE scope=? ORDER BY created_at DESC, rowid DESC LIMIT ?)""",
                           (scope, scope, self.max_messages))
            return True
        except (sqlite3.Error, OSError, SecretStoreError, ValueError, TypeError):
            logger.warning("微信引用缓存写入不可用；继续正常消息处理")
            return False

    def find(self, scope: str, sender: str, message_id) -> str | None:
        identifier = message_identifier(message_id)
        if not identifier or not self.path.exists():
            return None
        try:
            with closing(self._open()) as db:
                row = db.execute("""SELECT body FROM quotes WHERE scope=? AND sender=?
                    AND message_id=? AND created_at>=?""",
                    (scope, sender, identifier, time.time() - self.retention_seconds)).fetchone()
            return json.loads(decrypt_secret(row[0]))["text"] if row else None
        except (sqlite3.Error, OSError, SecretStoreError, ValueError, TypeError, KeyError):
            logger.warning("微信引用缓存读取不可用；引用内容按未缓存处理")
            return None

    def clear_channel(self, channel_id: int) -> bool:
        if not self.path.exists():
            return True
        try:
            with closing(self._open()) as db, db:
                db.execute("DELETE FROM quotes WHERE channel_id=?", (channel_id,))
            return True
        except (sqlite3.Error, OSError):
            logger.warning("微信引用缓存清理不可用；不影响绑定状态更新")
            return False
