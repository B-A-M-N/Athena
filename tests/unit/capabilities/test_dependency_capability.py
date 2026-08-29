from types import SimpleNamespace
import shlex
import sys

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


class _ReplayExecution(_Execution):
    def __init__(self):
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return await super().execute(request)


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
            type(
                "Distribution",
                (),
                {
                    "metadata": {"Name": "demo"},
                    "version": _Distribution.version,
                    "read_text": lambda self, name: _Distribution.read_text(name),
                },
            )()
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

    result = await DependencyCapability().invoke(_request("inspect", name="demo"), context=context)

    assert result.status is CapabilityResultStatus.OK
    assert "1.2.3" in result.output
    assert result.metadata["lock"]["resolved_version"] == "1.2.3"


@pytest.mark.asyncio
async def test_dependency_replay_uses_exact_lock_and_verifies_environment(tmp_path, monkeypatch):
    lock_dir = tmp_path / ".athena"
    lock_dir.mkdir()
    (lock_dir / "dependencies.lock.json").write_text(
        '{"format": 1, "packages": {"demo": {'
        '"manager": "python", "resolved_version": "1.2.3", '
        '"record_hashes": ["pkg/__init__.py:sha256=abc"]}}}'
    )
    context = SimpleNamespace(workspace=WorkspaceSpec(id="repo", root=str(tmp_path)))
    execution = _ReplayExecution()
    monkeypatch.setattr(
        "athena.capabilities.dependency.resolve_dependency_environment",
        lambda *args, **kwargs: SimpleNamespace(
            to_metadata=lambda: {"environment_fingerprint": "verified"}
        ),
    )

    result = await DependencyCapability(execution).invoke(
        _request("replay", name="demo"), context=context
    )

    assert result.status is CapabilityResultStatus.OK
    assert len(execution.requests) == 1
    source = execution.requests[0].source
    assert shlex.quote(sys.executable) in source
    assert "demo==1.2.3" in source
    assert "--no-deps" in source
    assert result.metadata["provenance"]["lock_source"].endswith(".athena/dependencies.lock.json")


@pytest.mark.asyncio
async def test_dependency_replay_fails_closed_without_content_hashes(tmp_path):
    lock_dir = tmp_path / ".athena"
    lock_dir.mkdir()
    (lock_dir / "dependencies.lock.json").write_text(
        '{"format": 1, "packages": {"demo": {"manager": "python", "resolved_version": "1.2.3"}}}'
    )
    context = SimpleNamespace(workspace=WorkspaceSpec(id="repo", root=str(tmp_path)))
    execution = _ReplayExecution()

    result = await DependencyCapability(execution).invoke(
        _request("replay", name="demo"), context=context
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "content hashes" in (result.error or "")
    assert execution.requests == []


@pytest.mark.asyncio
async def test_dependency_install_rejects_container_without_host_interpreter(tmp_path):
    context = SimpleNamespace(
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path), execution_backend="container")
    )
    execution = _ReplayExecution()

    result = await DependencyCapability(execution).invoke(
        _request("install", name="demo"), context=context
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "unsupported for execution backend 'container'" in (result.error or "")
    assert execution.requests == []
