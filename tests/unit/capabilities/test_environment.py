"""Environment capability contracts that are easy to misclassify."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from athena.capabilities.environment import DatabaseCapability, NetworkCapability
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
)
from athena.protocol.tasks import NetworkPolicy, WorkspaceSpec


def _request(**arguments):
    return CapabilityRequest(
        capability_id="database",
        arguments=arguments,
        task_id="task-db",
        call_id="db-call",
    )


@pytest.mark.asyncio
@pytest.mark.athena_scenario("ENV-001")
async def test_database_requires_workspace_context(tmp_path):
    path = tmp_path / "data.db"
    result = await DatabaseCapability().invoke(
        _request(operation="tables", path=str(path)),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "workspace" in (result.error or "")
    assert not path.exists()


def test_database_read_connection_cannot_attach_or_mutate(tmp_path):
    path = tmp_path / "data.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE records (value TEXT)")
        conn.execute("INSERT INTO records VALUES ('before')")

    capability = DatabaseCapability()
    connection = capability._connect(str(path), readonly=True)
    capability._set_read_only_authorizer(connection)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("ATTACH DATABASE ':memory:' AS other")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("UPDATE records SET value = 'after'")
    connection.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM records").fetchone() == ("before",)


@pytest.mark.asyncio
async def test_restricted_http_pins_the_checked_dns_address(monkeypatch):
    import httpx

    calls = {}

    monkeypatch.setattr(
        "athena.capabilities.environment.resolve_addresses",
        lambda host, port: ("93.184.216.34",),
    )
    monkeypatch.setattr(
        "athena.capabilities.environment.pinned_sync_transport",
        lambda host, addresses: calls.update(
            {"host": host, "addresses": tuple(addresses)}
        ) or object(),
    )

    class _Response:
        status_code = 200
        headers = {"content-type": "text/plain"}
        encoding = "utf-8"
        elapsed = SimpleNamespace(total_seconds=lambda: 0.001)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def iter_bytes(self):
            yield b"pinned"

    class _Client:
        def __init__(self, **kwargs):
            calls["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def stream(self, method, url, **kwargs):
            assert method == "GET"
            assert url == "https://example.test/"
            return _Response()

    monkeypatch.setattr(httpx, "Client", _Client)
    context = SimpleNamespace(
        workspace=WorkspaceSpec(
            id="workspace", root="/tmp", network_policy=NetworkPolicy.RESTRICTED,
        )
    )
    result = await NetworkCapability().invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-network",
            call_id="network-call",
            arguments={"operation": "http", "url": "https://example.test/"},
        ),
        context=context,
    )

    assert result.status is CapabilityResultStatus.OK
    assert calls["host"] == "example.test"
    assert calls["addresses"] == ("93.184.216.34",)
    assert calls["client"]["trust_env"] is False


@pytest.mark.asyncio
async def test_network_policy_deny_blocks_all_outbound_operations():
    context = SimpleNamespace(
        workspace=WorkspaceSpec(
            id="workspace", root="/tmp", network_policy=NetworkPolicy.DENY,
        )
    )
    for arguments in (
        {"operation": "http", "url": "https://example.test/"},
        {"operation": "tcp_connect", "host": "example.test", "port": 443},
        {"operation": "dns", "name": "example.test"},
        {"operation": "ping", "host": "example.test"},
    ):
        result = await NetworkCapability().invoke(
            CapabilityRequest(
                capability_id="network", task_id="task-network",
                call_id="network-call", arguments=arguments,
            ),
            context=context,
        )
        assert result.status is CapabilityResultStatus.FAILED
        assert "network denied" in (result.error or "")
