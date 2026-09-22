"""MCP connection lifecycle mechanism bound to the Athena service facade."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from athena.mcp.client import MCPClient
from athena.mcp.supervisor import MCPConnectionSupervisor
from athena.protocol.errors import ServiceNotReady
from athena.policy.credentials import SecretError
from athena.service.config import MCPConfig
from athena.service.mcp_runtime_ports import MCPRuntimePorts

_logger = logging.getLogger("athena.service.mcp_runtime")


class MCPRuntime:
    def __init__(self, service: Any, *, ports: MCPRuntimePorts | None = None) -> None:
        self._ports = ports or MCPRuntimePorts(service)

    def status(self) -> dict[str, dict[str, Any]]:
        service = self._ports
        status = dict(service.mcp_connection_status)
        configured = {server.name: server for server in service.config.mcp_servers}
        for name, server in configured.items():
            status.setdefault(
                name,
                {
                    "id": name,
                    "configured": True,
                    "required": bool(server.required),
                    "state": "configured",
                    "transport": "http" if server.url else "stdio",
                    "tool_count": 0,
                    "last_successful_connection": None,
                    "last_error": None,
                },
            )
        for client in service.mcp_clients:
            live = client.health()
            configured_server = configured.get(client.connection_id)
            live.update(
                {
                    "configured": configured_server is not None,
                    "required": bool(configured_server.required)
                    if configured_server is not None
                    else False,
                    "transport": (
                        "http"
                        if configured_server is not None and configured_server.url
                        else "stdio"
                    ),
                }
            )
            status[client.connection_id] = live
        return {name: dict(status[name]) for name in sorted(status)}

    def resources(self) -> list[dict[str, Any]]:
        provider = self._ports.mcp_resources
        if provider is None:
            return []
        return [
            {
                "uri": ref.uri,
                "name": ref.name,
                "description": ref.description,
                "server": ref.server,
            }
            for ref in provider.available()
        ]

    async def read_resource(self, uri: str, *, connection_id: str | None = None) -> list[Any]:
        provider = self._ports.mcp_resources
        if provider is None:
            raise ServiceNotReady("MCP resources are not initialized")
        return await provider.read_resource_blocks(uri, connection_id=connection_id)

    async def prompts(self) -> list[dict[str, Any]]:
        provider = self._ports.mcp_prompts
        if provider is None:
            return []
        return [
            {
                "name": ref.name,
                "description": ref.description,
                "arguments": list(ref.arguments),
                "server": ref.server,
            }
            for ref in await provider.available()
        ]

    async def render_prompt(
        self,
        name: str,
        arguments: Mapping[str, str] | None = None,
        *,
        connection_id: str | None = None,
    ) -> list[Any]:
        provider = self._ports.mcp_prompts
        if provider is None:
            raise ServiceNotReady("MCP prompts are not initialized")
        return await provider.render_prompt_blocks(
            name, dict(arguments or {}), connection_id=connection_id
        )

    async def connect(self) -> None:
        for server in self._ports.config.mcp_servers:
            await self.connect_server(server)

    async def start_supervisor(self) -> None:
        service = self._ports
        if not service.config.mcp_servers:
            return
        service.mcp_supervisor = MCPConnectionSupervisor(
            (server.name for server in service.config.mcp_servers),
            service.mcp_reconnect,
        )
        for name, status in service.mcp_connection_status.items():
            if status.get("state") == "connected":
                service.mcp_supervisor.mark_connected(name)
        await service.mcp_supervisor.start()

    async def handle_transport_failure(self, connection_id: str, error: BaseException) -> None:
        """Invalidate a live MCP surface before reconnect is attempted."""
        service = self._ports
        name = str(connection_id)
        current = dict(service.mcp_connection_status.get(name) or {})
        current.update(
            {
                "id": name,
                "state": "failed",
                "tool_count": 0,
                "last_error": f"{type(error).__name__}: {error}",
            }
        )
        service.mcp_connection_status[name] = current
        if service.mcp is not None:
            service.mcp.unregister_connection(name)
        if service.mcp_resources is not None:
            service.mcp_resources.remove_client(name)
        if service.mcp_prompts is not None:
            service.mcp_prompts.remove_client(name)
        if service.mcp_supervisor is not None:
            service.mcp_supervisor.mark_failed(name)
        service.live_capability_profile_status(self.status())

    async def connect_server(self, server: MCPConfig) -> dict[str, Any]:
        service = self._ports
        transport = "http" if server.url else "stdio"
        service.mcp_connection_status[server.name] = {
            "id": server.name,
            "configured": True,
            "required": bool(server.required),
            "state": "connecting",
            "transport": transport,
            "tool_count": 0,
            "last_successful_connection": None,
            "last_error": None,
        }
        client: MCPClient | None = None
        try:
            env = dict(server.env)
            if server.secret_env and service.secrets is not None:
                for env_name, credential_id in server.secret_env.items():
                    env[env_name] = service.secrets.resolve(credential_id)
            headers = dict(server.headers)
            if server.secret_headers and service.secrets is not None:
                for header_name, credential_id in server.secret_headers.items():
                    headers[header_name] = service.secrets.resolve(credential_id)
            if server.credential_id:
                if service.secrets is None:
                    raise SecretError(
                        f"MCP server {server.name!r} requires SecretManager credential"
                    )
                credential = service.secrets.resolve(server.credential_id)
                scheme = str(server.auth_scheme or "bearer").strip().lower()
                if scheme == "bearer":
                    headers.setdefault("Authorization", f"Bearer {credential}")
                elif scheme == "basic":
                    headers.setdefault("Authorization", f"Basic {credential}")
                else:
                    raise ValueError(
                        f"MCP server {server.name!r} auth_scheme must be bearer or basic"
                    )
            client_factory = getattr(service, "mcp_client_factory", MCPClient)
            client = client_factory(
                server.name,
                command=server.command,
                args=list(server.args),
                url=server.url,
                env=env,
                headers=headers,
                connect_timeout=server.connect_timeout,
                allow_insecure_remote=server.allow_insecure_remote,
                trust_env=server.trust_env,
                credentialed=bool(server.credential_id or server.secret_headers),
                on_transport_failure=service.handle_mcp_transport_failure,
            )
            await client.connect()
            service.mcp_clients.append(client)
            if service.mcp_resources is not None:
                service.mcp_resources.add_client(
                    server.name,
                    client,
                    allowed=server.allowed_resources,
                    denied=server.denied_resources,
                )
            if service.mcp_prompts is not None:
                service.mcp_prompts.add_client(
                    server.name,
                    client,
                    allowed=server.allowed_prompts,
                    denied=server.denied_prompts,
                )
            if service.mcp is not None:
                tools = await client.list_tools()
                allowed = set(server.allowed_tools)
                denied = set(server.denied_tools)
                if allowed:
                    tools = [tool for tool in tools if tool.name in allowed]
                if denied:
                    tools = [tool for tool in tools if tool.name not in denied]
                descriptors = service.mcp.register_all(
                    tools,
                    connection_id=client.connection_id,
                    client=client,
                    server_alias=server.name,
                )
                for discover in (client.list_resources, client.list_prompts):
                    try:
                        await discover()
                    except Exception as exc:  # noqa: BLE001 - discovery is optional
                        _logger.info(
                            "MCP discovery failed for %s (%s)", server.name, type(exc).__name__
                        )
            else:
                descriptors = []
            service.mcp_connection_status[server.name] = {
                **client.health(),
                "state": "connected",
                "tool_count": len(descriptors),
            }
            service.mcp_reconnect_failures[server.name] = 0
        except Exception as exc:
            _logger.warning("MCP server %s failed to connect (%s)", server.name, type(exc).__name__)
            if client is not None:
                if client in service.mcp_clients:
                    service.mcp_clients.remove(client)
                if service.mcp is not None:
                    service.mcp.unregister_connection(server.name)
                if service.mcp_resources is not None:
                    service.mcp_resources.remove_client(server.name)
                if service.mcp_prompts is not None:
                    service.mcp_prompts.remove_client(server.name)
                try:
                    await client.close()
                except Exception as close_exc:  # noqa: BLE001 - preserve original failure
                    _logger.info("MCP failed-connection cleanup failed: %s", close_exc)
            failures = service.mcp_reconnect_failures.get(server.name, 0) + 1
            service.mcp_reconnect_failures[server.name] = failures
            service.mcp_connection_status[server.name] = {
                **service.mcp_connection_status[server.name],
                "state": "circuit_open" if failures >= 3 else "failed",
                "consecutive_failures": failures,
                "circuit_open": failures >= 3,
                "last_error": "transport connection failed",
            }
        return dict(service.mcp_connection_status[server.name])

    async def reconnect(self, name: str) -> dict[str, Any]:
        service = self._ports
        server = next((item for item in service.config.mcp_servers if item.name == name), None)
        if server is None:
            return {"id": name, "state": "failed", "last_error": "server is not configured"}
        for client in list(service.mcp_clients):
            if client.connection_id != name:
                continue
            if service.mcp is not None:
                service.mcp.unregister_connection(name)
            if service.mcp_resources is not None:
                service.mcp_resources.remove_client(name)
            if service.mcp_prompts is not None:
                service.mcp_prompts.remove_client(name)
            await client.close()
            service.mcp_clients.remove(client)
        return await self.connect_server(server)


__all__ = ["MCPRuntime", "MCPRuntimePorts"]
