"""Complex-coding readiness gate (review item 18)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from athena.protocol.errors import ServiceNotReady

from athena.protocol.tasks import AgentRequest
from athena.service.service import AthenaService


@pytest.fixture
async def svc():
    s = AthenaService.in_memory()
    await s.start()
    yield s
    await s.stop()


async def test_non_complex_task_skips_readiness_check(svc):
    spec = svc._build_task_spec(
        AgentRequest(prompt="tell me a joke"),
        "session- readiness",
    )
    result = await svc.check_complex_coding_readiness(spec)
    assert result["required"] is False
    assert result["ready"] is True


async def test_complex_task_readiness_with_stores(svc):
    spec = svc._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem and run all tests",
        ),
        "session-readiness-complex",
    )
    result = await svc.check_complex_coding_readiness(spec)
    assert result["required"] is True
    # After startup, core infrastructure is ready; gaps may be empty.
    assert isinstance(result["gaps"], list)


async def test_complex_task_detects_missing_dispatcher_as_required_gap(svc):
    svc._dispatcher = None
    spec = svc._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem and run all tests",
        ),
        "session-readiness-gap",
    )
    result = await svc.check_complex_coding_readiness(spec)
    assert result["required"] is True
    assert not result["ready"]
    assert "dispatcher" in [g["check"] for g in result["required_gaps"]]


async def test_complex_submission_fails_closed_on_required_readiness_gap(svc):
    svc._dispatcher = None
    spec = svc._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem and run all tests",
        ),
        "session-readiness-fail-closed",
    )
    with pytest.raises(ServiceNotReady) as caught:
        await svc.submit_spec(spec)
    assert "dispatcher" in caught.value.data["missing"]


async def test_complex_submission_fails_closed_without_execute_runtime(svc):
    svc._dispatcher.registry.unregister("execute")
    spec = svc._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem and run all tests",
        ),
        "session-readiness-no-runtime",
    )
    with pytest.raises(ServiceNotReady) as caught:
        await svc.submit_spec(spec)
    assert "execution_runtime" in caught.value.data["missing"]


async def test_complex_readiness_requires_policy_clone_backend_and_proof(svc):
    spec = svc._build_task_spec(
        AgentRequest(prompt="refactor the authentication subsystem and run all tests"),
        "session-readiness-required-probes",
    )
    result = await svc.check_complex_coding_readiness(spec)
    checks = {item["check"] for item in result["required_gaps"]}

    # Existing behavior remains: missing runtime is required.
    assert "dispatcher" not in checks
    assert "execution_runtime" not in checks
    # New mandatory audit probes are reported when they cannot be proven.
    assert "verification_policy" in checks
    assert "independent_proof" in checks

    # A policy denial is not inferred from a tuple: '*' denies execute too.
    restricted = replace(spec, capability_policy=replace(spec.capability_policy, deny=("*",)))
    result = await svc.check_complex_coding_readiness(restricted)
    assert "verification_policy" in {item["check"] for item in result["required_gaps"]}


async def test_complex_readiness_detects_unwritable_shadow_state(svc, tmp_path):
    spec = svc._build_task_spec(
        AgentRequest(prompt="refactor the authentication subsystem and run all tests"),
        "session-readiness-clone",
    )
    shadow = svc.shadow_engine()
    state_root = Path(shadow._state_root)
    sentinel = state_root / "readiness-probe"
    sentinel.write_text("x", encoding="utf-8")
    state_root.chmod(0o500)
    try:
        result = await svc.check_complex_coding_readiness(spec)
    finally:
        state_root.chmod(0o700)
        sentinel.unlink(missing_ok=True)
    assert any(item["check"] == "candidate_workspace" for item in result["required_gaps"])


async def test_complex_readiness_fails_closed_for_unknown_backend(svc):
    """Readiness must use the same backend authority execution will use."""
    from athena.protocol.tasks import AgentRequest, WorkspaceSpec

    spec = svc._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem and run all tests",
            workspace=WorkspaceSpec(
                id="backend-gap",
                root=str(Path.cwd()),
                execution_backend="definitely-missing",
            ),
        ),
        "session-readiness-backend-missing",
    )
    result = await svc.check_complex_coding_readiness(spec)
    assert "execution_backend" in {item["check"] for item in result["required_gaps"]}


async def test_complex_readiness_probes_actual_clone_transport(svc, monkeypatch):
    """A successful manifest is insufficient when the clone operation fails."""
    from athena.shadow.engine import ShadowEngine

    spec = svc._build_task_spec(
        AgentRequest(prompt="refactor the authentication subsystem and run all tests"),
        "session-readiness-clone-failure",
    )

    async def failed_clone(self):
        raise RuntimeError("checkpoint clone worker failed")

    monkeypatch.setattr(ShadowEngine, "preflight_clone_transport", failed_clone)
    result = await svc.check_complex_coding_readiness(spec)
    assert any(
        item["check"] == "candidate_workspace"
        and "checkpoint clone worker failed" in item["detail"]
        for item in result["required_gaps"]
    )


async def test_complex_readiness_fails_closed_for_invalid_persisted_autonomy(svc):
    from athena.protocol.tasks import AgentRequest

    spec = svc._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem and run all tests",
            metadata={"autonomy": "offlien"},
        ),
        "session-readiness-invalid-autonomy",
    )
    result = await svc.check_complex_coding_readiness(spec)
    assert any(
        item["check"] == "verification_policy" and "invalid task autonomy" in item["detail"]
        for item in result["required_gaps"]
    )


@pytest.mark.parametrize(
    "backend", ["local", "sandboxed-local", "shadow", "sandbox", "verification"]
)
async def test_readiness_and_execution_backend_selection_parity(svc, backend):
    from athena.protocol.tasks import AgentRequest, WorkspaceSpec

    spec = svc._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem and run all tests",
            workspace=WorkspaceSpec(
                id="backend-parity",
                root=str(Path.cwd()),
                execution_backend=backend,
            ),
        ),
        f"session-backend-parity-{backend}",
    )
    execution_status = await svc._execution.check_backend(backend)
    result = await svc.check_complex_coding_readiness(spec)
    backend_gap = any(item["check"] == "execution_backend" for item in result["required_gaps"])
    assert execution_status["available"] is not backend_gap


async def test_readiness_registered_backend_matches_execution_availability(svc):
    from athena.protocol.tasks import AgentRequest, WorkspaceSpec

    class _Backend:
        name = "custom"

        def available(self):
            return False

    svc._execution.register_backend(_Backend())
    spec = svc._build_task_spec(
        AgentRequest(
            prompt="refactor the authentication subsystem and run all tests",
            workspace=WorkspaceSpec(
                id="backend-registered",
                root=str(Path.cwd()),
                execution_backend="custom",
            ),
        ),
        "session-backend-registered",
    )
    result = await svc.check_complex_coding_readiness(spec)
    assert "execution_backend" in {item["check"] for item in result["required_gaps"]}
