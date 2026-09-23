"""Readiness backend resolution must match the backend execution selects."""

from __future__ import annotations

import pytest

from athena.execution.backend import BackendCapabilities
from athena.execution.manager import ExecutionManager
from athena.protocol.execution import ExecutionLimits, ExecutionRequest


class Backend:
    name = "custom"

    def __init__(self, *, available: bool):
        self.available_value = available

    def available(self) -> bool:
        return self.available_value


@pytest.mark.parametrize(
    "backend", ["local", "sandboxed-local", "shadow", "sandbox", "verification"]
)
def test_builtin_backends_resolve_explicitly(backend):
    manager = ExecutionManager()
    status = manager.resolve_backend(backend)
    assert status["backend"] == backend
    assert status["recognized"] is True
    assert status["available"] is False
    assert "no concrete runtimes" in status["error"]


async def test_builtin_backend_readiness_requires_a_concrete_runtime():
    manager = ExecutionManager()

    class Runtime:
        name = "fixture"

    manager.register_runtime(Runtime())
    local = await manager.check_backend("local")
    assert local["available"] is True
    assert local["physical_backend"] == "local-runtime-manager"


def test_builtin_aliases_expose_their_physical_boundary():
    manager = ExecutionManager()
    statuses = {item["id"]: item for item in manager.backend_status()}
    for alias in ("sandboxed-local", "shadow", "sandbox", "verification"):
        assert statuses[alias]["physical_backend"] == "local-runtime-manager"
        assert statuses[alias]["recognized"] is True
        assert statuses[alias]["proof_status"] == "unverified"


def test_unknown_backend_fails_closed_with_catalog():
    manager = ExecutionManager()
    with pytest.raises(RuntimeError, match="no such execution backend") as caught:
        manager.resolve_backend("definitely-missing")
    assert "local" in str(caught.value)


async def test_registered_unavailable_backend_fails_closed():
    manager = ExecutionManager()
    manager.register_backend(Backend(available=False))
    status = await manager.check_backend("custom")
    assert status["available"] is False


async def test_registered_available_backend_resolves_to_execution_object():
    manager = ExecutionManager()
    backend = Backend(available=True)
    manager.register_backend(backend)
    assert manager.resolve_backend("custom") is backend
    assert (await manager.check_backend("custom"))["available"] is True


async def test_local_operator_backend_is_probed_not_assumed():
    manager = ExecutionManager()
    backend = Backend(available=False)
    backend.name = "local"
    manager.set_local_backend(backend)
    assert manager.resolve_backend("local") is backend
    assert (await manager.check_backend("local"))["available"] is False


async def test_backend_without_resource_limit_enforcement_fails_closed():
    manager = ExecutionManager()

    class UnsupportedBackend:
        name = "unsupported-limits"

        def capabilities(self):
            return BackendCapabilities(supported_runtimes=("python",))

    manager.register_backend(UnsupportedBackend())
    request = ExecutionRequest(
        runtime="python",
        source="pass",
        task_id="limits",
        workspace_id="limits",
        backend="unsupported-limits",
        resource_limits=ExecutionLimits(max_cpu_seconds=1),
    )
    with pytest.raises(RuntimeError, match="does not enforce resource limits"):
        await manager.execute(request)
    with pytest.raises(RuntimeError, match="does not enforce resource limits"):
        await manager.create_session(
            task_id="limits",
            runtime="python",
            backend="unsupported-limits",
            resource_limits=ExecutionLimits(max_cpu_seconds=1),
        )
