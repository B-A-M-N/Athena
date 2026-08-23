"""E2E execution against the REAL Python/Shell runtimes (not just the fake model).

The fake model only scripts WHICH capability calls to make; the code itself
(``echo hello`` / ``print(2+2)``) executes on the real ``ShellRuntime`` and
``PythonRuntime`` subprocesses through ``ExecutionManager``.

Multi-call scenarios are driven by a small script progression: the fake model
picks a script based on the accumulated user/result text, so an action script
runs first (no prior result) and a *terminal* script runs once its output
marker is present.
"""
from __future__ import annotations
import pytest

import json
import os

from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
from athena.state.mutations import COMPLETED, PLANNED


def _auto(prompt: str) -> AgentRequest:
    """A request allowed to execute/write without approval (autonomous profile)."""
    return AgentRequest(prompt=prompt, autonomy=AutonomyLevel.AUTONOMOUS)


def _cap_call(language: str, code: str) -> dict:
    return {
        "capability_id": "execute",
        "arguments": {"language": language, "code": code},
    }


def _term_after_ok() -> dict:
    """Terminate once any prior capability result succeeded (kills the loop)."""
    return {"match": {"capability_result_ok": True},
            "respond": {"text": "", "done": True}}


async def _wait_terminal(svc, task_id, target=TaskStatus.COMPLETE.value, tries=400, delay=0.02):
    from asyncio import sleep

    for _ in range(tries):
        if (await svc.get_task_status(task_id)) == target:
            return target
        await sleep(delay)
    return await svc.get_task_status(task_id)


async def _capability_outputs(svc, task_id) -> str:
    """Concatenate successful capability-result outputs recorded for the session."""
    task = await svc.get_task(task_id)
    rows = await svc._store_messages._db.fetch_all(
        "SELECT blocks FROM messages WHERE session_id = ? ORDER BY created_at ASC",
        (task.session_id,),
    )
    parts = []
    for row in rows or []:
        for b in json.loads(row["blocks"]):
            if b.get("type") == "capability_result" and b.get("ok"):
                parts.append(b.get("output") or "")
    return "\n".join(parts)


@pytest.mark.athena_claim("BHV-056")
@pytest.mark.athena_evidence("test", "e2e")
async def test_shell_execution_runs_real_runtime(make_service):
    """Model calls execute(shell, 'echo hello'); the real shell returns 'hello'."""
    svc = await make_service(scripts=[
        _term_after_ok(),
        {"match": {"user_contains": "HELLO"},
         "respond": {"capability_call": _cap_call("shell", "echo hello")}},
    ])
    task = await svc.submit(_auto("HELLO run the shell"), wait=False)
    assert await _wait_terminal(svc, task.id) == TaskStatus.COMPLETE.value
    outputs = await _capability_outputs(svc, task.id)
    assert "hello" in outputs, f"shell output missing 'hello': {outputs!r}"


@pytest.mark.athena_claim("BHV-056")
@pytest.mark.athena_evidence("test", "e2e")
async def test_python_execution_prints_4(make_service):
    svc = await make_service(scripts=[
        _term_after_ok(),
        {"match": {"user_contains": "PY_2PLUS2"},
         "respond": {"capability_call": _cap_call("python", "print(2+2)")}},
    ])
    task = await svc.submit(_auto("PY_2PLUS2 run python"), wait=False)
    assert await _wait_terminal(svc, task.id) == TaskStatus.COMPLETE.value
    outputs = await _capability_outputs(svc, task.id)
    assert "4" in outputs, f"python stdout missing '4': {outputs!r}"


@pytest.mark.athena_claim("BHV-058", "BHV-059")
@pytest.mark.athena_evidence("test", "e2e")
async def test_persistent_python_session_keeps_state(make_service):
    """Two execute calls in one task/session preserve runtime state: x=10 -> x*2==20."""
    svc = await make_service(scripts=[
        # Step 3 (final): "20" appears only after the read executes and prints it.
        {"match": {"user_contains": "20"},
         "respond": {"text": "", "done": True}},
        # Step 2: "SET" comes from step-1 output, so it runs only on turn 2.
        {"match": {"user_contains": "SET"},
         "respond": {"capability_call": _cap_call("python", "print(x*2)")}},
        # Step 1: writes x=10 in the persistent python session.
        {"match": {"user_contains": "PYPERSIST"},
         "respond": {"capability_call": _cap_call("python", "x=10; print('SET')")}},
    ])
    task = await svc.submit(_auto("PYPERSIST set then read"), wait=False)
    assert await _wait_terminal(svc, task.id) == TaskStatus.COMPLETE.value
    outputs = await _capability_outputs(svc, task.id)
    assert "20" in outputs, f"persistent x*2 must be 20: {outputs!r}"


@pytest.mark.athena_claim("BHV-052")
@pytest.mark.athena_evidence("test", "e2e")
async def test_fs_write_then_read_same_path(make_service):
    """fs.write then fs.read the same path: content on disk + mutation record."""
    svc = await make_service(scripts=[
        # Step 3 (final): "persisted" arrives once the read returns the content.
        {"match": {"user_contains": "persisted"},
         "respond": {"text": "", "done": True}},
        # Step 2: read after the write ("wrote ... bytes" is step-1 output).
        {"match": {"user_contains": "wrote"},
         "respond": {"capability_call": {
             "capability_id": "fs",
             "arguments": {"operation": "read", "path": "probe.txt"},
         }}},
        # Step 1: authoritative write -> emits "wrote N bytes".
        {"match": {"user_contains": "FSROUND"},
         "respond": {"capability_call": {
             "capability_id": "fs",
             "arguments": {"operation": "write", "path": "probe.txt",
                           "content": "persisted bytes", "create_dirs": True},
         }}},
    ])
    ws_root = os.path.realpath(svc._default_workspace.root)

    task = await svc.submit(_auto("FSROUND write probe.txt"), wait=False)

    # fs.read is not granted by the autonomous profile (default=ask), so the
    # read parks for approval; approve it to let the round-trip continue.
    from asyncio import sleep
    for _ in range(200):
        approval_id = await svc.pending_approval_id(task.id)
        if approval_id:
            await svc.approve(approval_id, granted=True, scope="call")
            break
        await sleep(0.02)

    assert await _wait_terminal(svc, task.id) == TaskStatus.COMPLETE.value

    on_disk = os.path.join(ws_root, "probe.txt")
    assert os.path.isfile(on_disk), f"fs.write did not materialise {on_disk}"
    with open(on_disk, "r", encoding="utf-8") as f:
        assert f.read() == "persisted bytes"

    outputs = await _capability_outputs(svc, task.id)
    assert "persisted bytes" in outputs, f"fs.read did not return content: {outputs!r}"

    rows = await svc._store_mutations.list_for_task(task.id)
    writes = [r for r in rows if r["operation"] == "write"]
    assert writes, f"expected a persisted write mutation, got {rows!r}"
    assert writes[0]["status"] in (COMPLETED, PLANNED)
    assert writes[0]["resource"].endswith("probe.txt")