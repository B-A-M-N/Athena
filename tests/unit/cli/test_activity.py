"""OI activity model tests (UI mission §16/§17/§25/§32).

Guarantees: one logical operation is updated in place across its lifecycle
(no row-per-event duplication), sections stay distinct (active vs recent vs
approval vs artifacts vs background), and cleanup is deterministic.
"""

from __future__ import annotations

from athena.cli.activity import ActivityModel, OpState


def _run_execute_lifecycle(act: ActivityModel, call_id: str = "c1") -> None:
    args = {"language": "shell", "code": "git status"}
    act.observe("CapabilityRequested",
                {"capability_id": "execute", "call_id": call_id, "arguments": args})
    act.observe("CapabilityStarted", {"capability_id": "execute", "call_id": call_id})
    act.observe("StdoutChunk", {"call_id": call_id, "data": "on branch main\n"})
    act.observe("CapabilityCompleted", {"capability_id": "execute", "call_id": call_id})


def test_operation_updated_in_place_not_duplicated():
    """Lifecycle events update ONE operation record — never append rows (§25)."""
    act = ActivityModel()
    _run_execute_lifecycle(act)
    # exactly one operation total: archived to recent, none left active
    assert act.active is None
    assert len(act.recent) == 1
    op = act.recent[0]
    assert op.state == OpState.DONE
    assert op.capability == "execute"
    assert "git status" in op.summary
    assert any("on branch main" in ln for ln in op.output_tail)


def test_active_operation_visible_while_running():
    act = ActivityModel()
    act.observe("CapabilityRequested",
                {"capability_id": "execute", "arguments": {"code": "make test"}})
    act.observe("CapabilityStarted", {"capability_id": "execute"})
    assert act.active is not None
    assert act.active.state == OpState.RUNNING
    assert "make test" in act.current_label()


def test_failed_operation_records_reason():
    act = ActivityModel()
    act.observe("CapabilityRequested", {"capability_id": "execute"})
    act.observe("CapabilityFailed", {"capability_id": "execute", "reason": "boom"})
    assert act.active is None
    assert act.recent[0].state == OpState.FAILED
    assert "boom" in act.recent[0].detail


def test_stderr_is_marked_in_output_tail():
    act = ActivityModel()
    act.observe("CapabilityRequested", {"capability_id": "execute"})
    act.observe("StderrChunk", {"data": "warn: something\n"})
    assert act.active is not None
    assert any(ln.startswith("! ") for ln in act.active.output_tail)


def test_output_tail_bounded_with_drop_counter():
    act = ActivityModel()
    act.observe("CapabilityRequested", {"capability_id": "execute"})
    for i in range(40):
        act.observe("StdoutChunk", {"data": f"line {i}\n"})
    op = act.active
    assert op is not None
    assert len(op.output_tail) <= 6
    assert op.output_dropped > 0


def test_approval_card_created_and_operation_pauses():
    act = ActivityModel()
    act.observe("CapabilityRequested", {"capability_id": "fs.write"})
    act.observe("ApprovalRequested", {
        "approval_id": "a1", "capability_id": "fs.write",
        "scopes": ["call", "task"], "reason": "write outside workspace",
    })
    assert act.approval is not None
    assert act.approval.capability == "fs.write"
    assert act.approval.scopes == ["call", "task"]
    assert "outside workspace" in act.approval.reason
    assert act.active is not None and act.active.state == OpState.WAITING
    assert "awaiting approval" in act.current_label()


def test_approval_resolved_approved_resumes_operation():
    act = ActivityModel()
    act.observe("CapabilityRequested", {"capability_id": "fs.write"})
    act.observe("ApprovalRequested", {"approval_id": "a1", "capability_id": "fs.write"})
    act.observe("ApprovalResolved", {"approval_id": "a1", "decision": "approved"})
    assert act.approval is None
    assert act.active is not None and act.active.state == OpState.RUNNING


def test_approval_resolved_denied_cancels_operation():
    act = ActivityModel()
    act.observe("CapabilityRequested", {"capability_id": "fs.write"})
    act.observe("ApprovalRequested", {"approval_id": "a1", "capability_id": "fs.write"})
    act.observe("ApprovalResolved", {"approval_id": "a1", "decision": "denied"})
    assert act.approval is None
    assert act.active is None
    assert act.recent[0].state == OpState.CANCELLED
    assert "denied" in act.recent[0].detail


def test_artifact_event_recorded_with_producer():
    act = ActivityModel()
    act.observe("CapabilityRequested", {"capability_id": "execute"})
    act.observe("ArtifactCreated", {"uri": "file:///tmp/report.md", "name": "report.md"})
    assert len(act.artifacts) == 1
    art = act.artifacts[0]
    assert art.name == "report.md"
    assert "report.md" in art.ref
    assert art.operation == "execute"
    assert act.active is not None and act.active.has_artifact


def test_background_task_lifecycle_and_attention():
    act = ActivityModel()
    act.observe("ChildTaskCreated", {"child_task_id": "t9", "objective": "scan deps"})
    assert "t9" in act.background
    act.observe("ChildTaskCompleted", {"child_task_id": "t9", "status": "FAILED"})
    bg = act.background["t9"]
    assert bg.state == "failed"
    assert bg.needs_attention


def test_task_terminal_retires_active_operation():
    act = ActivityModel()
    act.observe("CapabilityRequested", {"capability_id": "execute"})
    act.observe("TaskFailed", {})
    assert act.active is None
    assert act.recent[0].state == OpState.FAILED
    assert act.task_status == "failed"


def test_concurrent_operations_keyed_by_call_id():
    """Two overlapping executions stay separate operations (§18)."""
    act = ActivityModel()
    act.observe("CapabilityRequested",
                {"capability_id": "execute", "call_id": "A", "arguments": {"code": "one"}})
    act.observe("StdoutChunk", {"call_id": "A", "data": "out-A\n"})
    act.observe("CapabilityCompleted", {"call_id": "A", "capability_id": "execute"})
    act.observe("CapabilityRequested",
                {"capability_id": "execute", "call_id": "B", "arguments": {"code": "two"}})
    act.observe("StdoutChunk", {"call_id": "B", "data": "out-B\n"})
    assert act.active is not None
    assert "two" in act.active.summary
    assert any("out-B" in ln for ln in act.active.output_tail)
    assert not any("out-A" in ln for ln in act.active.output_tail)
    assert len(act.recent) == 1  # op A archived exactly once


def test_reset_clears_everything():
    act = ActivityModel()
    _run_execute_lifecycle(act)
    act.observe("ApprovalRequested", {"approval_id": "a9", "capability_id": "x"})
    act.observe("ChildTaskCreated", {"child_task_id": "t1"})
    act.reset_for_new_task()
    assert act.active is None
    assert act.approval is None
    assert not act.background
    assert act.task_status == "idle"


def test_idle_label_when_nothing_running():
    act = ActivityModel()
    assert act.current_label() == "idle"
