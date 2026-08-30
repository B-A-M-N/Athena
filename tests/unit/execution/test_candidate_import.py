from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.execution.manager import ExecutionManager
from athena.execution.runtimes import PythonRuntime
from athena.execution.runtimes import ShellRuntime
from athena.capabilities.execute import (
    ExecuteCapability,
    _candidate_python_environment,
    _trusted_toolchain_paths,
)
from athena.execution.environment import VerificationEnvironment
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyDecision, PolicyVerdict
from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.execution import ExecutionRequest, ExecutionExitStatus
from athena.protocol.tasks import NetworkPolicy, WorkspaceSpec
from athena.self_host.gates import SelfHostGatePolicy


class _AllowEngine:
    approvals = None

    def evaluate(self, request, *, autonomy=None):
        return PolicyDecision(PolicyVerdict.ALLOW, "test allow", "test.allow", ())


async def test_python_verification_imports_candidate_src_and_reports_candidate_file(tmp_path):
    candidate = tmp_path / "candidate"
    source = candidate / "src" / "athena"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("MARKER = 'candidate'\n", encoding="utf-8")
    manager = ExecutionManager()
    manager.register_runtime(PythonRuntime())

    result = await manager.execute(
        ExecutionRequest(
            runtime="python",
            source="import athena; print(athena.__file__)",
            task_id="candidate-import",
            workspace_id="candidate",
            backend="local",
            cwd=str(candidate),
            env={"PYTHONPATH": str(candidate / "src")},
        )
    )

    assert result.status is ExecutionExitStatus.EXITED
    assert result.exit_code == 0
    assert str(source / "__init__.py") in result.stdout
    assert Path(result.stdout.strip()).resolve() == (source / "__init__.py").resolve()


async def test_python_verification_sandbox_imports_candidate_and_uses_trusted_toolchain(tmp_path):
    candidate = tmp_path / "candidate"
    source = candidate / "src" / "example"
    source.mkdir(parents=True)
    module = source / "__init__.py"
    module.write_text("VALUE = 'CANDIDATE'\n", encoding="utf-8")
    workspace = WorkspaceSpec(
        id="candidate",
        root=str(candidate),
        execution_backend="shadow",
        network_policy=NetworkPolicy.DENY,
    )
    manager = ExecutionManager()
    manager.register_runtime(PythonRuntime())

    result = await manager.execute(
        ExecutionRequest(
            runtime="python",
            source="import example; print(example.VALUE); print(example.__file__)",
            task_id="candidate-sandbox-import",
            workspace_id="candidate",
            backend="shadow",
            cwd=str(candidate),
            workspace_root=str(candidate),
            network_policy=NetworkPolicy.DENY,
            env=_candidate_python_environment(str(candidate)),
            toolchain_paths=_trusted_toolchain_paths(workspace),
        )
    )

    assert result.status is ExecutionExitStatus.EXITED
    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == "CANDIDATE"
    assert result.stdout.splitlines()[1] == "/workspace/src/example/__init__.py"


async def test_candidate_sandbox_can_launch_operator_selected_uv(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    workspace = WorkspaceSpec(
        id="candidate",
        root=str(candidate),
        execution_backend="shadow",
        network_policy=NetworkPolicy.DENY,
    )
    manager = ExecutionManager()
    from athena.execution.runtimes import ShellRuntime

    manager.register_runtime(ShellRuntime())

    result = await manager.execute(
        ExecutionRequest(
            runtime="shell",
            source="uv --version",
            task_id="candidate-sandbox-uv",
            workspace_id="candidate",
            backend="shadow",
            cwd=str(candidate),
            workspace_root=str(candidate),
            network_policy=NetworkPolicy.DENY,
            toolchain_paths=_trusted_toolchain_paths(workspace),
        )
    )

    assert result.status is ExecutionExitStatus.EXITED
    assert result.exit_code == 0
    assert result.stdout.startswith("uv ")


async def test_self_host_sandbox_exposes_frozen_python_and_rust_toolchains(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    project_root = Path(__file__).resolve().parents[3]
    for name in ("pyproject.toml", "uv.lock"):
        (candidate / name).write_text(
            (project_root / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    verification = VerificationEnvironment.from_project(
        str(project_root), include_project_root=True, include_rust=True
    )
    manager = ExecutionManager()
    manager.register_runtime(ShellRuntime())

    result = await manager.execute(
        ExecutionRequest(
            runtime="shell",
            source=("uv run --frozen --no-sync python --version; cargo --version; rustc --version"),
            task_id="candidate-toolchains",
            workspace_id="candidate",
            backend="shadow",
            cwd=str(candidate),
            workspace_root=str(candidate),
            network_policy=NetworkPolicy.DENY,
            env=verification.for_workspace(str(candidate)),
            toolchain_paths=verification.readonly_mounts,
        )
    )

    assert result.status is ExecutionExitStatus.EXITED
    assert result.exit_code == 0
    assert "Python " in result.stdout
    assert "cargo " in result.stdout
    assert "rustc " in result.stdout


async def test_self_host_sandbox_runs_locked_offline_cargo_check(tmp_path):
    """Rust proof must link inside the actual network-denied candidate sandbox."""
    project_root = Path(__file__).resolve().parents[3]
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    shutil.copytree(project_root / "native", candidate / "native")
    verification = VerificationEnvironment.from_project(
        str(project_root), include_project_root=True, include_rust=True
    )
    manager = ExecutionManager()
    manager.register_runtime(ShellRuntime())

    result = await manager.execute(
        ExecutionRequest(
            runtime="shell",
            source="cargo check --manifest-path native/Cargo.toml --locked --offline",
            task_id="candidate-cargo-check",
            workspace_id="candidate",
            backend="shadow",
            cwd=str(candidate),
            workspace_root=str(candidate),
            network_policy=NetworkPolicy.DENY,
            env=verification.for_workspace(str(candidate)),
            toolchain_paths=verification.readonly_mounts,
        )
    )

    assert result.status is ExecutionExitStatus.EXITED
    assert result.exit_code == 0, result.stderr


async def test_self_host_dependency_proof_fails_closed_for_broken_candidate(tmp_path):
    """Dependency proof must reject candidate metadata instead of using base venv."""
    project_root = Path(__file__).resolve().parents[3]
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    shutil.copytree(project_root / "src", candidate / "src")
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        shutil.copy2(project_root / name, candidate / name)
    manifest = candidate / "pyproject.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '"httpx>=0.27",', '"definitely-not-a-real-athena-package-xyz>=1",'
        ),
        encoding="utf-8",
    )
    verification = VerificationEnvironment.from_project(
        str(project_root), include_project_root=True, include_rust=True
    )
    manager = ExecutionManager()
    manager.register_runtime(ShellRuntime())

    result = await manager.execute(
        ExecutionRequest(
            runtime="shell",
            source=SelfHostGatePolicy.dependency_environment_command("candidate-dependency-proof"),
            task_id="candidate-dependency-proof",
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

    assert result.exit_code != 0


async def test_candidate_proof_uses_execute_dispatcher_sandbox_and_uv(tmp_path):
    """The real proof path must import the candidate and run its frozen toolchain."""
    candidate = tmp_path / "candidate"
    source = candidate / "src" / "example"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VALUE = 'CANDIDATE'\n", encoding="utf-8")
    project_root = Path(__file__).resolve().parents[3]
    (candidate / "pyproject.toml").write_text(
        (project_root / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (candidate / "uv.lock").write_text(
        (project_root / "uv.lock").read_text(encoding="utf-8"), encoding="utf-8"
    )
    verification = VerificationEnvironment.from_project(str(project_root))
    workspace = WorkspaceSpec(
        id="candidate",
        root=str(candidate),
        execution_backend="shadow",
        network_policy=NetworkPolicy.DENY,
    )
    manager = ExecutionManager()
    manager.register_runtime(PythonRuntime())
    manager.register_runtime(ShellRuntime())
    registry = CapabilityRegistry()
    registry.register(ExecuteCapability(manager))
    dispatcher = CapabilityDispatcher(registry, _AllowEngine())

    imported = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="execute",
            arguments={
                "language": "python",
                "code": "import example; print(example.VALUE); print(example.__file__)",
            },
            task_id="candidate-proof",
        ),
        workspace=workspace,
        verification_environment=verification,
    )
    toolchain = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="execute",
            arguments={"language": "shell", "code": "uv --version"},
            task_id="candidate-proof",
        ),
        workspace=workspace,
        verification_environment=verification,
    )

    assert imported.status.value == "ok"
    assert imported.output.splitlines()[0] == "CANDIDATE"
    assert imported.output.splitlines()[1] == "/workspace/src/example/__init__.py"
    assert toolchain.status.value == "ok"
    assert toolchain.output.startswith("uv ")
    assert not (candidate / ".venv").exists()


def test_verification_environment_rejects_forged_host_mount():
    with pytest.raises(ValueError, match="untrusted mount"):
        VerificationEnvironment.from_record(
            {
                "project_root": "/project",
                "python": "/project/.venv/bin/python",
                "uv": "/usr/local/bin/uv",
                "environment_root": "/project/.venv",
                "environment": {"UV_PROJECT_ENVIRONMENT": "/project/.venv"},
                "readonly_mounts": ["/etc"],
            }
        )


def test_verification_environment_rejects_foreign_base_root():
    with pytest.raises(ValueError, match="base root does not match"):
        VerificationEnvironment.from_record(
            {
                "project_root": "/project",
                "base_root": "/other",
                "python": "/project/.venv/bin/python",
                "uv": "/usr/local/bin/uv",
                "environment_root": "/project/.venv",
                "environment": {"UV_PROJECT_ENVIRONMENT": "/project/.venv"},
                "readonly_mounts": ["/project/.venv", "/usr/local/bin/uv"],
            }
        )


def test_verification_environment_record_must_match_service_identity():
    expected = VerificationEnvironment(
        project_root="/project",
        python="/project/.venv/bin/python",
        uv="/usr/local/bin/uv",
        environment_root="/project/.venv",
        environment={"UV_PROJECT_ENVIRONMENT": "/project/.venv"},
        readonly_mounts=("/project/.venv", "/usr/local/bin/uv"),
    )
    forged = expected.to_record()
    forged["environment"] = {
        "UV_PROJECT_ENVIRONMENT": "/project/.venv",
        "PYTHONDONTWRITEBYTECODE": "0",
    }

    with pytest.raises(ValueError, match="trusted service identity"):
        VerificationEnvironment.from_record(forged, expected=expected)


async def test_standalone_verifier_does_not_hydrate_private_metadata():
    from athena.kernel.verifiers import CompositeVerifier
    from athena.protocol.tasks import Criterion, TaskSpec, VerificationSpec, VerificationType

    task = TaskSpec(
        id="private-record",
        objective="test",
        metadata={
            "_athena_verification_environment": {
                "project_root": "/project",
                "python": "/project/.venv/bin/python",
                "uv": "/usr/local/bin/uv",
                "environment_root": "/project/.venv",
                "environment": {"UV_PROJECT_ENVIRONMENT": "/project/.venv"},
                "readonly_mounts": ["/project/.venv", "/usr/local/bin/uv"],
            }
        },
    )
    criterion = Criterion(
        id="command",
        description="command:true",
        verification=VerificationSpec(type=VerificationType.COMMAND, command="true"),
    )

    verifier = CompositeVerifier()
    result = await verifier.verify(task, (criterion,))
    assert result == [False]
