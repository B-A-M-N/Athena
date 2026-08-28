"""Fork invariants: lineage, event-prefix boundaries, approval non-transfer.

Pins contracts of ``athena.causal`` beyond FORK-001..003:

* fork boundary validation rejects out-of-range event sequences and leaves
  no durable trace on refusal;
* the causal reconstruction digest binds to the event PREFIX (same boundary
  -> same digest, parent timeline growth does not change it, a different
  boundary changes it);
* forking never mutates the parent task row or its event log, and the fork
  session records its causal lineage;
* the cloned transcript excludes messages created after the boundary event,
  and copied messages carry auditable source metadata;
* a consumed CALL-scoped approval does not transfer to the fork task, and
  the durable approval ledger of the fork starts clean;
* a failed checkpoint materialization removes the speculative fork workspace
  and session.
"""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

import athena.causal.fork as fork_module
from athena.causal import CheckpointManager, TaskForker
from athena.policy.approvals import args_digest
from athena.protocol.capabilities import EffectClass
from athena.protocol.ids import new_id
from athena.protocol.messages import (
    Message,
    Provenance,
    Role,
    SourceType,
    TextBlock,
)
from athena.protocol.policy import ApprovalScope, PolicyRequest, Principal
from athena.protocol.tasks import AgentRequest, WorkspaceSpec
from athena.service.service import AthenaService


@pytest.fixture
async def svc():
    service = AthenaService.in_memory()
    await service.start()
    try:
        yield service
    finally:
        try:
            await service.stop()
        except Exception:
            pass


async def _task(svc, prompt="fork-invariants"):
    return await svc.submit(AgentRequest(prompt=prompt), wait=True)


def _digest(svc_row):
    return svc_row["metadata"]["causal_reconstruction"]["event_prefix_sha256"]


async def test_fork_rejects_out_of_range_event_sequences(svc):
    task = await _task(svc)
    last = await svc._store_events.last_sequence(task.id)
    forker = TaskForker(service=svc)

    with pytest.raises(ValueError, match="ends at event"):
        await forker.fork(task_id=task.id, after_event_sequence=last + 5)
    with pytest.raises(ValueError, match="non-negative"):
        await forker.fork(task_id=task.id, after_event_sequence=-1)

    # Refusals must not leave a forked task or speculative session behind.
    assert await svc._store_tasks.list_children(task.id) == []
    sessions = {row["id"] for row in await svc._sessions.list_all()}
    parent_session = (await svc._store_tasks.get(task.id))["session_id"]
    fork_sessions = {s for s in sessions if s != parent_session}
    assert fork_sessions == set()


async def test_fork_prefix_digest_binds_to_event_prefix(svc):
    task = await _task(svc)
    last = await svc._store_events.last_sequence(task.id)
    forker = TaskForker(service=svc)

    fork1 = await forker.fork(task_id=task.id, after_event_sequence=last)
    fork2 = await forker.fork(task_id=task.id, after_event_sequence=last)
    row1 = await svc._store_tasks.get(fork1["fork_id"])
    row2 = await svc._store_tasks.get(fork2["fork_id"])
    # Same boundary -> identical event-prefix digest.
    assert _digest(row1) == _digest(row2)

    # Parent timeline grows after the forks were taken...
    await svc._store_events.append_event("TASK_NOTE", {"note": "post-fork"}, task_id=task.id)
    # ...but the digest at the OLD boundary is unchanged (prefix, not tip).
    fork3 = await forker.fork(task_id=task.id, after_event_sequence=last)
    row3 = await svc._store_tasks.get(fork3["fork_id"])
    assert _digest(row3) == _digest(row1)

    # A different boundary covers a different event set -> different digest.
    fork4 = await forker.fork(task_id=task.id, after_event_sequence=last + 1)
    row4 = await svc._store_tasks.get(fork4["fork_id"])
    assert _digest(row4) != _digest(row1)


async def test_fork_never_mutates_parent_task_or_event_log(svc):
    task = await _task(svc)
    events_before = await svc._store_events.list_for_task(task.id)
    parent_before = await svc._store_tasks.get(task.id)
    last = await svc._store_events.last_sequence(task.id)

    fork = await TaskForker(service=svc).fork(task_id=task.id, after_event_sequence=last)

    assert await svc._store_events.list_for_task(task.id) == events_before
    assert await svc._store_tasks.get(task.id) == parent_before

    # The fork is independent durable work with a causally linked session.
    fork_id = fork["fork_id"]
    assert await svc._store_events.last_sequence(fork_id) > 0
    fork_row = await svc._store_tasks.get(fork_id)
    fork_session = fork_row["metadata"]["fork_session_id"]
    assert fork_session and fork_session != parent_before["session_id"]
    session = await svc._sessions.get(fork_session)
    assert session["parent_id"] == parent_before["session_id"]
    assert session["metadata"]["causal_fork_of_task"] == task.id
    assert session["metadata"]["causal_fork_after_event"] == last


async def test_fork_transcript_excludes_messages_after_boundary(svc):
    task = await _task(svc)
    parent_session = (await svc._store_tasks.get(task.id))["session_id"]
    first = (await svc._store_events.list_for_task(task.id))[0]

    def _message(text, when):
        return Message(
            id=new_id("msg"),
            role=Role.USER,
            blocks=(TextBlock(text=text),),
            created_at=when,
            # NOTE: ``trust`` must stay the default TrustClass member. The
            # serializer (state/sessions.py:187) calls ``.value`` on it, so
            # a plain str value would crash the append (see report).
            provenance=Provenance(source_type=SourceType.USER),
        )

    boundary_ts = first.timestamp
    early = _message("early-message", boundary_ts - timedelta(seconds=1))
    late = _message("late-message", boundary_ts + timedelta(seconds=1))
    await svc._store_messages.append_to_session(parent_session, early)
    await svc._store_messages.append_to_session(parent_session, late)

    fork = await TaskForker(service=svc).fork(task_id=task.id, after_event_sequence=first.sequence)
    fork_session = (await svc._store_tasks.get(fork["fork_id"]))["metadata"]["fork_session_id"]
    msgs = await svc._store_messages.list_session_messages(fork_session)
    texts = [b.text for m in msgs for b in m.blocks if hasattr(b, "text")]

    assert "early-message" in texts, "message before the boundary is copied"
    assert "late-message" not in texts, (
        "message created after the boundary event must never be copied"
    )

    # The copy is auditable: new id, source message linked.
    cloned = next(m for m in msgs if "early-message" in getattr(m.blocks[0], "text", ""))
    assert cloned.id != early.id
    assert cloned.metadata["causal_fork_source_message"] == early.id
    assert cloned.metadata["causal_fork_source_session"] == parent_session


async def test_consumed_call_approval_does_not_transfer_to_fork(svc):
    task = await _task(svc)
    parent_session = (await svc._store_tasks.get(task.id))["session_id"]
    del parent_session  # unused; kept clarity of scope to task ids below

    manager = svc._policy.approvals
    principal = Principal("agent", "athena")
    arguments = {"operation": "read", "path": "a.txt"}
    approval_id = manager.create_request(
        principal,
        ApprovalScope.CALL,
        capability="fs",
        task_id=task.id,
        args_digest=args_digest(arguments),
        call_id="call-parent",
    )
    manager.grant(approval_id)

    last = await svc._store_events.last_sequence(task.id)
    fork = await TaskForker(service=svc).fork(task_id=task.id, after_event_sequence=last)
    fork_id = fork["fork_id"]

    def _request(task_id, call_id):
        return PolicyRequest(
            principal=principal,
            task_id=task_id,
            capability_id="fs",
            arguments=dict(arguments),
            workspace=WorkspaceSpec(id="w", root="/tmp"),
            execution_backend="local",
            effects=frozenset({EffectClass.READ_LOCAL}),
            call_id=call_id,
        )

    # The fork task cannot use the parent's (still unconsumed) grant:
    # grant task binding refuses the transfer.
    assert manager.covers_request(_request(fork_id, "call-fork")) is None

    # The parent's own call consumes the single-use grant...
    assert manager.covers_request(_request(task.id, "call-parent")) is not None
    # ...and neither the parent's next call nor the fork can replay it.
    assert manager.covers_request(_request(task.id, "call-parent-2")) is None
    assert manager.covers_request(_request(fork_id, "call-fork")) is None

    # Durable ledger: granted approvals stay bound to the parent task row;
    # the fork starts with a clean approval ledger.
    durable = svc._store_approvals
    dur_id = await durable.create_request(
        task_id=task.id, capability_id="fs", arguments=dict(arguments)
    )
    await durable.record_grant(dur_id, resolver="user", scope="call")
    assert await durable.list_for_task(fork_id) == []
    parent_rows = await durable.list_for_task(task.id)
    assert dur_id in {r["id"] for r in parent_rows}
    granted = [r for r in parent_rows if r["id"] == dur_id]
    assert granted[0]["status"] == "GRANTED"


async def test_fork_checkpoint_materialize_failure_leaves_no_trace(svc, tmp_path, monkeypatch):
    ws = WorkspaceSpec(id="w", root=str(tmp_path / "ws"))
    (Path(ws.root)).mkdir(parents=True, exist_ok=True)
    task = await svc.submit(AgentRequest(prompt="ckpt-fail", workspace=ws), wait=True)

    created_dirs: list[str] = []
    original_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        created_dirs.append(path)
        return path

    monkeypatch.setattr(fork_module.tempfile, "mkdtemp", tracking_mkdtemp)

    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))

    async def failing_materialize(checkpoint_id, workspace_root):
        raise RuntimeError("simulated materialization failure")

    monkeypatch.setattr(mgr, "materialize", failing_materialize)

    sessions_before = {row["id"] for row in await svc._sessions.list_all()}

    with pytest.raises(RuntimeError, match="simulated materialization failure"):
        await TaskForker(service=svc, checkpoint_manager=mgr).fork(
            task_id=task.id,
            after_event_sequence=await svc._store_events.last_sequence(task.id),
            workspace_checkpoint_id="ckpt_broken",
        )

    # The speculative fork workspace was removed...
    assert created_dirs, "fork attempted to create a fork workspace"
    assert all(not os.path.exists(d) for d in created_dirs)
    # ...and no orphaned fork session survived.
    assert {row["id"] for row in await svc._sessions.list_all()} == sessions_before
