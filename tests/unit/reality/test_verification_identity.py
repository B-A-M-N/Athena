from athena.protocol.tasks import Criterion, VerificationSpec, VerificationType
from athena.reality.coordinator import _deduplicate_criteria
from athena.self_host.gates import SelfHostGatePolicy
from athena.verification.identity import command_proof_id, proof_subsumes, verification_proof_id


def _command(command: str, identifier: str) -> Criterion:
    return Criterion(
        id=identifier,
        description=command,
        verification=VerificationSpec(type=VerificationType.COMMAND, command=command),
    )


def test_equivalent_command_implementations_share_proof_identity():
    assert command_proof_id("pytest -q") == "python.full_tests"
    assert command_proof_id("uv run --frozen pytest -p no:cacheprovider -q") == (
        "python.full_tests"
    )
    assert command_proof_id("uv run pytest -q tests/unit/test_router.py").startswith(
        "python.affected_tests:"
    )
    assert command_proof_id("pytest -q tests/e2e/test_release_black_box.py") == "python.e2e_tests"
    assert command_proof_id("pytest -q --ignore=tests/e2e") == "python.full_tests"
    assert command_proof_id("cargo check --manifest-path native/Cargo.toml") == "rust.check"


def test_stronger_proof_subsumes_narrower_python_proof():
    assert proof_subsumes("python.full_tests", "python.affected_tests:tests/unit/test_router.py")
    assert proof_subsumes("python.dependency_environment", "python.full_tests")
    assert not proof_subsumes(
        "python.affected_tests:tests/unit/test_router.py", "python.full_tests"
    )


def test_criteria_dedup_uses_semantic_proof_identity():
    criteria = _deduplicate_criteria(
        (
            _command("pytest -q", "full"),
            _command("uv run --frozen pytest -p no:cacheprovider -q", "wrapped"),
        )
    )
    assert [criterion.id for criterion in criteria] == ["full"]


def test_low_risk_python_patch_uses_affected_tests_and_frozen_safety():
    criteria = tuple(
        _command(command, f"gate-{index}")
        for index, command in enumerate(
            (
                *SelfHostGatePolicy.REQUIRED_COMMANDS,
                *SelfHostGatePolicy.frozen_safety_commands("/repo"),
            )
        )
    )
    selected = SelfHostGatePolicy.select_criteria(
        criteria,
        changed_resources=("src/athena/cli/app.py",),
        impact={"affected_tests": ["tests/unit/cli/test_surface.py"]},
    )
    ids = {verification_proof_id(item.verification) for item in selected if item.verification}
    assert {"python.format", "python.lint", "python.types"} <= ids
    assert any(value.startswith("python.affected_tests:") for value in ids)
    assert "python.full_tests" not in ids
    assert "python.frozen_safety" in ids
    assert "architecture.frozen" in ids
    assert "rust.tests" not in ids


def test_dependency_patch_replaces_base_python_probes():
    criteria = tuple(
        _command(command, f"gate-{index}")
        for index, command in enumerate(SelfHostGatePolicy.REQUIRED_COMMANDS)
    )
    selected = SelfHostGatePolicy.select_criteria(
        criteria,
        changed_resources=("pyproject.toml",),
        task_id="dependency-test",
    )
    ids = {verification_proof_id(item.verification) for item in selected if item.verification}
    assert "python.dependency_environment" in ids
    assert "python.format" not in ids
    assert "python.lint" not in ids
    assert "python.types" not in ids
    assert "python.full_tests" not in ids


def test_high_risk_patch_keeps_full_gate_matrix():
    criteria = tuple(
        _command(command, f"gate-{index}")
        for index, command in enumerate(SelfHostGatePolicy.REQUIRED_COMMANDS)
    )
    selected = SelfHostGatePolicy.select_criteria(
        criteria,
        changed_resources=("src/athena/kernel/kernel.py",),
    )
    assert {command_proof_id(command) for command in SelfHostGatePolicy.REQUIRED_COMMANDS} <= {
        verification_proof_id(item.verification) for item in selected if item.verification
    }
