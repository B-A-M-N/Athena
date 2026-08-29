from __future__ import annotations

from pathlib import Path

from athena.execution.manager import ExecutionManager
from athena.execution.runtimes import PythonRuntime
from athena.protocol.execution import ExecutionRequest, ExecutionExitStatus


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
