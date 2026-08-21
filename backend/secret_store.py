"""数据库凭据的信封加密。

每个字段使用随机数据密钥（DEK）和 AES-256-GCM 加密；DEK 再由部署主密钥（KEK）
加密。数据库只保存密文信封，主密钥来自环境变量，不落库。旧明文可读，并由启动迁移
一次性转换，便于平滑升级。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.types import Text, TypeDecorator

PREFIX = "enc:v1:"
_KEY_AAD = b"agent-envelope-key:v1"
_VALUE_AAD = b"agent-envelope-value:v1"


class SecretStoreError(RuntimeError):
    pass


def _configured_keys() -> list[bytes]:
    # 独立主密钥优先；为已有部署提供以 JWT_SECRET 派生的兼容路径。
    primary = os.getenv("SECRET_MASTER_KEY") or os.getenv("JWT_SECRET") or ""
    previous = [
        item.strip()
        for item in os.getenv("SECRET_MASTER_KEY_PREVIOUS", "").split(",")
        if item.strip()
    ]
    if not primary:
        raise SecretStoreError("未配置 SECRET_MASTER_KEY（或兼容的 JWT_SECRET）")
    return [
        hashlib.sha256(b"agent-secret-store:v1\0" + item.encode("utf-8")).digest()
        for item in [primary, *previous]
    ]


def _kid(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_secret(value: str | None) -> str:
    plain = "" if value is None else str(value)
    if not plain or is_encrypted(plain):
        return plain
    master = _configured_keys()[0]
    data_key = secrets.token_bytes(32)
    key_nonce = secrets.token_bytes(12)
    value_nonce = secrets.token_bytes(12)
    envelope = {
        "kid": _kid(master),
        "kn": _b64(key_nonce),
        "ek": _b64(AESGCM(master).encrypt(key_nonce, data_key, _KEY_AAD)),
        "vn": _b64(value_nonce),
        "ct": _b64(
            AESGCM(data_key).encrypt(
                value_nonce, plain.encode("utf-8"), _VALUE_AAD
            )
        ),
    }
    encoded = _b64(
        json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return PREFIX + encoded


def decrypt_secret(value: str | None) -> str:
    stored = "" if value is None else str(value)
    if not is_encrypted(stored):
        return stored
    try:
        envelope = json.loads(_unb64(stored[len(PREFIX):]).decode("utf-8"))
        keys = _configured_keys()
        preferred = [key for key in keys if _kid(key) == envelope.get("kid")]
        candidates = preferred + [key for key in keys if key not in preferred]
        last_error = None
        for master in candidates:
            try:
                data_key = AESGCM(master).decrypt(
                    _unb64(envelope["kn"]), _unb64(envelope["ek"]), _KEY_AAD
                )
                plain = AESGCM(data_key).decrypt(
                    _unb64(envelope["vn"]), _unb64(envelope["ct"]), _VALUE_AAD
                )
                return plain.decode("utf-8")
            except Exception as exc:
                last_error = exc
        raise last_error or ValueError("没有可用主密钥")
    except Exception as exc:
        raise SecretStoreError("数据库凭据无法解密；请检查主密钥及历史轮换密钥") from exc


class EncryptedText(TypeDecorator):
    """ORM 透明加解密类型；数据库层始终看到密文。"""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_secret(value)

    def process_result_value(self, value, dialect):
        return decrypt_secret(value)


_SECRET_COLUMNS = {
    "model_providers": ("api_key", "auth_extra", "custom_headers"),
    "mcp_servers": ("headers",),
    "channels": ("token", "app_secret"),
    # 执行快照含提供商与 MCP 凭据，故任务载荷整体加密。
    "jobs": ("payload",),
}


def migrate_plaintext_secrets(bind) -> int:
    """把历史明文原位转换为密文；幂等且不经 ORM，避免双重加密。"""
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    table_columns = {
        table_name: {
            column["name"] for column in inspector.get_columns(table_name)
        }
        for table_name in _SECRET_COLUMNS
        if table_name in tables
    }
    changed = 0
    transaction_context = (
        contextlib.nullcontext(bind) if isinstance(bind, Connection) else bind.begin()
    )
    with transaction_context as connection:
        for table_name, columns in _SECRET_COLUMNS.items():
            if table_name not in tables:
                continue
            existing = table_columns[table_name]
            for column in columns:
                if column not in existing:
                    continue
                rows = connection.execute(
                    text(
                        f'SELECT id, "{column}" FROM "{table_name}" '
                        f'WHERE "{column}" IS NOT NULL AND "{column}" != :empty'
                    ),
                    {"empty": ""},
                ).all()
                for row_id, value in rows:
                    if is_encrypted(value):
                        continue
                    connection.execute(
                        text(
                            f'UPDATE "{table_name}" SET "{column}"=:value WHERE id=:id'
                        ),
                        {"value": encrypt_secret(str(value)), "id": row_id},
                    )
                    changed += 1
    return changed
