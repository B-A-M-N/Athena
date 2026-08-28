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
    _database_request_digest,
    _external_request_digest,
    _service_request_digest,
)
from athena.capabilities.dispatcher import CapabilityDispatcher, SuspendedCall
from athena.capabilities.registry import CapabilityRegistry
from athena.artifacts.store import ArtifactStore
from athena.execution.environment import ProjectEnvironmentFingerprint
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResultStatus,
    ExternalEffectPhase,
)
from athena.protocol.policy import PolicyDecision, PolicyVerdict
from athena.policy.engine import PolicyEngine
from athena.protocol.tasks import NetworkPolicy, WorkspaceSpec
from athena.state.database import Database
from athena.state.external_effects import ExternalEffectStore
from athena.state.mutations import COMPLETED, MutationStore


def _request(**arguments):
    return CapabilityRequest(
        capability_id="database",
        arguments=arguments,
        task_id="task-db",
        call_id="db-call",
    )


def test_project_environment_fingerprint_is_canonical_and_lock_bound(tmp_path):
    workspace = WorkspaceSpec(id="workspace", root=str(tmp_path))
    fingerprint = ProjectEnvironmentFingerprint()

    first = fingerprint.fingerprint(workspace, extras={"tool": "compiler"})
    first_record = fingerprint.describe(workspace, extras={"tool": "compiler"})
    assert first == fingerprint.fingerprint(workspace, extras={"tool": "compiler"})
    assert first_record["dependency_lock_hash"] is None
    assert "root" not in first_record

    lock = tmp_path / ".athena" / "dependencies.lock.json"
    lock.parent.mkdir()
    lock.write_text('{"dependencies": ["compiler==1"]}\n', encoding="utf-8")
    second = fingerprint.fingerprint(workspace, extras={"tool": "compiler"})
    assert second != first

    lock.write_text('{"dependencies": ["compiler==2"]}\n', encoding="utf-8")
    assert fingerprint.fingerprint(workspace, extras={"tool": "compiler"}) != second


def test_project_environment_fingerprint_records_tool_versions(monkeypatch, tmp_path):
    workspace = WorkspaceSpec(id="workspace", root=str(tmp_path))
    monkeypatch.setattr(
        "athena.execution.environment.shutil.which",
        lambda name: "/usr/bin/fake-tool" if name == "node" else None,
    )
    monkeypatch.setattr(
        "athena.execution.environment.os.stat",
        lambda _path: SimpleNamespace(st_mtime_ns=7, st_size=11),
    )
    monkeypatch.setattr(
        "athena.execution.environment.os.path.realpath",
        lambda path: path,
    )
    monkeypatch.setattr(
        "athena.execution.environment.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v22.1.0\n"),
    )
    record = ProjectEnvironmentFingerprint().describe(workspace)
    assert '"version":"v22.1.0"' in record["toolchain"]["node"]


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
        lambda host, addresses: (
            calls.update({"host": host, "addresses": tuple(addresses)}) or object()
        ),
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
            id="workspace",
            root="/tmp",
            network_policy=NetworkPolicy.RESTRICTED,
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
async def test_http_external_transaction_has_receipts_idempotency_and_compensation(monkeypatch):
    calls: list[dict] = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        if kwargs["url"].endswith("/verify"):
            return {"status": 200, "body_head": "present"}
        if kwargs["method"] == "DELETE":
            return {"status": 204, "body_head": ""}
        return {"status": 201, "body_head": "created"}

    monkeypatch.setattr(
        "athena.capabilities.environment._external_http_request",
        fake_request,
    )
    capability = NetworkCapability(external_store=ExternalEffectStore())
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="workspace", root="/tmp"),
    )

    async def invoke(arguments):
        return await capability.invoke(
            CapabilityRequest(
                capability_id="network",
                task_id="task-external",
                call_id=f"call-{len(calls)}",
                arguments=arguments,
            ),
            context=context,
        )

    prepared = await invoke(
        {
            "operation": "http_transaction",
            "phase": "prepare",
            "url": "https://api.example.test/items",
            "method": "POST",
            "body": "{}",
            "idempotency_key": "item-1",
            "compensate_url": "https://api.example.test/items/item-1",
            "compensate_method": "DELETE",
        }
    )
    prepared_record = json.loads(prepared.output)
    assert prepared_record["status"] == "PREPARED"
    transaction_id = prepared_record["transaction_id"]

    applied = await invoke(
        {
            "operation": "http_transaction",
            "phase": "apply",
            "url": "https://api.example.test/items",
            "method": "POST",
            "body": "{}",
            "idempotency_key": "item-1",
            "transaction_id": transaction_id,
        }
    )
    assert json.loads(applied.output)["status"] == "COMPLETED"
    assert len(calls) == 1
    assert calls[0]["headers"]["Idempotency-Key"] == "item-1"

    replay = await invoke(
        {
            "operation": "http_transaction",
            "phase": "apply",
            "url": "https://api.example.test/items",
            "method": "POST",
            "body": "{}",
            "idempotency_key": "item-1",
            "transaction_id": transaction_id,
        }
    )
    assert json.loads(replay.output)["status"] == "COMPLETED"
    assert len(calls) == 1

    verified = await invoke(
        {
            "operation": "http_transaction",
            "phase": "verify",
            "url": "https://api.example.test/items",
            "method": "POST",
            "body": "{}",
            "transaction_id": transaction_id,
            "verify_url": "https://api.example.test/items/verify",
            "verify_method": "GET",
            "expected_body_contains": "present",
        }
    )
    assert json.loads(verified.output)["status"] == "VERIFIED"

    compensated = await invoke(
        {
            "operation": "http_transaction",
            "phase": "compensate",
            "url": "https://api.example.test/items",
            "method": "POST",
            "body": "{}",
            "idempotency_key": "item-1",
            "transaction_id": transaction_id,
            "compensate_url": "https://api.example.test/items/item-1",
            "compensate_method": "DELETE",
        }
    )
    assert compensated.status is CapabilityResultStatus.OK, (
        compensated.status,
        compensated.error,
        compensated.metadata,
    )
    assert json.loads(compensated.output)["status"] == "COMPENSATION_SENT"

    compensation_verified = await invoke(
        {
            "operation": "http_transaction",
            "phase": "verify",
            "url": "https://api.example.test/items",
            "method": "POST",
            "body": "{}",
            "transaction_id": transaction_id,
            "verify_url": "https://api.example.test/items/verify",
            "verify_method": "GET",
            "expected_body_contains": "present",
        }
    )
    assert compensation_verified.status is CapabilityResultStatus.OK
    assert json.loads(compensation_verified.output)["status"] == ("COMPENSATION_VERIFIED")
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_http_external_transaction_marks_uncertain_apply_recovery(monkeypatch):
    calls = 0

    def fail_request(**kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("remote timeout")

    monkeypatch.setattr(
        "athena.capabilities.environment._external_http_request",
        fail_request,
    )
    capability = NetworkCapability(external_store=ExternalEffectStore())
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="workspace", root="/tmp"),
    )
    base = {
        "operation": "http_transaction",
        "url": "https://api.example.test/items",
        "method": "POST",
        "body": "{}",
        "idempotency_key": "item-2",
    }
    prepared = await capability.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-external",
            call_id="prepare-2",
            arguments={**base, "phase": "prepare"},
        ),
        context=context,
    )
    transaction_id = json.loads(prepared.output)["transaction_id"]
    failed = await capability.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-external",
            call_id="apply-2",
            arguments={**base, "phase": "apply", "transaction_id": transaction_id},
        ),
        context=context,
    )
    assert failed.status is CapabilityResultStatus.FAILED
    assert "recovery required" in (failed.error or "")
    retry = await capability.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-external",
            call_id="retry-2",
            arguments={**base, "phase": "apply", "transaction_id": transaction_id},
        ),
        context=context,
    )
    assert retry.status is CapabilityResultStatus.FAILED
    assert calls == 1

    # An uncertain APPLYING receipt is recoverable by verification; recovery
    # must not require issuing the mutating request a second time.
    monkeypatch.setattr(
        "athena.capabilities.environment._external_http_request",
        lambda **kwargs: {"status": 200, "body_head": "present"},
    )
    verified = await capability.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-external",
            call_id="verify-2",
            arguments={
                **base,
                "phase": "verify",
                "transaction_id": transaction_id,
                "verify_url": "https://api.example.test/items/item-2",
                "verify_method": "GET",
                "expected_body_contains": "present",
            },
        ),
        context=context,
    )
    assert verified.status is CapabilityResultStatus.OK, verified.error
    assert json.loads(verified.output)["status"] == "VERIFIED"
    assert calls == 1


@pytest.mark.asyncio
async def test_reconstructed_http_capability_refuses_apply_after_crash(monkeypatch):
    calls = 0

    def fake_request(**kwargs):
        nonlocal calls
        calls += 1
        return {"status": 201}

    monkeypatch.setattr(
        "athena.capabilities.environment._run_external_http_request",
        fake_request,
    )
    store = ExternalEffectStore()
    capability = NetworkCapability(external_store=store)
    base = {
        "operation": "http_transaction",
        "url": "https://api.example.test/items",
        "method": "POST",
        "body": "{}",
        "idempotency_key": "crash-http-1",
    }
    identity = capability.descriptor.resolve_external_identity(base)
    assert identity is not None
    await store.prepare(
        transaction_id="crash-http-tx",
        task_id="task-crash-http",
        capability_id="network",
        external_identity=identity,
        request_digest=_external_request_digest(base),
        idempotency_key=base["idempotency_key"],
        phase=ExternalEffectPhase.PREPARE,
    )
    await store.begin_apply(
        transaction_id="crash-http-tx",
        task_id="task-crash-http",
        capability_id="network",
        external_identity=identity,
        request_digest=_external_request_digest(base),
        idempotency_key=base["idempotency_key"],
    )

    reconstructed = NetworkCapability(external_store=store)
    result = await reconstructed.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-crash-http",
            call_id="crash-http-retry",
            arguments={**base, "phase": "apply", "transaction_id": "crash-http-tx"},
        )
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert "recovery" in (result.error or "")
    assert calls == 0


@pytest.mark.asyncio
async def test_reconstructed_service_capability_refuses_apply_after_crash(monkeypatch):
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return 0, "", ""

    monkeypatch.setattr("athena.capabilities.environment._run", fake_run)
    store = ExternalEffectStore()
    capability = ServiceCapability(external_store=store)
    base = {
        "operation": "service_transaction",
        "unit": "athena.service",
        "service_operation": "start",
        "idempotency_key": "crash-service-1",
    }
    identity = capability.descriptor.resolve_external_identity(base)
    assert identity is not None
    await store.prepare(
        transaction_id="crash-service-tx",
        task_id="task-crash-service",
        capability_id="service",
        external_identity=identity,
        request_digest=_service_request_digest("athena.service", "start", False),
        idempotency_key=base["idempotency_key"],
        phase=ExternalEffectPhase.PREPARE,
    )
    await store.begin_apply(
        transaction_id="crash-service-tx",
        task_id="task-crash-service",
        capability_id="service",
        external_identity=identity,
        request_digest=_service_request_digest("athena.service", "start", False),
        idempotency_key=base["idempotency_key"],
    )

    reconstructed = ServiceCapability(external_store=store)
    result = await reconstructed.invoke(
        CapabilityRequest(
            capability_id="service",
            task_id="task-crash-service",
            call_id="crash-service-retry",
            arguments={
                **base,
                "phase": "apply",
                "transaction_id": "crash-service-tx",
            },
        )
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert "recovery" in (result.error or "")
    assert calls == 0


@pytest.mark.asyncio
async def test_reconstructed_database_capability_refuses_apply_after_crash(tmp_path):
    path = str(tmp_path / "crash.db")
    base = {
        "operation": "database_transaction",
        "path": path,
        "sql": "INSERT INTO records VALUES (?)",
        "params": ["value"],
        "idempotency_key": "crash-database-1",
    }
    store = ExternalEffectStore()
    capability = DatabaseCapability(external_store=store)
    workspace = WorkspaceSpec(id="workspace", root=str(tmp_path))
    identity = capability.descriptor.resolve_external_identity(base, workspace)
    assert identity is not None
    await store.prepare(
        transaction_id="crash-database-tx",
        task_id="task-crash-database",
        capability_id="database",
        external_identity=identity,
        request_digest=_database_request_digest(path, base["sql"], base["params"]),
        idempotency_key=base["idempotency_key"],
        phase=ExternalEffectPhase.PREPARE,
    )
    await store.begin_apply(
        transaction_id="crash-database-tx",
        task_id="task-crash-database",
        capability_id="database",
        external_identity=identity,
        request_digest=_database_request_digest(path, base["sql"], base["params"]),
        idempotency_key=base["idempotency_key"],
    )

    reconstructed = DatabaseCapability(external_store=store)
    result = await reconstructed.invoke(
        CapabilityRequest(
            capability_id="database",
            task_id="task-crash-database",
            call_id="crash-database-retry",
            arguments={
                **base,
                "phase": "apply",
                "transaction_id": "crash-database-tx",
            },
        ),
        context=SimpleNamespace(workspace=workspace),
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert "recovery" in (result.error or "")
    assert not (tmp_path / "crash.db").exists()


@pytest.mark.asyncio
async def test_http_external_transaction_is_governed_by_canonical_dispatcher(
    monkeypatch,
):
    calls: list[dict] = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        return {"status": 201, "body_head": "created"}

    monkeypatch.setattr(
        "athena.capabilities.environment._external_http_request",
        fake_request,
    )
    store = ExternalEffectStore()
    capability = NetworkCapability(external_store=store)
    registry = CapabilityRegistry()
    registry.register(capability)
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"))
    workspace = WorkspaceSpec(id="workspace", root="/tmp")
    base = {
        "operation": "http_transaction",
        "url": "https://api.example.test/items",
        "method": "POST",
        "body": "{}",
        "idempotency_key": "item-3",
    }

    prepared = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="network",
            arguments={**base, "phase": "prepare"},
            task_id="task-dispatch",
            call_id="prepare-dispatch",
            origin=CapabilityRequestOrigin.SYSTEM,
        ),
        workspace=workspace,
    )
    assert prepared.status is CapabilityResultStatus.OK
    transaction_id = json.loads(prepared.output)["transaction_id"]

    apply_request = CapabilityRequest(
        capability_id="network",
        arguments={**base, "phase": "apply", "transaction_id": transaction_id},
        task_id="task-dispatch",
        call_id="apply-dispatch",
        origin=CapabilityRequestOrigin.SYSTEM,
    )
    suspended = await dispatcher.dispatch(apply_request, workspace=workspace)
    assert isinstance(suspended, SuspendedCall)
    assert calls == []
    assert suspended.approval_id is not None

    dispatcher.policy.approvals.grant(suspended.approval_id)
    applied = await dispatcher.dispatch(apply_request, workspace=workspace)
    assert applied.status is CapabilityResultStatus.OK
    assert json.loads(applied.output)["status"] == "COMPLETED"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_external_transaction_rejects_reused_idempotency_key(monkeypatch):
    monkeypatch.setattr(
        "athena.capabilities.environment._external_http_request",
        lambda **kwargs: {"status": 201, "body_head": "created"},
    )
    capability = NetworkCapability(external_store=ExternalEffectStore())
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="workspace", root="/tmp"),
    )
    base = {
        "operation": "http_transaction",
        "phase": "prepare",
        "url": "https://api.example.test/items",
        "method": "POST",
        "body": "{}",
        "idempotency_key": "same-key",
    }
    first = await capability.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-external",
            call_id="first",
            arguments=base,
        ),
        context=context,
    )
    second = await capability.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-external",
            call_id="second",
            arguments={**base, "transaction_id": "different-tx"},
        ),
        context=context,
    )
    assert first.status is CapabilityResultStatus.OK
    assert second.status is CapabilityResultStatus.FAILED
    assert "already bound" in (second.error or "")


@pytest.mark.asyncio
async def test_http_external_transaction_rejects_non_success_apply(monkeypatch):
    monkeypatch.setattr(
        "athena.capabilities.environment._external_http_request",
        lambda **kwargs: {"status": 500, "body_head": "server error"},
    )
    capability = NetworkCapability(external_store=ExternalEffectStore())
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="workspace", root="/tmp"),
    )
    base = {
        "operation": "http_transaction",
        "url": "https://api.example.test/items",
        "method": "POST",
        "body": "{}",
        "idempotency_key": "item-failure",
    }

    prepared = await capability.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-external",
            call_id="prepare-failure",
            arguments={**base, "phase": "prepare"},
        ),
        context=context,
    )
    transaction_id = json.loads(prepared.output)["transaction_id"]
    applied = await capability.invoke(
        CapabilityRequest(
            capability_id="network",
            task_id="task-external",
            call_id="apply-failure",
            arguments={**base, "phase": "apply", "transaction_id": transaction_id},
        ),
        context=context,
    )

    assert applied.status is CapabilityResultStatus.FAILED
    assert json.loads(applied.output)["status"] == "APPLY_FAILED"
    assert "500" in (applied.error or "")


@pytest.mark.asyncio
async def test_network_policy_deny_blocks_all_outbound_operations():
    context = SimpleNamespace(
        workspace=WorkspaceSpec(
            id="workspace",
            root="/tmp",
            network_policy=NetworkPolicy.DENY,
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
                capability_id="network",
                task_id="task-network",
                call_id="network-call",
                arguments=arguments,
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
        _request(operation="execute", path=path, sql="CREATE TABLE records (value TEXT)"),
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
            _request(
                operation="execute", path=path, sql="INSERT INTO records VALUES (?)", params=[value]
            ),
            context=context,
        )
        assert result.status is CapabilityResultStatus.OK

    page = await capability.invoke(
        _request(
            operation="query",
            path=path,
            sql="SELECT value FROM records ORDER BY rowid",
            offset=1,
            limit=1,
        ),
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
async def test_database_external_transaction_is_idempotent_verifiable_and_reversible(
    tmp_path,
):
    path = tmp_path / "records.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE records (value TEXT)")
    connection.execute("INSERT INTO records VALUES ('before')")
    connection.commit()
    connection.close()

    capability = DatabaseCapability(
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        external_store=ExternalEffectStore(),
    )
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="workspace", root=str(tmp_path)),
    )
    base = {
        "operation": "database_transaction",
        "path": str(path),
        "sql": "INSERT INTO records VALUES (?)",
        "params": ["after"],
        "idempotency_key": "records-insert-1",
    }

    prepared = await capability.invoke(_request(**base, phase="prepare"), context=context)
    assert prepared.status is CapabilityResultStatus.OK, prepared.error
    prepared_receipt = json.loads(prepared.output)
    transaction_id = prepared_receipt["transaction_id"]

    applied = await capability.invoke(
        _request(**base, phase="apply", transaction_id=transaction_id),
        context=context,
    )
    assert applied.status is CapabilityResultStatus.OK, applied.error
    applied_receipt = json.loads(applied.output)
    assert applied_receipt["status"] == "COMPLETED"
    assert applied_receipt["response"]["before_ref"]

    replay = await capability.invoke(
        _request(**base, phase="apply", transaction_id=transaction_id),
        context=context,
    )
    assert replay.status is CapabilityResultStatus.OK
    assert json.loads(replay.output)["status"] == "COMPLETED"

    verified = await capability.invoke(
        _request(
            **base,
            phase="verify",
            transaction_id=transaction_id,
            verify_sql="SELECT value FROM records ORDER BY rowid",
            expected_rowcount=2,
            expected_value="before",
        ),
        context=context,
    )
    assert verified.status is CapabilityResultStatus.OK, verified.error
    assert json.loads(verified.output)["status"] == "VERIFIED"

    compensated = await capability.invoke(
        _request(**base, phase="compensate", transaction_id=transaction_id),
        context=context,
    )
    assert compensated.status is CapabilityResultStatus.OK, compensated.error
    assert json.loads(compensated.output)["status"] == "COMPENSATION_VERIFIED"

    connection = sqlite3.connect(path)
    rows = connection.execute("SELECT value FROM records").fetchall()
    connection.close()
    assert rows == [("before",)]


@pytest.mark.asyncio
async def test_database_external_apply_is_approval_gated_by_canonical_dispatcher(
    tmp_path,
):
    capability = DatabaseCapability(external_store=ExternalEffectStore())
    registry = CapabilityRegistry()
    registry.register(capability)
    dispatcher = CapabilityDispatcher(registry, PolicyEngine())
    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="database",
            task_id="task-db",
            call_id="database-apply-approval",
            arguments={
                "operation": "database_transaction",
                "phase": "apply",
                "path": str(tmp_path / "records.db"),
                "sql": "INSERT INTO records VALUES (?)",
                "params": ["value"],
                "transaction_id": "database-tx-1",
                "idempotency_key": "database-write-1",
            },
        ),
        workspace=WorkspaceSpec(id="workspace", root=str(tmp_path)),
    )

    assert isinstance(result, SuspendedCall)
    assert capability._external_store._memory == {}


@pytest.mark.asyncio
async def test_external_effect_contract_floor_narrows_an_ordinary_allow():
    store = ExternalEffectStore()
    capability = NetworkCapability(external_store=store)
    registry = CapabilityRegistry()
    registry.register(capability)
    policy = PolicyEngine("autonomous")
    policy.evaluate = lambda request, autonomy=None: PolicyDecision(  # type: ignore[method-assign]
        PolicyVerdict.ALLOW,
        "test policy allow",
    )
    dispatcher = CapabilityDispatcher(registry, policy)

    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="network",
            task_id="task-network",
            call_id="contract-floor",
            arguments={
                "operation": "http_transaction",
                "phase": "apply",
                "url": "https://api.example.test/items",
                "method": "POST",
                "transaction_id": "tx-floor",
                "idempotency_key": "key-floor",
            },
        ),
        workspace=WorkspaceSpec(id="workspace", root="/tmp"),
    )

    assert isinstance(result, SuspendedCall)
    assert result.approval_id
    assert store._memory == {}


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
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="service",
            task_id="task-1",
            call_id="service-1",
            arguments={"operation": "status", "unit": "athena.service"},
        )
    )

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    assert payload["state"]["activestate"] == "active"
    assert payload["state"]["substate"] == "running"

    rejected = await capability.invoke(
        CapabilityRequest(
            capability_id="service",
            task_id="task-1",
            call_id="service-2",
            arguments={"operation": "status", "unit": "--user"},
        )
    )
    assert rejected.status is CapabilityResultStatus.FAILED
    assert "invalid systemd unit" in (rejected.error or "")
    assert all("--user" not in command[3:] for command in calls)


@pytest.mark.asyncio
async def test_service_external_transaction_is_idempotent_verifiable_and_reversible(
    monkeypatch,
):
    active_state = "inactive"
    action_calls: list[list[str]] = []

    def fake_run(command, timeout=15.0, shell=False):
        nonlocal active_state
        if "show" in command:
            return (
                0,
                (
                    "LoadState=loaded\n"
                    f"ActiveState={active_state}\n"
                    "SubState=running\n"
                    "UnitFileState=disabled\n"
                ),
                "",
            )
        action_calls.append(command)
        operation = command[1]
        if operation == "start":
            active_state = "active"
        elif operation == "stop":
            active_state = "inactive"
        return 0, "", ""

    monkeypatch.setattr("athena.capabilities.environment._run", fake_run)
    capability = ServiceCapability(external_store=ExternalEffectStore())
    base = {
        "operation": "service_transaction",
        "unit": "athena.service",
        "service_operation": "start",
        "idempotency_key": "service-1",
    }
    prepared = await capability.invoke(
        CapabilityRequest(
            capability_id="service",
            task_id="task-service",
            call_id="prepare-service",
            arguments={**base, "phase": "prepare"},
        )
    )
    prepared_payload = json.loads(prepared.output)
    assert prepared_payload["status"] == "PREPARED"
    transaction_id = prepared_payload["transaction_id"]
    assert prepared_payload["response"]["before"]["activestate"] == "inactive"

    applied = await capability.invoke(
        CapabilityRequest(
            capability_id="service",
            task_id="task-service",
            call_id="apply-service",
            arguments={**base, "phase": "apply", "transaction_id": transaction_id},
        )
    )
    assert json.loads(applied.output)["status"] == "COMPLETED"
    assert len(action_calls) == 1

    replay = await capability.invoke(
        CapabilityRequest(
            capability_id="service",
            task_id="task-service",
            call_id="replay-service",
            arguments={**base, "phase": "apply", "transaction_id": transaction_id},
        )
    )
    assert json.loads(replay.output)["status"] == "COMPLETED"
    assert len(action_calls) == 1

    verified = await capability.invoke(
        CapabilityRequest(
            capability_id="service",
            task_id="task-service",
            call_id="verify-service",
            arguments={
                **base,
                "phase": "verify",
                "transaction_id": transaction_id,
                "expected_active_state": "active",
            },
        )
    )
    assert json.loads(verified.output)["status"] == "VERIFIED"

    compensated = await capability.invoke(
        CapabilityRequest(
            capability_id="service",
            task_id="task-service",
            call_id="compensate-service",
            arguments={
                **base,
                "phase": "compensate",
                "transaction_id": transaction_id,
                "compensate_operation": "stop",
            },
        )
    )
    assert json.loads(compensated.output)["status"] == "COMPENSATION_VERIFIED"
    assert [command[1] for command in action_calls] == ["start", "stop"]
