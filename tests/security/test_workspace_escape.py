"""Path-traversal and workspace-escape invariants for the ``fs``/``execute``
capabilities.

A capability request targeting a path outside the bound workspace must never be
executed. ``FilesystemCapability._resolve`` realpaths the candidate and rejects
any path that escapes ``workspace.root``; ``ExecuteCapability._resolve_cwd``
falls back to a safe default (None) rather than let a command run outside the
workspace.
"""

from __future__ import annotations

import os

from athena.capabilities.execute import ExecuteCapability
from athena.capabilities.fs import FilesystemCapability
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
)
from athena.protocol.tasks import WorkspaceSpec


def _ws(root: str) -> WorkspaceSpec:
    return WorkspaceSpec(id="w", root=root)


def _fs(root: str) -> FilesystemCapability:
    return FilesystemCapability(workspace=_ws(root))


def _write_req(ws_root: str, path: str, task="t1") -> CapabilityRequest:
    req = CapabilityRequest(
        capability_id="fs",
        arguments={"operation": "write", "path": path, "content": "x"},
        task_id=task,
    )
    object.__setattr__(req, "call_id", "call-test")
    return req


async def test_path_traversal_write_is_denied(tmp_path):
    fs = _fs(str(tmp_path))
    result = await fs.invoke(_write_req(str(tmp_path), "../../../etc/passwd"))
    assert result.status == CapabilityResultStatus.FAILED
    assert "escape" in result.error.lower()


async def test_absolute_path_outside_workspace_is_denied(tmp_path):
    fs = _fs(str(tmp_path))
    result = await fs.invoke(_write_req(str(tmp_path), "/tmp/athena_eval_evil"))
    assert result.status == CapabilityResultStatus.FAILED
    assert "escape" in result.error.lower() or "outside" in result.error.lower()


async def test_symlink_escape_write_is_denied(tmp_path):
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)

    fs = _fs(str(tmp_path))
    result = await fs.invoke(_write_req(str(tmp_path), "link.txt"))
    # The write realpaths through the symlink, which escapes the workspace.
    assert result.status == CapabilityResultStatus.FAILED
    # The target must NOT have been modified.
    assert outside.read_text() == "secret"


async def test_directory_traversal_via_dotdot_denied(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    fs = _fs(str(tmp_path))
    result = await fs.invoke(_write_req(str(tmp_path), "sub/../../escape.txt"))
    assert result.status == CapabilityResultStatus.FAILED
    assert not (tmp_path / "escape.txt").exists()


def test_execute_relative_cwd_escape_is_rejected(tmp_path):
    cap = ExecuteCapability(None, workspace=_ws(str(tmp_path)))
    # "../../.." resolves above the workspace -> must not escape.
    assert cap._resolve_cwd(_ws(str(tmp_path)), "../../../") is None


def test_execute_absolute_cwd_outside_workspace_is_rejected(tmp_path):
    cap = ExecuteCapability(None, workspace=_ws(str(tmp_path)))
    assert cap._resolve_cwd(_ws(str(tmp_path)), "/etc") is None


def test_execute_cwd_inside_workspace_is_allowed(tmp_path):
    cap = ExecuteCapability(None, workspace=_ws(str(tmp_path)))
    inside = str(tmp_path / "sub")
    os.makedirs(inside, exist_ok=True)
    resolved = cap._resolve_cwd(_ws(str(tmp_path)), "sub")
    assert resolved is not None
    assert resolved.startswith(os.path.realpath(str(tmp_path)))