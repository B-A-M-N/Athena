"""Dispatcher-path tests for read-only project Git intelligence."""

from __future__ import annotations

import subprocess

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.git import GitCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResultStatus,
)
from athena.protocol.tasks import WorkspaceSpec


def _request(operation: str, **arguments) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="git",
        arguments={"operation": operation, **arguments},
        task_id="git-task",
        origin=CapabilityRequestOrigin.USER_DIRECT,
    )


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Athena Tests"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "athena@example.invalid"], cwd=tmp_path, check=True
    )
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial project baseline"],
        cwd=tmp_path,
        check=True,
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")
    return source


async def test_git_observations_run_through_dispatcher(tmp_path):
    source = _repo(tmp_path)
    registry = CapabilityRegistry()
    registry.register(GitCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("offline"))
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    status = await dispatcher.dispatch(_request("status"), workspace=workspace)
    diff = await dispatcher.dispatch(_request("diff", path="module.py"), workspace=workspace)
    history = await dispatcher.dispatch(
        _request("log", path="module.py", limit=5), workspace=workspace
    )
    shown = await dispatcher.dispatch(_request("show", ref="HEAD"), workspace=workspace)
    blame = await dispatcher.dispatch(
        _request("blame", path=str(source), start=1, end=1),
        workspace=workspace,
    )
    branch = await dispatcher.dispatch(_request("branch"), workspace=workspace)
    merge_base = await dispatcher.dispatch(
        _request("merge_base", ref="HEAD", other_ref="HEAD"),
        workspace=workspace,
    )
    baseline = await dispatcher.dispatch(_request("baseline"), workspace=workspace)

    results = [status, diff, history, shown, blame, branch, merge_base, baseline]
    assert all(result.status is CapabilityResultStatus.OK for result in results)
    assert "module.py" in status.output
    assert "-VALUE = 1" in diff.output
    assert "initial project baseline" in history.output
    assert "initial project baseline" in shown.output
    assert "VALUE = 2" in blame.output
    assert merge_base.output.strip()
    assert str(tmp_path) in baseline.output


async def test_git_rejects_paths_outside_routed_workspace(tmp_path):
    registry = CapabilityRegistry()
    registry.register(GitCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("offline"))
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    result = await dispatcher.dispatch(_request("diff", path="/etc/hosts"), workspace=workspace)

    assert result.status is CapabilityResultStatus.FAILED
    assert "outside" in (result.error or "")


async def test_git_reads_candidate_worktree_with_base_git_metadata(tmp_path):
    source = _repo(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "module.py").write_text("VALUE = 99\n", encoding="utf-8")

    capability = GitCapability(
        candidate_resolver=lambda task_id: {
            "base_root": str(tmp_path),
            "candidate_root": str(candidate),
            "branch_id": "shadow-task",
        }
    )
    result = await capability.invoke(
        _request("diff", path="module.py"),
        context=type(
            "Context", (), {"workspace": WorkspaceSpec(id="candidate", root=str(candidate))}
        )(),
    )

    assert result.status is CapabilityResultStatus.OK
    assert "-VALUE = 1" in result.output
    assert "+VALUE = 99" in result.output
    assert result.metadata["candidate_view"] is True
    assert source.read_text(encoding="utf-8") == "VALUE = 2\n"
