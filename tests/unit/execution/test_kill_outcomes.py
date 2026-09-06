"""P1-11 regression: process-tree kills return structured outcomes.

Cleanup may stay best-effort, but shutdown must KNOW when a process tree
could not be proven dead — silence is how orphaned processes survive a
release gate.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from athena.execution.process_tree import ProcessKillOutcome, kill_tree, spawn_owned


def _sleep_process(seconds: float = 30) -> subprocess.Popen:
    return spawn_owned([sys.executable, "-c", f"import time; time.sleep({seconds})"])


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
    assert outcome.proven_dead is True
    assert outcome.already_dead is True


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
