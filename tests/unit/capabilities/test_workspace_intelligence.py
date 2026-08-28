"""Workspace project-profile and change-impact capability tests."""

from __future__ import annotations

import json

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.environment import WorkspaceCapability
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
        capability_id="workspace",
        arguments={"operation": operation, **arguments},
        task_id="workspace-intelligence",
        origin=CapabilityRequestOrigin.USER_DIRECT,
    )


async def test_workspace_profile_and_impact_are_dispatcher_capabilities(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    registry = CapabilityRegistry()
    registry.register(WorkspaceCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("offline"))
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    profile = await dispatcher.dispatch(
        _request("profile"), workspace=workspace
    )
    impact = await dispatcher.dispatch(
        _request("impact", paths=["src/app.py"]), workspace=workspace
    )

    assert profile.status is CapabilityResultStatus.OK
    profile_data = json.loads(profile.output)
    assert profile_data["languages"] == ["Python"]
    assert profile_data["package_systems"] == ["pyproject"]
    assert profile_data["source_roots"] == ["src"]
    assert profile_data["test_roots"] == ["tests"]
    assert profile_data["fingerprint"]

    assert impact.status is CapabilityResultStatus.OK
    impact_data = json.loads(impact.output)
    assert impact_data["changed"] == ["src/app.py"]
    assert impact_data["impacted"][0]["path"] == "tests/test_app.py"
    assert impact_data["impacted"][0]["confidence"] == "high"


async def test_workspace_impact_rejects_empty_or_outside_inputs(tmp_path):
    registry = CapabilityRegistry()
    registry.register(WorkspaceCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine("offline"))
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    empty = await dispatcher.dispatch(
        _request("impact"), workspace=workspace
    )
    outside = await dispatcher.dispatch(
        _request("impact", paths=["/etc/hosts"]), workspace=workspace
    )

    assert empty.status is CapabilityResultStatus.FAILED
    assert "non-empty" in (empty.error or "")
    assert outside.status is CapabilityResultStatus.FAILED
    assert "outside workspace" in (outside.error or "")
