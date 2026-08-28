"""Environment capability contracts that are easy to misclassify."""

from __future__ import annotations

import sqlite3
import json
from types import SimpleNamespace

import pytest

from athena.capabilities.environment import (
    DatabaseCapability,
    NetworkCapability,
    ServiceCapability,
)
from athena.artifacts.store import ArtifactStore
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
)
from athena.protocol.tasks import NetworkPolicy, WorkspaceSpec
from athena.state.database import Database
from athena.state.mutations import COMPLETED, MutationStore


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


@pytest.mark.asyncio
async def test_database_execute_records_snapshot_and_queries_are_paginated(tmp_path):
    db = Database(":memory:")
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-db', 'RUNNING', 'supervised', 'database test', "
        "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
    )
    mutation_store = MutationStore(db)
    capability = DatabaseCapability(
        mutation_store=mutation_store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="workspace", root=str(tmp_path)),
    )
    path = str(tmp_path / "records.db")

    created = await capability.invoke(
        _request(operation="execute", path=path,
                 sql="CREATE TABLE records (value TEXT)"),
        context=context,
    )
    assert created.status is CapabilityResultStatus.OK
    rows = await mutation_store.list_for_task("task-db")
    assert len(rows) == 1
    assert rows[0]["status"] == COMPLETED
    assert rows[0]["reversible"] is True
    assert rows[0]["operation"] == "database.execute"

    for value in ("one", "two", "three"):
        result = await capability.invoke(
            _request(operation="execute", path=path,
                     sql="INSERT INTO records VALUES (?)", params=[value]),
            context=context,
        )
        assert result.status is CapabilityResultStatus.OK

    page = await capability.invoke(
        _request(operation="query", path=path,
                 sql="SELECT value FROM records ORDER BY rowid",
                 offset=1, limit=1),
        context=context,
    )
    assert page.status is CapabilityResultStatus.OK
    payload = json.loads(page.output)
    assert payload["rows"] == [["two"]]
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert payload["truncated"] is True
    await db.close()


@pytest.mark.asyncio
async def test_database_rejects_symlinked_path_components(tmp_path):
    outside = tmp_path.parent / "outside-db"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted")
    capability = DatabaseCapability()
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="workspace", root=str(tmp_path)),
    )

    result = await capability.invoke(
        _request(operation="execute", path="linked/data.db", sql="SELECT 1"),
        context=context,
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "symlink" in (result.error or "") or "outside workspace" in (result.error or "")


@pytest.mark.asyncio
async def test_service_status_is_structured_and_unit_names_are_constrained(monkeypatch):
    calls = []

    def fake_run(command, timeout=15.0, shell=False):
        calls.append(command)
        if "show" in command:
            return 0, "LoadState=loaded\nActiveState=active\nSubState=running\n", ""
        return 0, "● athena.service - Athena\n", ""

    monkeypatch.setattr("athena.capabilities.environment._run", fake_run)
    capability = ServiceCapability()
    result = await capability.invoke(CapabilityRequest(
        capability_id="service", task_id="task-1", call_id="service-1",
        arguments={"operation": "status", "unit": "athena.service"},
    ))

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    assert payload["state"]["activestate"] == "active"
    assert payload["state"]["substate"] == "running"

    rejected = await capability.invoke(CapabilityRequest(
        capability_id="service", task_id="task-1", call_id="service-2",
        arguments={"operation": "status", "unit": "--user"},
    ))
    assert rejected.status is CapabilityResultStatus.FAILED
    assert "invalid systemd unit" in (rejected.error or "")
    assert all("--user" not in command[3:] for command in calls)
