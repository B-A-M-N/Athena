"""DNS-pinned HTTP transports.

Policy checks that resolve a hostname and then hand the hostname to a normal
HTTP client still have a DNS-rebinding window: the client may resolve the
name again at connect time. These transports preserve the original hostname
for HTTP Host/SNI while forcing the socket connection to one of the already
approved addresses.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable


def resolve_addresses(
    host: str,
    port: int,
    *,
    resolver: Callable[..., Iterable[tuple]] = socket.getaddrinfo,
) -> tuple[str, ...]:
    """Resolve stable, de-duplicated socket addresses for a host."""
    try:
        literal = socket.inet_pton(socket.AF_INET, host)
    except OSError:
        literal = None
    if literal is not None:
        return (host,)
    try:
        literal = socket.inet_pton(socket.AF_INET6, host)
    except OSError:
        literal = None
    if literal is not None:
        return (host,)
    infos = resolver(host, port, type=socket.SOCK_STREAM)
    addresses = {str(info[4][0]) for info in infos if info and info[4]}
    return tuple(sorted(addresses))


def pinned_sync_transport(host: str, addresses: Iterable[str]):
    """Build an httpx sync transport that connects ``host`` by pinned IP."""
    import httpcore
    import httpx

    pinned = _address_map(host, addresses)

    class _PinnedBackend(httpcore.SyncBackend):
        def connect_tcp(self, target_host, port, timeout=None, local_address=None,
                        socket_options=None):
            target = pinned.get(_normalize_host(target_host), target_host)
            return super().connect_tcp(
                target, port, timeout, local_address, socket_options
            )

    transport = httpx.HTTPTransport(
        verify=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )
    ssl_context = getattr(transport._pool, "_ssl_context", None)
    transport._pool = httpcore.ConnectionPool(
        ssl_context=ssl_context,
        max_connections=1,
        max_keepalive_connections=0,
        network_backend=_PinnedBackend(),
    )
    return transport


def pinned_async_transport(host: str, addresses: Iterable[str]):
    """Build an httpx async transport that connects ``host`` by pinned IP."""
    import httpcore
    import httpx

    pinned = _address_map(host, addresses)

    class _PinnedBackend(httpcore.AnyIOBackend):
        async def connect_tcp(self, target_host, port, timeout=None,
                              local_address=None, socket_options=None):
            target = pinned.get(_normalize_host(target_host), target_host)
            return await super().connect_tcp(
                target, port, timeout, local_address, socket_options
            )

    transport = httpx.AsyncHTTPTransport(
        verify=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )
    ssl_context = getattr(transport._pool, "_ssl_context", None)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl_context,
        max_connections=1,
        max_keepalive_connections=0,
        network_backend=_PinnedBackend(),
    )
    return transport


def _normalize_host(host: str | bytes) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    return str(host).strip().lower().rstrip(".")


def _address_map(host: str, addresses: Iterable[str]) -> dict[str, str]:
    values = tuple(str(address).strip() for address in addresses if str(address).strip())
    if not values:
        raise ValueError(f"no resolved addresses for {host}")
    return {_normalize_host(host): values[0]}


__all__ = ["pinned_async_transport", "pinned_sync_transport", "resolve_addresses"]
