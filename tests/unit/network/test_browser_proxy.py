from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from athena.network.browser_proxy import DNSPinnedBrowserProxy


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
