from types import SimpleNamespace

import pytest

from athena.capabilities.dependency import DependencyCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.protocol.execution import ExecutionExitStatus, ExecutionResult
from athena.protocol.tasks import WorkspaceSpec


class _Execution:
    async def execute(self, request):
        return ExecutionResult(
            execution_id="exec-dependency",
            exit_code=0,
            status=ExecutionExitStatus.EXITED,
            stdout="installed",
        )


class _Distribution:
    version = "1.2.3"

    @staticmethod
    def read_text(name):
        assert name == "RECORD"
        return "pkg/__init__.py,sha256=abc,12\npkg-1.2.3.dist-info/METADATA,,"


def _request(operation, **arguments):
    return CapabilityRequest(
        capability_id="dependency",
        task_id="task-deps",
        call_id=f"dependency-{operation}",
        arguments={"operation": operation, **arguments},
    )


@pytest.mark.asyncio
async def test_dependency_install_writes_reproducibility_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "athena.capabilities.dependency.importlib.metadata.distributions",
        lambda path: [
            type("Distribution", (), {
                "metadata": {"Name": "demo"},
                "version": _Distribution.version,
                "read_text": lambda self, name: _Distribution.read_text(name),
            })()
        ],
    )
    context = SimpleNamespace(workspace=WorkspaceSpec(id="repo", root=str(tmp_path)))

    result = await DependencyCapability(_Execution()).invoke(
        _request("install", name="demo", version="1.2"), context=context
    )

    assert result.status is CapabilityResultStatus.OK
    lock = (tmp_path / ".athena" / "dependencies.lock.json").read_text()
    assert '"resolved_version": "1.2.3"' in lock
    assert '"task_id": "task-deps"' in lock
    assert '"pkg/__init__.py:sha256=abc"' in lock


@pytest.mark.asyncio
async def test_dependency_inspect_reports_locked_version(tmp_path):
    lock_dir = tmp_path / ".athena"
    lock_dir.mkdir()
    (lock_dir / "dependencies.lock.json").write_text(
        '{"format": 1, "packages": {"demo": {"resolved_version": "1.2.3"}}}'
    )
    context = SimpleNamespace(workspace=WorkspaceSpec(id="repo", root=str(tmp_path)))

    result = await DependencyCapability().invoke(
        _request("inspect", name="demo"), context=context
    )

    assert result.status is CapabilityResultStatus.OK
    assert "1.2.3" in result.output
    assert result.metadata["lock"]["resolved_version"] == "1.2.3"
