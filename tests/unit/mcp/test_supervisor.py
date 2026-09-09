import asyncio

import pytest

from athena.mcp.supervisor import MCPConnectionSupervisor


@pytest.mark.asyncio
async def test_mcp_supervisor_opens_circuit_and_recovers_after_cooldown():
    attempts = 0

    async def reconnect(_name):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return {"state": "disconnected"}
        return {"state": "connected"}

    supervisor = MCPConnectionSupervisor(
        ["server"],
        reconnect,
        interval_seconds=0.01,
        max_backoff_seconds=0.01,
        jitter=0,
        circuit_failures=2,
        circuit_open_seconds=0.02,
    )
    await supervisor.start()
    try:
        await asyncio.sleep(0.25)
        assert supervisor.states["server"] == MCPConnectionSupervisor.CONNECTED
        assert attempts >= 3
    finally:
        await supervisor.stop()
