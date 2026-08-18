import pytest

from athena.mcp.adapter import MCPAdapter
from athena.mcp.client import MCPClient, MCPToolRef
from athena.mcp.tools import canonical_capability_id
from athena.capabilities.registry import CapabilityRegistry
from athena.protocol.capabilities import CapabilityOrigin


@pytest.fixture
def registry():
    return CapabilityRegistry()


@pytest.fixture
def fake_client():
    return MCPClient(connection_id="conn-1", url="http://127.0.0.1:1")


def _tool(**overrides):
    spec = dict(
        name="k8s_run",
        description="Run a pod on the cluster",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "replicas": {"type": "integer"}},
            "required": ["name"],
        },
        annotations={"readOnlyHint": True},
    )
    spec.update(overrides)
    return MCPToolRef(**spec)


def test_builds_capability_descriptor_namespaced_and_untrusted(registry, fake_client):
    adapter = MCPAdapter(registry, origin=CapabilityOrigin.MCP)
    descriptor = adapter.build_descriptor(
        _tool(), connection_id="conn-1", server_alias="kube"
    )
    assert descriptor.id == "mcp:conn-1:k8s_run"
    assert canonical_capability_id("conn-1", "k8s_run") == descriptor.id
    assert descriptor.origin is CapabilityOrigin.MCP
    assert "untrusted" in descriptor.tags


def test_tool_schema_maps_to_descriptor_input_schema(registry, fake_client):
    adapter = MCPAdapter(registry)
    descriptor = adapter.build_descriptor(
        _tool(), connection_id="conn-2", server_alias="kube"
    )
    schema = descriptor.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["name"]
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["replicas"]["type"] == "integer"


async def test_registered_tool_resolves_through_capability_registry(registry, fake_client):
    adapter = MCPAdapter(registry)
    descriptor = adapter.register_tool(
        _tool(), connection_id="conn-3", client=fake_client, server_alias="kube"
    )
    resolved = registry.resolve(descriptor.id)
    assert resolved.id == descriptor.id
    assert resolved.origin is CapabilityOrigin.MCP

    assert descriptor.id in adapter.capability_ids()