"""出站请求安全校验（SSRF 防护）。

管理员可填写的外联 URL（MCP 服务、模型提供商接口）由服务端发起请求，
若不加限制可被用于探测内网或访问云元数据端点（169.254.169.254 等）。

默认策略：拒绝目标解析到 环回 / 私网 / 链路本地 / 多播 / 保留 / 未指定 网段。
- 可信内网部署：设 SSRF_ALLOW_PRIVATE=true 全量放行；
- 精确放行：SSRF_ALLOWLIST 填逗号分隔的 host 或 CIDR（如 "10.0.0.5,192.168.1.0/24,gw.local"）。

注意：本校验在请求前解析 DNS 并检查所有解析结果，可挡掉直接指向内网的 URL；
对 DNS rebinding（解析后改绑）只能做尽力而为的防护——真正强隔离需在网络层做出站策略。
"""
import ipaddress
import socket
from urllib.parse import urlparse

from .config import settings


class OutboundBlocked(ValueError):
    """目标 URL 命中受限网段或使用了非法 scheme。"""


def _allowlist() -> tuple[set[str], list]:
    hosts: set[str] = set()
    nets: list = []
    for raw in (settings.SSRF_ALLOWLIST or "").split(","):
        entry = raw.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            hosts.add(entry.lower())
    return hosts, nets


def _ip_blocked(addr: ipaddress._BaseAddress) -> bool:
    return bool(
        addr.is_loopback or addr.is_private or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def validate_outbound_url(url: str) -> None:
    """校验外联 URL；不合规抛 OutboundBlocked。SSRF_ALLOW_PRIVATE=true 时整体跳过。"""
    if settings.SSRF_ALLOW_PRIVATE:
        return
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise OutboundBlocked(f"仅允许 http/https 协议，收到：{parsed.scheme or '（空）'}")
    host = parsed.hostname
    if not host:
        raise OutboundBlocked("URL 缺少主机名")

    allow_hosts, allow_nets = _allowlist()
    if host.lower() in allow_hosts:
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OutboundBlocked(f"无法解析主机 {host}：{exc}")

    blocked: set[str] = set()
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr in net for net in allow_nets):
            continue
        if _ip_blocked(addr):
            blocked.add(ip)
    if blocked:
        raise OutboundBlocked(
            f"目标 {host} 解析到受限网段 {', '.join(sorted(blocked))}"
            "（环回/内网/链路本地/保留）。如确为可信内网服务，"
            "请设 SSRF_ALLOW_PRIVATE=true 或将其加入 SSRF_ALLOWLIST。"
        )
