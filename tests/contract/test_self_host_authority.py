"""Frozen-base invariants for Athena's self-host boundary.

This file is executed from the base checkout while the imports and commands
under test resolve from the candidate workspace.  It therefore stays small,
deterministic, and independent of candidate-authored unit tests.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from athena.execution.environment import VerificationEnvironment
from athena.protocol.tasks import AgentRequest
from athena.self_host.gates import SelfHostGateBundle, SelfHostGatePolicy
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
