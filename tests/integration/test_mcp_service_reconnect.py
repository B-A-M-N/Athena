"""Service-level MCP transport loss, readiness, and reconnect proof."""

from __future__ import annotations

import asyncio

import pytest

from athena.mcp.client import MCPPromptRef, MCPResourceRef, MCPToolRef
from athena.protocol.errors import ServiceNotReady
from athena.protocol.tasks import AgentRequest
from athena.service.config import AthenaConfig, MCPConfig, ProviderConfig
from athena.service.service import AthenaService


class _ReconnectableMCPClient:
    instances: list["_ReconnectableMCPClient"] = []

    def __init__(self, connection_id: str, *, on_transport_failure=None, **_kwargs) -> None:
        self.connection_id = connection_id
        self._on_transport_failure = on_transport_failure
        self.connected = False
        self._resource_cache: dict[str, MCPResourceRef] = {}
        self._prompt_cache: dict[str, MCPPromptRef] = {}
        self.__class__.instances.append(self)

    async def connect(self):
        self.connected = True
        return self

    async def close(self) -> None:
        self.connected = False

    def health(self) -> dict[str, object]:
        return {
            "id": self.connection_id,
            "configured": True,
            "state": "connected" if self.connected else "stopped",
            "transport": "stdio",
            "tool_count": 1 if self.connected else 0,
            "last_successful_connection": "test",
            "last_error": None,
        }

    async def list_tools(self) -> list[MCPToolRef]:
        if not self.connected:
            raise RuntimeError("transport is down")
        return [
            MCPToolRef(
                name="inspect",
                description="Inspect the remote fixture",
                annotations={"readOnlyHint": True},
                server=self.connection_id,
            )
        ]

    async def list_resources(self) -> list[MCPResourceRef]:
        if not self.connected:
            raise RuntimeError("transport is down")
        ref = MCPResourceRef(
            uri="mcp://demo/resource",
            name="demo resource",
            server=self.connection_id,
        )
        self._resource_cache[ref.uri] = ref
        return [ref]

    async def list_prompts(self) -> list[MCPPromptRef]:
        if not self.connected:
            raise RuntimeError("transport is down")
        ref = MCPPromptRef(
            name="demo-prompt",
            description="Demo prompt",
            server=self.connection_id,
        )
        self._prompt_cache[ref.name] = ref
        return [ref]

    async def fail_transport(self) -> None:
        self.connected = False
        assert self._on_transport_failure is not None
        await self._on_transport_failure(self.connection_id, RuntimeError("transport died"))


@pytest.mark.athena_claim("BHV-MCP-RECONNECT-SERVICE")
@pytest.mark.athena_evidence("test", "integration")
@pytest.mark.asyncio
async def test_service_mcp_loss_removes_surface_blocks_admission_and_recovers(
    tmp_path,
    monkeypatch,
) -> None:
    _ReconnectableMCPClient.instances.clear()
    monkeypatch.setattr("athena.service.service.MCPClient", _ReconnectableMCPClient)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = AthenaService(
        config=AthenaConfig(
            db_path=":memory:",
            workspace_root=str(workspace),
            artifact_root=str(workspace / "artifacts"),
            providers=(
                ProviderConfig(
                    kind="fake",
                    name="fake",
                    extra={
                        "scripts": [
                            {
                                "match": {"user_contains": "MCP_RECONNECT_TASK"},
                                "respond": {"text": "recovered", "done": True},
                            }
                        ]
                    },
                ),
            ),
            mcp_servers=(MCPConfig(name="demo", command="fixture", required=True),),
            required_capabilities=("mcp:demo",),
        )
    )
    await service.start()
    try:
        assert len(_ReconnectableMCPClient.instances) == 1
        assert service._mcp is not None
        assert service._mcp.capability_ids() == ["mcp:demo:inspect"]
        assert service._mcp_resources is not None
        assert [item.uri for item in service._mcp_resources.available()] == ["mcp://demo/resource"]
        assert service._mcp_prompts is not None
        assert [item.name for item in await service._mcp_prompts.available()] == ["demo-prompt"]
        await service.require_capability_profile_ready()

        supervisor = service._mcp_supervisor
        assert supervisor is not None
        supervisor._base = 30.0
        supervisor._max = 30.0
        supervisor._jitter = 0.0
        await _ReconnectableMCPClient.instances[0].fail_transport()

        assert service._mcp_connection_status["demo"]["state"] == "failed"
        assert service._mcp.capability_ids() == []
        assert service._mcp_resources.available() == []
        assert await service._mcp_prompts.available() == []
        with pytest.raises(ServiceNotReady):
            await service.require_capability_profile_ready()

        supervisor._base = 0.01
        supervisor._max = 0.01
        supervisor.notify("demo")
        for _ in range(200):
            if (
                len(_ReconnectableMCPClient.instances) >= 2
                and service._mcp_connection_status["demo"]["state"] == "connected"
                and service._mcp.capability_ids() == ["mcp:demo:inspect"]
            ):
                break
            await asyncio.sleep(0.01)

        assert len(_ReconnectableMCPClient.instances) == 2
        assert service._mcp_connection_status["demo"]["state"] == "connected"
        assert service._mcp.capability_ids() == ["mcp:demo:inspect"]
        assert [item.uri for item in service._mcp_resources.available()] == ["mcp://demo/resource"]
        assert [item.name for item in await service._mcp_prompts.available()] == ["demo-prompt"]
        await service.require_capability_profile_ready()
        task = await service.submit(AgentRequest(prompt="MCP_RECONNECT_TASK"), wait=True)
        assert await service.get_task_status(task.id) == "COMPLETE"
    finally:
        await service.stop()
