from __future__ import annotations

from pathlib import Path

from athena.execution.manager import ExecutionManager
from athena.execution.runtimes import PythonRuntime
from athena.capabilities.execute import _candidate_python_environment, _trusted_toolchain_paths
from athena.protocol.execution import ExecutionRequest, ExecutionExitStatus
from athena.protocol.tasks import NetworkPolicy, WorkspaceSpec


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
