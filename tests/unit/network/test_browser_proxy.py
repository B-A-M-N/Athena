from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from athena.network.browser_proxy import BrowserProxyConfig, DNSPinnedBrowserProxy


class _Writer:
    def __init__(self) -> None:
        self.data: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        self.data.append(value)

    async def drain(self) -> None:
        return None

    def write_eof(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _OverrunReader:
    async def readuntil(self, _separator):
        raise asyncio.LimitOverrunError("header delimiter not found", consumed=65_536)


def test_proxy_limits_are_operator_bounded():
    proxy = DNSPinnedBrowserProxy(
        BrowserProxyConfig(
            max_concurrent_tunnels=4,
            idle_timeout_seconds=1,
            max_tunnel_seconds=2,
        )
    )
    assert proxy.config.max_concurrent_tunnels == 4
    with pytest.raises(ValueError):
        BrowserProxyConfig(max_concurrent_tunnels=0)


@pytest.mark.asyncio
async def test_connect_uses_validated_dns_address(monkeypatch):
    proxy = DNSPinnedBrowserProxy()
    client_reader = asyncio.StreamReader()
    client_reader.feed_data(b"CONNECT public.example.test:443 HTTP/1.1\r\n\r\n")
    client_reader.feed_eof()
    client_writer = _Writer()
    upstream_reader = asyncio.StreamReader()
    upstream_reader.feed_eof()
    upstream_writer = _Writer()
    calls: list[tuple[str, int]] = []

    def fake_validate(url, policy):
        assert url == "https://public.example.test:443"
        assert policy == "allow"
        return SimpleNamespace(addresses=("93.184.216.34",)), None

    async def fake_open_connection(host, port):
        calls.append((host, port))
        return upstream_reader, upstream_writer

    monkeypatch.setattr("athena.network.browser_proxy.validate_target", fake_validate)
    monkeypatch.setattr(
        "athena.network.browser_proxy.asyncio.open_connection", fake_open_connection
    )

    await proxy._handle(client_reader, client_writer)

    assert calls == [("93.184.216.34", 443)]
    assert b"200 Connection Established" in b"".join(client_writer.data)


@pytest.mark.asyncio
async def test_absolute_http_request_uses_validated_dns_address(monkeypatch):
    proxy = DNSPinnedBrowserProxy()
    client_reader = asyncio.StreamReader()
    client_reader.feed_data(
        b"GET http://public.example.test/path?q=1 HTTP/1.1\r\n"
        b"Proxy-Authorization: secret\r\nX-Test: yes\r\n\r\n"
    )
    client_reader.feed_eof()
    client_writer = _Writer()
    upstream_reader = asyncio.StreamReader()
    upstream_reader.feed_data(b"HTTP/1.1 200 OK\r\n\r\n")
    upstream_reader.feed_eof()
    upstream_writer = _Writer()
    calls: list[tuple[str, int]] = []

    def fake_validate(url, policy):
        assert url == "http://public.example.test/path?q=1"
        assert policy == "allow"
        return SimpleNamespace(addresses=("93.184.216.34",)), None

    async def fake_open_connection(host, port):
        calls.append((host, port))
        return upstream_reader, upstream_writer

    monkeypatch.setattr("athena.network.browser_proxy.validate_target", fake_validate)
    monkeypatch.setattr(
        "athena.network.browser_proxy.asyncio.open_connection", fake_open_connection
    )

    await proxy._handle(client_reader, client_writer)

    assert calls == [("93.184.216.34", 80)]
    outgoing = b"".join(upstream_writer.data)
    assert outgoing.startswith(b"GET /path?q=1 HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in outgoing
    assert b"X-Test: yes" in outgoing


@pytest.mark.asyncio
async def test_ipv6_connect_uses_bracketed_validation_and_pinned_address(monkeypatch):
    proxy = DNSPinnedBrowserProxy()
    reader = asyncio.StreamReader()
    reader.feed_data(b"CONNECT [2001:db8::1]:8443 HTTP/1.1\r\n\r\n")
    reader.feed_eof()
    writer = _Writer()
    upstream_reader = asyncio.StreamReader()
    upstream_reader.feed_eof()
    upstream_writer = _Writer()
    calls: list[tuple[str, int]] = []

    def fake_validate(url, policy):
        assert url == "https://[2001:db8::1]:8443"
        return SimpleNamespace(addresses=("2001:db8::2",)), None

    async def fake_open_connection(host, port):
        calls.append((host, port))
        return upstream_reader, upstream_writer

    monkeypatch.setattr("athena.network.browser_proxy.validate_target", fake_validate)
    monkeypatch.setattr(
        "athena.network.browser_proxy.asyncio.open_connection", fake_open_connection
    )

    await proxy._handle(reader, writer)

    assert calls == [("2001:db8::2", 8443)]


@pytest.mark.asyncio
async def test_policy_tightening_closes_existing_tunnels():
    proxy = DNSPinnedBrowserProxy()
    client_writer = _Writer()
    upstream_writer = _Writer()
    proxy._active_tunnels[1] = (client_writer, upstream_writer)

    await proxy.set_policy("restricted")

    assert client_writer.closed
    assert upstream_writer.closed
    assert proxy._active_tunnels == {}


@pytest.mark.asyncio
async def test_restricted_proxy_rejects_loopback_before_connect():
    proxy = DNSPinnedBrowserProxy()
    await proxy.set_policy("restricted")
    reader = asyncio.StreamReader()
    reader.feed_data(b"CONNECT 127.0.0.1:80 HTTP/1.1\r\n\r\n")
    reader.feed_eof()
    writer = _Writer()

    await proxy._handle(reader, writer)

    assert b"403 Forbidden" in b"".join(writer.data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["192.168.1.10:80", "[::1]:80"],
)
async def test_restricted_proxy_rejects_private_and_ipv6_loopback(target):
    proxy = DNSPinnedBrowserProxy()
    await proxy.set_policy("restricted")
    reader = asyncio.StreamReader()
    reader.feed_data(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    reader.feed_eof()
    writer = _Writer()

    await proxy._handle(reader, writer)

    assert b"403 Forbidden" in b"".join(writer.data)


@pytest.mark.asyncio
async def test_deny_policy_rejects_new_connections_before_open(monkeypatch):
    proxy = DNSPinnedBrowserProxy()
    await proxy.set_policy("deny")
    reader = asyncio.StreamReader()
    reader.feed_data(b"CONNECT public.example.test:443 HTTP/1.1\r\n\r\n")
    reader.feed_eof()
    writer = _Writer()

    async def unexpected_open(*_args, **_kwargs):
        raise AssertionError("denied target must not open a socket")

    monkeypatch.setattr("athena.network.browser_proxy.asyncio.open_connection", unexpected_open)
    await proxy._handle(reader, writer)

    assert b"403 Forbidden" in b"".join(writer.data)


def test_tunnel_limit_rejects_connection_33():
    proxy = DNSPinnedBrowserProxy()
    proxy._active_tunnels = {
        index: (_Writer(), _Writer()) for index in range(proxy.MAX_CONCURRENT_TUNNELS)
    }
    upstream_writer = _Writer()

    with pytest.raises(PermissionError, match="tunnel limit"):
        proxy._register_tunnel(99, _Writer(), upstream_writer)

    assert upstream_writer.closed


@pytest.mark.asyncio
async def test_idle_timeout_closes_tunnel(monkeypatch):
    from athena.network.browser_proxy import _tunnel

    monkeypatch.setattr(DNSPinnedBrowserProxy, "IDLE_TIMEOUT_SECONDS", 0.001)
    client_reader = asyncio.StreamReader()
    upstream_reader = asyncio.StreamReader()
    client_writer = _Writer()
    upstream_writer = _Writer()

    await _tunnel(client_reader, client_writer, upstream_reader, upstream_writer)

    assert upstream_writer.closed


@pytest.mark.asyncio
async def test_maximum_tunnel_lifetime_closes_tunnel(monkeypatch):
    from athena.network.browser_proxy import _tunnel

    monkeypatch.setattr(DNSPinnedBrowserProxy, "IDLE_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(DNSPinnedBrowserProxy, "MAX_TUNNEL_SECONDS", 0.001)
    client_reader = asyncio.StreamReader()
    upstream_reader = asyncio.StreamReader()
    client_writer = _Writer()
    upstream_writer = _Writer()

    await _tunnel(client_reader, client_writer, upstream_reader, upstream_writer)

    assert upstream_writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_request",
    [b"CONNECT missing-port: HTTP/1.1\r\n\r\n", b"not-a-request-line\r\n\r\n"],
)
async def test_malformed_proxy_request_is_bounded_rejection(raw_request):
    proxy = DNSPinnedBrowserProxy()
    reader = asyncio.StreamReader()
    reader.feed_data(raw_request)
    reader.feed_eof()
    writer = _Writer()

    await proxy._handle(reader, writer)

    assert b"403 Forbidden" in b"".join(writer.data)


@pytest.mark.asyncio
async def test_oversized_header_without_delimiter_is_bounded_rejection():
    proxy = DNSPinnedBrowserProxy()
    writer = _Writer()

    await proxy._handle(_OverrunReader(), writer)

    assert b"403 Forbidden" in b"".join(writer.data)
    assert writer.closed
