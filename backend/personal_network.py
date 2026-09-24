"""Personal model traffic connects only to IPs approved at socket creation.

HTTP URLs retain their hostname for Host headers, TLS verification and origin
pooling. Only the network backend sees the pinned numeric destination. Personal
traffic never uses environment proxies or the platform private-network bypass.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


_TRANSITION_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32",
))


def public_addresses(host: str, port: int) -> list[str]:
    """Resolve once and reject the entire answer if any address is non-public."""
    if "%" in host:
        raise ValueError("个人模型接口不允许局域网范围地址")
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = []
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if (not address.is_global or address.is_multicast or address.is_reserved
                or address.is_loopback or address.is_link_local or address.is_unspecified
                or any(address in network for network in _TRANSITION_NETWORKS)):
            raise ValueError("个人模型接口只能连接公网地址")
        value = str(address)
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise ValueError("个人模型接口没有可用公网地址")
    return addresses


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend=None):
        self._backend = backend or AutoBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # DNS resolution is part of the connect budget. Revalidate on *every*
        # new socket, including retries after a previously valid resolution.
        budget = 10.0 if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + budget
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(public_addresses, host, port), timeout=budget,
            )
        except asyncio.TimeoutError as exc:
            raise httpcore.ConnectTimeout("个人模型接口解析超时") from exc
        except (ValueError, OSError) as exc:
            raise httpcore.ConnectError("个人模型接口必须解析为公网地址") from exc
        last_error = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpcore.ConnectTimeout("个人模型接口连接超时")
            try:
                return await self._backend.connect_tcp(
                    address, port, timeout=remaining,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError("个人模型接口连接失败")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("个人模型接口不允许本地套接字")

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *, network_backend=None):
        # HTTPX 0.28.1 delegates streaming/error translation/closing to _pool;
        # HTTPCore 1.0.9 exposes network_backend on this constructor. Both are
        # locked dependencies. Keep this adapter covered by real HTTPX tests.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=30, max_keepalive_connections=10, keepalive_expiry=5,
            network_backend=network_backend or PublicNetworkBackend(),
            retries=0,
        )


_client: httpx.AsyncClient | None = None


def get_personal_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            transport=PublicHTTPTransport(), trust_env=False,
            follow_redirects=False, timeout=120,
        )
    return _client


async def aclose_personal_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
