"""Release sandbox matrix against the actual Bubblewrap/process-tree boundary.

The higher-level capability suites exercise each product lane.  These tests
pin the shared release invariant underneath them: workspace writes are local,
toolchains are read-only, ambient secrets and denied network are absent, and
cancel terminates the owned process tree.  A missing sandbox backend is an
admission failure, never a host-execution fallback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from athena.execution.process_tree import kill_tree, sandbox_argv, spawn_owned


@pytest.mark.dsh_release
@pytest.mark.athena_claim("ATHENA-SEC-003")
@pytest.mark.athena_evidence("sandbox")
def test_release_sandbox_boundary_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe confinement, writes, network, secret hygiene, and toolchains."""
    if os.name != "posix" or shutil.which("bwrap") is None:
        pytest.fail("release sandbox matrix requires a Linux Bubblewrap backend")

    monkeypatch.setenv("ATHENA_RELEASE_SECRET", "must-not-cross-boundary")
    probe = (
        "import json, os, pathlib, socket\n"
        "workspace = pathlib.Path('/workspace')\n"
        "workspace.joinpath('allowed.txt').write_text('workspace-write')\n"
        "tmp = pathlib.Path('/tmp/private.txt')\n"
        "tmp.write_text('private-tmp')\n"
        "toolchain_read_only = False\n"
        "try:\n"
        "    pathlib.Path('/usr/bin/python3').write_bytes(b'nope')\n"
        "except (OSError, PermissionError):\n"
        "    toolchain_read_only = True\n"
        "network_denied = False\n"
        "try:\n"
        "    with socket.create_connection(('198.51.100.1', 80), timeout=0.25):\n"
        "        pass\n"
        "except OSError:\n"
        "    network_denied = True\n"
        "print(json.dumps({'secret': os.environ.get('ATHENA_RELEASE_SECRET'), "
        "'workspace': workspace.joinpath('allowed.txt').read_text(), "
        "'tmp': tmp.read_text(), 'toolchain_read_only': toolchain_read_only, "
        "'network_denied': network_denied}))\n"
    )
    process = spawn_owned(
        [sys.executable, "-c", probe],
        cwd=str(tmp_path),
        sandbox_root=str(tmp_path),
        network_policy="deny",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert result == {
        "secret": None,
        "workspace": "workspace-write",
        "tmp": "private-tmp",
        "toolchain_read_only": True,
        "network_denied": True,
    }
    assert (tmp_path / "allowed.txt").read_text(encoding="utf-8") == "workspace-write"
    assert not (tmp_path / "private.txt").exists()


@pytest.mark.dsh_release
@pytest.mark.athena_claim("ATHENA-SEC-003")
def test_release_sandbox_missing_backend_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Bubblewrap means no restricted command line can be constructed."""
    import athena.execution.process_tree as process_tree

    real_which = shutil.which
    monkeypatch.setattr(
        process_tree.shutil,
        "which",
        lambda name: None if name == "bwrap" else real_which(name),
    )
    with pytest.raises(RuntimeError, match="requires bubblewrap"):
        sandbox_argv(["true"], root=str(tmp_path))


@pytest.mark.dsh_release
@pytest.mark.athena_claim("ATHENA-SEC-004")
def test_release_cancel_kills_owned_child_tree() -> None:
    """Cancellation must reap a spawned child, not only the shell leader."""
    process = spawn_owned(
        ["bash", "-c", "sleep 60 & child=$!; echo $child; wait"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_line = process.stdout.readline() if process.stdout is not None else ""
    child_pid = int(child_line.strip())
    kill_tree(process, timeout=2.0)
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
