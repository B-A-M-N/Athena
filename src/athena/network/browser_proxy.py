"""Small Athena-owned browser proxy with per-request DNS pinning.

Playwright's URL interception does not control the socket resolver.  This
proxy validates every CONNECT/HTTP request, connects to the checked address,
and tunnels TLS/WebSocket traffic without giving Chromium a second resolver.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from athena.network.target_policy import validate_target


class DNSPinnedBrowserProxy:
    MAX_CONCURRENT_TUNNELS = 32
    IDLE_TIMEOUT_SECONDS = 60.0
    MAX_TUNNEL_SECONDS = 300.0

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._policy = "allow"
        self._active_tunnels: dict[int, tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = {}

    @property
    def server_url(self) -> str:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("browser proxy is not started")
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def start(self) -> str:
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server_url

    async def set_policy(self, policy: str | object | None) -> None:
        requested = str(getattr(policy, "value", policy) or "allow").casefold()
        rank = {"allow": 0, "restricted": 1, "deny": 2}
        previous = self._policy
        self._policy = requested
        if rank.get(requested, 2) > rank.get(previous, 0):
            await self._close_active_tunnels()

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        await self._close_active_tunnels()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tunnel_id = id(writer)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            header_bytes = await reader.readuntil(b"\r\n\r\n")
            if len(header_bytes) > 64 * 1024:
                raise ValueError("browser proxy request headers are too large")
            lines = header_bytes[:-4].decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers = [line for line in lines[1:] if line and not line.lower().startswith("proxy-")]
            if method.upper() == "CONNECT":
                host, port = _split_host_port(target, default=443)
                validated, error = validate_target(
                    f"https://{_format_host(host)}:{port}", self._policy
                )
                if error or validated is None:
                    raise PermissionError(error or "browser proxy target rejected")
                upstream = await asyncio.open_connection(
                    validated.addresses[0] if validated.addresses else host, port
                )
                upstream_writer = upstream[1]
                self._register_tunnel(tunnel_id, writer, upstream_writer)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await _tunnel(reader, writer, *upstream)
                return
            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("browser proxy requires an absolute HTTP URL")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            validated, error = validate_target(target, self._policy)
            if error or validated is None:
                raise PermissionError(error or "browser proxy target rejected")
            upstream_reader, upstream_writer = await asyncio.open_connection(
                validated.addresses[0] if validated.addresses else parsed.hostname, port
            )
            self._register_tunnel(tunnel_id, writer, upstream_writer)
            request_target = parsed.path or "/"
            if parsed.query:
                request_target += "?" + parsed.query
            outgoing = f"{method} {request_target} {version}\r\n"
            outgoing += "\r\n".join(headers) + "\r\n\r\n"
            upstream_writer.write(outgoing.encode("latin-1"))
            await upstream_writer.drain()
            await _tunnel(reader, writer, upstream_reader, upstream_writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, PermissionError, ValueError):
            try:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        finally:
            self._active_tunnels.pop(tunnel_id, None)
            if upstream_writer is not None:
                upstream_writer.close()
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _register_tunnel(
        self,
        tunnel_id: int,
        client_writer: asyncio.StreamWriter,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        if len(self._active_tunnels) >= self.MAX_CONCURRENT_TUNNELS:
            upstream_writer.close()
            raise PermissionError("browser proxy tunnel limit reached")
        self._active_tunnels[tunnel_id] = (client_writer, upstream_writer)

    async def _close_active_tunnels(self) -> None:
        tunnels = list(self._active_tunnels.values())
        self._active_tunnels.clear()
        for client_writer, upstream_writer in tunnels:
            client_writer.close()
            upstream_writer.close()
        await asyncio.gather(
            *(writer.wait_closed() for tunnel in tunnels for writer in tunnel),
            return_exceptions=True,
        )


async def _tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    async def copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(64 * 1024), DNSPinnedBrowserProxy.IDLE_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    return
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()
        finally:
            try:
                writer.write_eof()
            except (AttributeError, OSError, RuntimeError):
                pass

    tasks = {
        asyncio.create_task(copy(client_reader, upstream_writer)),
        asyncio.create_task(copy(upstream_reader, client_writer)),
    }
    try:
        await asyncio.wait(
            tasks,
            timeout=DNSPinnedBrowserProxy.MAX_TUNNEL_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    upstream_writer.close()
    await upstream_writer.wait_closed()


def _split_host_port(value: str, *, default: int) -> tuple[str, int]:
    raw = str(value).strip()
    if raw.startswith("["):
        host, _, port = raw[1:].partition("]")
        return host, int(port[1:]) if port.startswith(":") else default
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        return host, int(port)
    return raw, default


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


__all__ = ["DNSPinnedBrowserProxy"]
