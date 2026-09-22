"""Readiness backend resolution must match the backend execution selects."""

from __future__ import annotations

import pytest

from athena.execution.manager import ExecutionManager


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
    assert status["available"] is True


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
