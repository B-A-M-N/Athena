"""Typed capability failures, truthful health, and classified retry behavior."""

from __future__ import annotations

import asyncio

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.health import CapabilityHealth
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    RetryPolicy,
)
from athena.protocol.tasks import WorkspaceSpec


class _Executor:
    def __init__(self, descriptor, behavior=None):
        self.descriptor = descriptor
        self.behavior = behavior or (
            lambda request: CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )
        )
        self.attempts = 0

    async def invoke(self, request, **kwargs):
        self.attempts += 1
        return self.behavior(request)


def _read(schema=None, effects=None):
    from athena.protocol.capabilities import EffectClass

    effects = effects or frozenset({EffectClass.READ_LOCAL})
    return CapabilityDescriptor(
        id="typed.read",
        description="typed read",
        input_schema=schema or {"type": "object"},
        effects=effects,
        retry_policy=RetryPolicy.READ_ONLY,
    )


def _dispatcher(executor, health=None):
    registry = CapabilityRegistry()
    registry.register(executor)
    return CapabilityDispatcher(registry, PolicyEngine("autonomous"), health=health)


def _request(**arguments):
    return CapabilityRequest("typed.read", arguments, task_id="typed-task", call_id="typed-call")


def _workspace(tmp_path):
    return WorkspaceSpec(id="typed", root=str(tmp_path))


async def test_filesystem_missing_read_carries_typed_domain_failure(tmp_path):
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    health = CapabilityHealth(failure_threshold=2)
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("autonomous"), health=health)
    request = CapabilityRequest(
        "fs", {"operation": "read", "path": "missing.txt"}, task_id="fs-task", call_id="fs-call"
    )
    result = await dispatcher.dispatch(request, workspace=_workspace(tmp_path))

    assert result.status is CapabilityResultStatus.FAILED
    assert result.metadata["failure_code"] == "not_found"
    assert result.metadata["failure_operation"] == "read"
    assert result.metadata["failure_outcome_known"] is True
    record = health.get("fs")
    assert record["total_calls"] == 1
    assert record["failures"] == 1
    assert record["successes"] == 0
    assert record["consecutive_failures"] == 0
    assert record["status"] == "closed"


def test_health_domain_outcome_does_not_open_circuit():
    health = CapabilityHealth(failure_threshold=2)
    for _ in range(5):
        health.record_domain_outcome("fs", "missing file")
    record = health.get("fs")
    assert record["total_calls"] == 5
    assert record["failures"] == 5
    assert record["successes"] == 0
    assert record["status"] == "closed"


def test_health_infrastructure_failures_open_circuit():
    health = CapabilityHealth(failure_threshold=2)
    health.record_failure("remote", "unavailable")
    health.record_failure("remote", "unavailable")
    assert health.get("remote")["status"] == "open"


def test_missing_metadata_failure_is_still_truthful_health_failure(tmp_path):
    executor = _Executor(
        _read(),
        lambda request: CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.FAILED,
            error="legacy failure",
        ),
    )
    health = CapabilityHealth(failure_threshold=1)
    dispatcher = _dispatcher(executor, health=health)
    result = asyncio.run(dispatcher.dispatch(_request(), workspace=_workspace(tmp_path)))

    assert result.status is CapabilityResultStatus.FAILED
    assert result.error == "legacy failure"
    assert health.get("typed.read")["failures"] == 1
    assert health.get("typed.read")["status"] == "open"


def test_retry_only_for_retryable_known_outcomes(tmp_path):
    attempts = {"count": 0}

    def fail_once(request):
        attempts["count"] += 1
        raise ConnectionError("temporary dependency loss")

    executor = _Executor(_read(), fail_once)
    dispatcher = _dispatcher(executor)
    with pytest.raises(ConnectionError):
        asyncio.run(dispatcher.dispatch(_request(), workspace=_workspace(tmp_path)))
    assert attempts["count"] == 2


def test_retry_does_not_repeat_domain_or_programmer_errors(tmp_path):
    cases = [
        FileNotFoundError("missing"),
        PermissionError("denied"),
        TypeError("bad contract"),
        ValueError("invalid"),
    ]
    for exc in cases:
        attempts = {"count": 0}

        def fail(request, exc=exc):
            attempts["count"] += 1
            raise exc

        executor = _Executor(_read(), fail)
        dispatcher = _dispatcher(executor)
        with pytest.raises(type(exc)):
            asyncio.run(dispatcher.dispatch(_request(), workspace=_workspace(tmp_path)))
        assert attempts["count"] == 1


def test_default_retry_policy_is_conservative():
    from athena.protocol.capabilities import EffectClass

    write = CapabilityDescriptor(
        id="write",
        description="w",
        input_schema={"type": "object"},
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
    )
    execute = CapabilityDescriptor(
        id="execute",
        description="e",
        input_schema={"type": "object"},
        effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
    )
    external = CapabilityDescriptor(
        id="external",
        description="x",
        input_schema={"type": "object"},
        effects=frozenset({EffectClass.NETWORK_WRITE}),
    )
    read_only = CapabilityDescriptor(
        id="read",
        description="r",
        input_schema={"type": "object"},
        effects=frozenset({EffectClass.READ_LOCAL}),
    )
    assert write.resolve_retry_policy() is RetryPolicy.NEVER
    assert execute.resolve_retry_policy() is RetryPolicy.NEVER
    assert external.resolve_retry_policy() is RetryPolicy.NEVER
    assert read_only.resolve_retry_policy() is RetryPolicy.READ_ONLY
