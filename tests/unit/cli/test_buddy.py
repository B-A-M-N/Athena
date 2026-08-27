"""Semantic buddy state machine tests (UI mission §8/§32).

Guarantees: event→state mapping, priority ordering (approval overrides
executing), pinned-state exit conditions, sticky decay (never stuck), and
deterministic cleanup.
"""

from __future__ import annotations

import pytest

from athena.cli.buddy import STICKY_TICKS, Buddy, BuddyState


def test_buddy_starts_idle():
    assert Buddy().state == BuddyState.IDLE


def test_model_delta_maps_to_thinking():
    b = Buddy()
    b.observe("ModelDelta", {"text": "hi"})
    assert b.state == BuddyState.THINKING


def test_execution_events_map_to_executing():
    b = Buddy()
    for et in ("CapabilityRequested", "ExecutionStarted", "StdoutChunk"):
        b.observe(et, {})
    assert b.state == BuddyState.EXECUTING


def test_approval_overrides_executing():
    """Approval-required legitimately overrides generic executing (§8)."""
    b = Buddy()
    b.observe("ExecutionStarted", {})
    b.observe("ApprovalRequested", {"approval_id": "a1"})
    assert b.state == BuddyState.APPROVAL


def test_approval_pinned_against_stream_chatter():
    """Stdout while approval is pending must NOT unpin the approval state."""
    b = Buddy()
    b.observe("ApprovalRequested", {"approval_id": "a1"})
    b.observe("StdoutChunk", {"data": "noise\n"})
    b.observe("ModelDelta", {"text": "noise"})
    assert b.state == BuddyState.APPROVAL


def test_approval_resolution_resumes_operation():
    b = Buddy()
    b.observe("ExecutionStarted", {})
    b.observe("ApprovalRequested", {"approval_id": "a1"})
    b.observe("ApprovalResolved", {"decision": "approved"})
    assert b.state == BuddyState.EXECUTING


def test_approval_resolution_without_operation_returns_idle():
    b = Buddy()
    b.observe("ApprovalRequested", {"approval_id": "a1"})
    # no operation opened; ApprovalRequested alone doesn't open one
    b2 = Buddy()
    b2.observe("ApprovalRequested", {"approval_id": "a1"})
    b2.observe("ApprovalResolved", {"decision": "denied"})
    assert b2.state == BuddyState.IDLE


def test_task_completed_is_sticky_then_decays_to_idle():
    """Terminal states hold briefly, then idle — never permanently stuck."""
    b = Buddy()
    b.observe("TaskCompleted", {})
    assert b.state == BuddyState.SUCCESS
    for _ in range(STICKY_TICKS[BuddyState.SUCCESS]):
        b.tick()
    assert b.state == BuddyState.IDLE


def test_failure_outranks_thinking_and_decays():
    b = Buddy()
    b.observe("ModelDelta", {"text": "work"})
    b.observe("TaskFailed", {})
    assert b.state == BuddyState.FAILURE
    for _ in range(STICKY_TICKS[BuddyState.FAILURE] + 1):
        b.tick()
    assert b.state == BuddyState.IDLE


def test_sticky_success_swallows_low_priority_chatter():
    b = Buddy()
    b.observe("TaskCompleted", {})
    b.observe("ModelDelta", {"text": "leftover"})
    assert b.state == BuddyState.SUCCESS


def test_cancel_maps_to_interrupted():
    b = Buddy()
    b.observe("TaskCancelled", {})
    assert b.state == BuddyState.INTERRUPTED


def test_waiting_state_for_delegated_work():
    b = Buddy()
    b.observe("ExecutionStarted", {})
    b.observe("ChildTaskCreated", {"child_task_id": "t2"})
    assert b.state == BuddyState.WAITING


def test_context_events_map_to_reading():
    b = Buddy()
    b.observe("ModelDelta", {"text": "x"})
    b.observe("ContextBuildStarted", {})
    assert b.state == BuddyState.READING


def test_task_state_changed_waiting_approval_maps_to_approval():
    b = Buddy()
    b.observe("TaskStateChanged", {"status": "WAITING_APPROVAL"})
    assert b.state == BuddyState.APPROVAL


def test_recovering_state():
    b = Buddy()
    b.observe("TaskStateChanged", {"status": "RECOVERING"})
    assert b.state == BuddyState.RECOVERING


def test_unknown_event_is_ignored():
    b = Buddy()
    b.observe("NoSuchEvent", {})
    assert b.state == BuddyState.IDLE


def test_reset_returns_to_idle():
    b = Buddy()
    b.observe("TaskFailed", {})
    b.reset()
    assert b.state == BuddyState.IDLE


@pytest.mark.parametrize("terminal_event", [
    "TaskCompleted", "TaskFailed", "TaskCancelled", "TaskInterrupted",
])
def test_no_stuck_states_after_full_lifecycle(terminal_event):
    """Any terminal event sequence must return to idle in bounded ticks."""
    b = Buddy()
    b.observe("TaskStarted", {})
    b.observe("ExecutionStarted", {})
    b.observe("StdoutChunk", {"data": "x\n"})
    b.observe(terminal_event, {})
    for _ in range(12):
        b.tick()
    assert b.state == BuddyState.IDLE
