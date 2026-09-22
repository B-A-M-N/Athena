"""MCP runtime port contract evidence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.service.mcp_runtime_ports import MCPRuntimePorts


def test_mcp_runtime_ports_use_public_resource_names() -> None:
    owner = SimpleNamespace(_mcp_clients=[], _mcp_connection_status={})
    ports = MCPRuntimePorts(owner)

    assert ports.mcp_clients == []
    ports.mcp_connection_status = {"server": {"state": "connected"}}
    assert owner._mcp_connection_status == {"server": {"state": "connected"}}


def test_mcp_runtime_ports_reject_unlisted_resources() -> None:
    ports = MCPRuntimePorts(SimpleNamespace())

    with pytest.raises(AttributeError, match="MCP runtime port is not allowed"):
        _ = ports.unrelated_resource
