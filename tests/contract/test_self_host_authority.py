"""Frozen-base invariants for Athena's self-host boundary.

This file is executed from the base checkout while the imports and commands
under test resolve from the candidate workspace.  It therefore stays small,
deterministic, and independent of candidate-authored unit tests.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from athena.execution.manager import ExecutionManager
from athena.execution.environment import VerificationEnvironment
from athena.execution.runtimes import ShellRuntime
from athena.protocol.execution import ExecutionExitStatus, ExecutionRequest
from athena.protocol.tasks import AgentRequest
from athena.protocol.tasks import NetworkPolicy
from athena.self_host.gates import SelfHostGateBundle, SelfHostGatePolicy
from athena.self_host.controller import SelfHostMissionController
from athena.self_host.reviewer import SelfHostIndependentReviewer
from athena.self_host.risk import SelfHostRiskClassifier
from athena.service.service import AthenaService


def test_generic_metadata_cannot_activate_self_host():
    service = AthenaService.in_memory()
    with pytest.raises(ValueError, match="reserved Athena metadata"):
        service._build_task_spec(  # noqa: SLF001 - frozen authority assertion
            AgentRequest(
                prompt="attempt boundary bypass",
                metadata={"_athena_self_host": True, "mutation_mode": "direct"},
            ),
            "contract-session",
        )


def test_generic_metadata_cannot_override_cache_namespace():
    service = AthenaService.in_memory()
    with pytest.raises(ValueError, match="reserved Athena metadata"):
        service._build_task_spec(  # noqa: SLF001 - frozen authority assertion
            AgentRequest(
                prompt="attempt cache partition bypass",
                metadata={"cache_namespace": "victim"},
            ),
            "contract-session",
        )


def test_persisted_verification_environment_is_not_authority():
    expected = VerificationEnvironment(
        project_root="/repo",
        python="/repo/.venv/bin/python",
        uv="/usr/local/bin/uv",
        environment_root="/repo/.venv",
        environment={"UV_PROJECT_ENVIRONMENT": "/repo/.venv"},
        readonly_mounts=("/repo/.venv", "/usr/local/bin/uv"),
    )
    forged = expected.to_record()
    forged["readonly_mounts"] = ["/repo/.venv", "/etc"]
    with pytest.raises(ValueError, match="untrusted mount"):
        VerificationEnvironment.from_record(forged, expected=expected)


async def test_candidate_uv_cache_write_cannot_modify_host_cache(tmp_path, monkeypatch):
    """Frozen proof boundary keeps the real operator cache outside the sandbox."""
    root = Path(__file__).resolve().parents[2]
    host_cache = tmp_path / "host-uv-cache"
    host_cache.mkdir()
    sentinel = host_cache / "operator-owned-marker"
    sentinel.write_text("unchanged", encoding="utf-8")
    monkeypatch.setenv("UV_CACHE_DIR", str(host_cache))
    verification = VerificationEnvironment.from_project(
        str(root), include_project_root=True, include_rust=True, task_id="contract-cache-proof"
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    manager = ExecutionManager()
    manager.register_runtime(ShellRuntime())
    result = await manager.execute(
        ExecutionRequest(
            runtime="shell",
            source='printf "candidate" > "$UV_CACHE_DIR/frozen-proof-marker"',
            task_id="contract-cache-proof",
            workspace_id="candidate",
            backend="shadow",
            cwd=str(candidate),
            workspace_root=str(candidate),
            network_policy=NetworkPolicy.DENY,
            env=verification.for_workspace(str(candidate)),
            toolchain_paths=verification.readonly_mounts,
            writable_toolchain_paths=verification.writable_mounts,
        )
    )
    assert result.status is ExecutionExitStatus.EXITED
    assert result.exit_code == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (host_cache / "frozen-proof-marker").exists()


def test_mandatory_self_host_boundary_is_service_owned():
    assert "uv lock --check --offline" in SelfHostGatePolicy.REQUIRED_COMMANDS
    assert any(
        command.startswith("cargo check") for command in SelfHostGatePolicy.REQUIRED_COMMANDS
    )
    assert any(
        "tests/contract" in command
        for command in SelfHostGatePolicy.frozen_safety_commands("/repo")
    )


def test_dependency_proof_runs_python_gates_in_ephemeral_environment():
    command = SelfHostGatePolicy.dependency_environment_command("dependency-proof")
    assert "uv sync --locked --offline --extra dev" in command
    assert "uv run --frozen --no-sync pytest" in command
    assert "/tmp/athena-self-proof-" in command


def test_frozen_design_context_prioritizes_core_and_supports_retrieval():
    root = Path(__file__).resolve().parents[2]
    design_files = tuple(
        {
            "path": relative,
            "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest(),
        }
        for relative in SelfHostGatePolicy.DESIGN_CONTRACTS
    )
    bundle = SelfHostGateBundle(
        source_revision="test-source",
        project_root=str(root),
        design_files=design_files,
        safety_files=(),
        required_commands=(),
        design_bundle_hash="test-design",
        gate_bundle_hash="test-gates",
    )
    context = bundle.retrieve_design_context(paths=["src/athena/self_host/gates.py"])
    assert "--- SECURITY.md ---" in context
    assert "--- SELF_HOSTING.md ---" in context
    assert "--- docs/ARCHITECTURE.md ---" in context
    assert "--- src/athena/self_host/gates.py ---" in context


def test_candidate_imports_candidate_source_when_running_in_candidate_view():
    import athena

    imported = Path(athena.__file__).resolve()
    candidate_source = Path("/workspace/src/athena").resolve()
    if candidate_source.is_dir():
        assert imported.is_relative_to(candidate_source)
    else:
        assert imported.name == "__init__.py"
        assert "src/athena" in imported.as_posix()


def test_authority_surfaces_and_certificate_deletes_are_high_risk():
    resources = [
        {"path": ".github/workflows/ci.yml", "before_hash": "a", "after_hash": "b"},
        {
            "path": "tests/contract/test_self_host_authority.py",
            "before_hash": "a",
            "after_hash": "b",
        },
        {"path": "docs/removed.md", "before_hash": "a", "after_hash": None},
    ]
    result = SelfHostRiskClassifier.classify(resources)
    assert result["level"] == "high"
    assert result["requires_independent_review"] is True


def test_integrity_review_requires_exact_frozen_authority():
    certificate = {
        "certificate_hash": "cert",
        "base_fingerprint": "base",
        "candidate_fingerprint": "candidate",
        "proof_authority": {
            "source_revision": "source",
            "design_bundle_hash": "design",
            "gate_bundle_hash": "gate",
        },
    }
    result = SelfHostIndependentReviewer.review(
        status="VERIFIED",
        certificate=certificate,
        verification=[{"id": "gate", "passed": True}],
        expected_authority={
            "source_revision": "source",
            "design_bundle_hash": "wrong",
            "gate_bundle_hash": "gate",
        },
    )
    assert result["eligible"] is False
    assert result["checks"]["proof_authority_bound"] is False


def test_planner_completion_is_only_a_proposal():
    plan = {
        "completed_work_items": [{"status": "completed", "task_id": "task-1"}],
        "phase": "PROMOTE",
    }
    item, reason, error = SelfHostMissionController.parse_planner_output(
        '{"done":true,"reason":"looks complete"}', indexed_files=set()
    )
    assert item is None
    assert error is None
    proposed = SelfHostMissionController.propose_completion(plan, reason=reason or "")
    assert proposed["phase"] == "COMPLETION_PROPOSED"
    assert "completion_proof" not in proposed
