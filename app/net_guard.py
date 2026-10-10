"""Outbound connection guard for server-side requests to user-configured hosts.

A manager can point the Git integration at any host, so the server must not become a way to
reach its own network: cloud metadata endpoints, link-local and loopback services, or (unless
the deployment opts in) private ranges where an on-prem GitHub Enterprise might live.

The check runs when the socket is opened, against the address actually being connected to, so
a DNS answer that changes between a check and the connection (DNS rebinding) cannot slip past
it. When the environment routes the request through a proxy, the proxy resolves the name; the
host is then checked locally as a best effort before the request leaves.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.request
from typing import Optional

import httpcore
import httpx

# Never reachable, whatever the deployment allows: cloud metadata services and addresses with
# no legitimate use as a remote API.
_METADATA = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
    ipaddress.ip_network("fe80::/10"),
)
# Internal ranges: allowed only when the deployment opts in (an on-prem server).
_SHARED = ipaddress.ip_network("100.64.0.0/10")

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class HostNotAllowed(Exception):
    """The destination resolves to an address the server must not connect to."""


def address_problem(ip: Address, allow_private: bool) -> Optional[str]:
    """Why ``ip`` may not be connected to, or None when it may."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    elif isinstance(ip, ipaddress.IPv6Address) and ip.sixtofour is not None:
        ip = ip.sixtofour  # 2002:7f00:1:: reaches 127.0.0.1 through a 6to4 relay
    if any(ip in net for net in _METADATA if net.version == ip.version) or ip.is_link_local:
        return "a link-local or cloud metadata address"
    if ip.is_unspecified or ip.is_multicast:
        return "a reserved address"
    if ip.is_loopback or ip.is_private or (ip.version == 4 and ip in _SHARED):
        return None if allow_private else "a private network address"
    if ip.is_reserved or (not allow_private and not ip.is_global):
        return "a reserved address"
    return None


def _addresses(host: str, port: int) -> list[Address]:
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos))


def allowed_addresses(host: str, port: int, allow_private: bool) -> list[Address]:
    """Every address ``host`` resolves to, refusing the host if any of them is not allowed (a
    name with one internal answer is treated as internal)."""
    addresses = _addresses(host, port)
    for ip in addresses:
        problem = address_problem(ip, allow_private)
        if problem:
            raise HostNotAllowed(f"{host} resolves to {problem}")
    return addresses


class _GuardedBackend(httpcore.SyncBackend):
    def __init__(self, allow_private: bool) -> None:
        self._allow_private = allow_private

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = allowed_addresses(host, port, self._allow_private)
        except socket.gaierror as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        last: Optional[Exception] = None
        for ip in addresses:
            try:
                return super().connect_tcp(str(ip), port, timeout, local_address, socket_options)
            except httpcore.ConnectError as exc:
                last = exc
        raise last or httpcore.ConnectError(f"could not connect to {host}")


def guarded_transport(*, verify, allow_private: bool) -> httpx.HTTPTransport:
    """An httpx transport whose direct connections only reach allowed addresses."""
    transport = httpx.HTTPTransport(verify=verify)
    pool = transport._pool
    if not hasattr(pool, "_network_backend"):  # pragma: no cover - guards an httpcore upgrade
        raise RuntimeError("httpcore no longer exposes the connection pool's network backend")
    pool._network_backend = _GuardedBackend(allow_private)
    return transport


def _proxied(url: httpx.URL) -> bool:
    proxies = urllib.request.getproxies()
    if not (proxies.get(url.scheme) or proxies.get("all")):
        return False
    return not urllib.request.proxy_bypass(url.host)


def precheck(url: str, allow_private: bool) -> None:
    """Best-effort check for a request a proxy will resolve: refuse a host that resolves here
    to a disallowed address. A name this machine cannot resolve is left to the proxy."""
    parsed = httpx.URL(url)
    if not _proxied(parsed):
        return
    try:
        allowed_addresses(parsed.host, parsed.port or 443, allow_private)
    except socket.gaierror:
        return


__all__ = [
    "HostNotAllowed",
    "address_problem",
    "allowed_addresses",
    "guarded_transport",
    "precheck",
]
