"""P1-11 regression: process-tree kills return structured outcomes.

Cleanup may stay best-effort, but shutdown must KNOW when a process tree
could not be proven dead — silence is how orphaned processes survive a
release gate.
"""

from __future__ import annotations

import subprocess
import sys
import time
import os
import signal
from types import SimpleNamespace

import pytest

from athena.execution.process_tree import ProcessKillOutcome, kill_tree, spawn_owned


def _sleep_process(seconds: float = 30) -> subprocess.Popen:
    return spawn_owned([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def test_restricted_network_requires_a_containment_root():
    with pytest.raises(RuntimeError, match="requires a workspace sandbox"):
        spawn_owned(
            [sys.executable, "-c", "pass"],
            network_policy="deny",
        )


def test_local_resource_limits_are_applied_before_runtime_start():
    process = spawn_owned(
        [
            sys.executable,
            "-c",
            "import resource; print(resource.getrlimit(resource.RLIMIT_CPU)[0])",
        ],
        resource_limits=SimpleNamespace(max_cpu_seconds=1),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        output, _stderr = process.communicate(timeout=5)
        output = output.strip()
        assert int(output) == 1
    finally:
        if process.poll() is None:
            kill_tree(process)


def test_kill_tree_proves_detached_descendant_cleanup():
    source = (
        "import subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True); "
        "print(child.pid, flush=True); time.sleep(30)"
    )
    process = spawn_owned([sys.executable, "-c", source], stdout=subprocess.PIPE, text=True)
    child_pid = int(process.stdout.readline().strip())
    outcome = kill_tree(process, timeout=1.0)
    assert outcome.proven_dead is True
    assert child_pid not in outcome.survivors


def test_kill_tree_reports_proven_death():
    process = _sleep_process(0.05)
    # A fixed sleep races interpreter startup under a loaded runner
    # (observed on -n 6: startup alone can exceed 200ms, so the child is
    # still alive and already_dead is legitimately False).  Poll for the
    # exit we intend to observe, with a generous bound.
    deadline = time.monotonic() + 10.0
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert process.poll() is not None  # child exited on its own
    outcome = kill_tree(process)
    assert isinstance(outcome, ProcessKillOutcome)
    assert outcome.proven_dead is False
    assert outcome.already_dead is True
    assert outcome.cleanup_obligation


def test_kill_tree_does_not_claim_proof_after_root_exits_before_capture():
    source = (
        "import subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True); "
        "print(child.pid, flush=True)"
    )
    process = spawn_owned([sys.executable, "-c", source], stdout=subprocess.PIPE, text=True)
    child_pid = int(process.stdout.readline().strip())
    process.wait(timeout=5)
    try:
        outcome = kill_tree(process)
        assert outcome.proven_dead is False
        assert outcome.cleanup_obligation
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.athena_claim("BHV-062")
def test_kill_tree_running_process_is_proven_dead_after_ladder():
    process = _sleep_process()
    try:
        outcome = kill_tree(process, timeout=5.0)
        assert isinstance(outcome, ProcessKillOutcome)
        assert outcome.proven_dead is True
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()


@pytest.mark.athena_claim("BHV-062")
def test_kill_tree_survivor_is_reported_not_silent(monkeypatch):
    """A process that refuses to die must come back as structured evidence
    (proven_dead=False with survivors), not as a silent best-effort miss."""
    process = _sleep_process()
    try:
        # Simulate an unkillable tree: the process never observes exit.
        monkeypatch.setattr(process, "poll", lambda: None)
        outcome = kill_tree(process, timeout=0.1)
        assert outcome.proven_dead is False
        assert outcome.already_dead is False
    finally:
        if process.poll() is None:
            process.kill()


def test_kill_tree_async_reports_proven_death():
    import asyncio

    from athena.execution.process_tree import kill_tree_async

    async def main():
        # start_new_session=True matches every production kill_tree_async
        # caller (synthesis runtime, hermes manager, synthesis engine): the
        # child gets its own process group, so the kill ladder targets the
        # child's tree, never ours.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            start_new_session=True,
        )
        outcome = await kill_tree_async(process, timeout=5.0)
        return outcome

    outcome = asyncio.run(main())
    assert isinstance(outcome, ProcessKillOutcome)
    assert outcome.proven_dead is True
