"""Environment capability contracts that are easy to misclassify."""

from __future__ import annotations

import sqlite3

import pytest

from athena.capabilities.environment import DatabaseCapability
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
)


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
