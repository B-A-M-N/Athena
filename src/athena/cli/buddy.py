"""Semantic buddy (mascot) state machine — a derived UI projection.

INV-007 preserved: the buddy is a *read-only* projection of the canonical
event stream.  It owns no task state, no policy, no approval authority; it
maps authoritative kernel events onto a small deterministic presentation
state machine.

Design rules (UI mission §7–§10):

* explicit priority ordering so overlapping signals cannot fight — an
  approval request legitimately overrides generic executing;
* sticky terminal states (success/failure/interrupted) hold for a bounded
  number of render ticks, then fall back to idle — the mascot can never be
  permanently stuck, and never animates meaninglessly;
* transient signals (lifecycle chatter, progress heartbeats) never override
  an active operation;
* every transition is a pure function of (current state, event, tick) so it
  is exhaustively unit-testable without a terminal.

States::

    idle      listening   thinking    executing
    reading   waiting     approval    success
    failure   interrupted recovering
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["Buddy", "BuddyState", "STATE_PRIORITY"]


class BuddyState:
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    EXECUTING = "executing"
    READING = "reading"
    WAITING = "waiting"           # blocked on background/delegated work
    APPROVAL = "approval"         # approval-required (pauses the world)
    SUCCESS = "success"
    FAILURE = "failure"
    INTERRUPTED = "interrupted"
    RECOVERING = "recovering"


# Higher wins.  An event whose target state outranks the current state
# replaces it; a lower-rank signal is ignored while something more important
# is being shown.  APPROVAL outranks EXECUTING because a paused operation is
# the thing the operator must see.  FAILURE/INTERRUPTED outrank everything
# transient; SUCCESS sits just below them.
STATE_PRIORITY: dict[str, int] = {
    BuddyState.IDLE: 0,
    BuddyState.LISTENING: 1,
    BuddyState.THINKING: 2,
    BuddyState.READING: 3,
    BuddyState.EXECUTING: 4,
    BuddyState.WAITING: 5,
    BuddyState.RECOVERING: 6,
    BuddyState.APPROVAL: 7,
    BuddyState.SUCCESS: 8,
    BuddyState.FAILURE: 9,
    BuddyState.INTERRUPTED: 9,
}

# Sticky states hold this many render ticks before decaying to IDLE so a
# completed/failed task remains visible long enough to register.  All other
# states are event-driven: they persist until the next mapping event.
STICKY_TICKS: dict[str, int] = {
    BuddyState.SUCCESS: 6,
    BuddyState.FAILURE: 10,
    BuddyState.INTERRUPTED: 10,
}

# States that must be explicitly exited by an event (or sticky decay); a
# stray low-priority signal never clears them.
_PINNED = frozenset({BuddyState.APPROVAL, BuddyState.RECOVERING})


# ---------------------------------------------------------------------------
# Event → target-state mapping.  Payload-sensitive cases are handled in
# _target_for(); this table covers the unconditional ones.
# ---------------------------------------------------------------------------
_BASE_MAP: dict[str, str] = {
    # model is working
    "ModelRequestStarted": BuddyState.THINKING,
    "ModelReasoningDelta": BuddyState.THINKING,
    "ModelDelta": BuddyState.THINKING,
    # model request failed before any response
    "ModelRequestFailed": BuddyState.FAILURE,
    # context assembly is a "reading" activity
    "ContextBuildStarted": BuddyState.READING,
    "ContextBuilt": BuddyState.READING,
    "ContextCompressed": BuddyState.READING,
    "MemoryCandidateCreated": BuddyState.READING,
    "MemoryWritten": BuddyState.READING,
    "SkillActivated": BuddyState.READING,
    # execution / tool use
    "CapabilityRequested": BuddyState.EXECUTING,
    "CapabilityStarted": BuddyState.EXECUTING,
    "CapabilityProgress": BuddyState.EXECUTING,
    "RuntimeSessionCreated": BuddyState.EXECUTING,
    "ExecutionStarted": BuddyState.EXECUTING,
    "StdoutChunk": BuddyState.EXECUTING,
    "StderrChunk": BuddyState.EXECUTING,
    "ExecutionExited": BuddyState.EXECUTING,
    "MutationRecorded": BuddyState.EXECUTING,
    # delegated / background work — active but not foreground
    "ChildTaskCreated": BuddyState.WAITING,
    "ChildTaskCompleted": BuddyState.WAITING,
    "TaskBlocked": BuddyState.WAITING,
    # approvals
    "ApprovalRequested": BuddyState.APPROVAL,
    "PolicyDecisionMade": BuddyState.APPROVAL,
    # task lifecycle
    "TaskStarted": BuddyState.THINKING,
    "TaskQueued": BuddyState.LISTENING,
    "TaskCreated": BuddyState.LISTENING,
    "TaskCompleted": BuddyState.SUCCESS,
    "TaskPartial": BuddyState.SUCCESS,
    "TaskFailed": BuddyState.FAILURE,
    "TaskCancelled": BuddyState.INTERRUPTED,
    "TaskInterrupted": BuddyState.INTERRUPTED,
}


@dataclass
class Buddy:
    """Deterministic derived buddy state.

    ``observe(event_type, payload)`` feeds one authoritative event.
    ``tick()`` advances the render clock (once per repaint) and decays sticky
    states.  ``state`` is always safe to render.
    """

    state: str = BuddyState.IDLE
    speech: str = ""
    carried: str = ""            # activity object glyph, e.g. "[>]"
    _sticky_left: int = 0
    _operation_open: bool = field(default=False)

    # -- mapping ---------------------------------------------------------
    def _target_for(self, event_type: str, payload: Mapping[str, Any]) -> str | None:
        if event_type == "ApprovalResolved":
            # Explicit exit from the pinned approval state.  Resume the
            # underlying operation if one is still open.
            return BuddyState.EXECUTING if self._operation_open else BuddyState.IDLE
        if event_type == "CapabilityCompleted":
            # Operation closed; the task may still be thinking.  Do not jump
            # to SUCCESS here — only the task lifecycle decides completion.
            return BuddyState.THINKING
        if event_type in {"CapabilityFailed", "ExecutionTimedOut",
                          "MutationRecordFailed", "ExecutionInterrupted"}:
            # An operation-level failure: show failure, but a later task-level
            # event may refine it (recovery, retry, overall success).
            return BuddyState.FAILURE
        if event_type == "TaskStateChanged":
            status = str(payload.get("status") or payload.get("to") or "").upper()
            if status == "WAITING_APPROVAL":
                return BuddyState.APPROVAL
            if status in {"RUNNING", "EXECUTING"}:
                return BuddyState.EXECUTING if self._operation_open else BuddyState.THINKING
            if status in {"RECOVERING", "RETRYING", "RESUMING"}:
                return BuddyState.RECOVERING
            if status == "BLOCKED":
                return BuddyState.WAITING
            return None
        return _BASE_MAP.get(event_type)

    # -- public API --------------------------------------------------------
    def observe(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        payload = payload or {}
        self._track_operation(event_type)
        target = self._target_for(event_type, payload)
        if target is None:
            return
        cur_rank = STATE_PRIORITY.get(self.state, 0)
        new_rank = STATE_PRIORITY.get(target, 0)
        if self.state in _PINNED and target not in _PINNED and new_rank < cur_rank:
            # A pinned state (approval / recovering) exits only via its
            # explicit exit event or a higher-priority state.
            if event_type != "ApprovalResolved":
                return
            self._enter(target)
            return
        if self._sticky_left > 0 and new_rank < cur_rank:
            # A sticky terminal state still on screen swallows lower-priority
            # chatter until it decays.
            return
        if new_rank < cur_rank:
            return
        self._enter(target)

    def _enter(self, state: str) -> None:
        self.state = state
        self._sticky_left = STICKY_TICKS.get(state, 0)
        if state in (BuddyState.IDLE, BuddyState.LISTENING):
            self.carried = ""
            self.speech = ""

    def _track_operation(self, event_type: str) -> None:
        if event_type in {
            "CapabilityRequested", "CapabilityStarted", "ExecutionStarted",
        }:
            self._operation_open = True
        elif event_type in {
            "CapabilityCompleted", "CapabilityFailed", "ExecutionExited",
            "ExecutionTimedOut", "ExecutionInterrupted",
            "TaskCompleted", "TaskPartial", "TaskFailed",
            "TaskCancelled", "TaskInterrupted",
        }:
            self._operation_open = False

    def tick(self) -> None:
        """Advance one render tick; decay sticky terminal states to idle."""
        if self._sticky_left > 0:
            self._sticky_left -= 1
            if self._sticky_left == 0 and self.state in STICKY_TICKS:
                self._enter(BuddyState.IDLE)

    def reset(self) -> None:
        """Hard reset for a new task/turn — deterministic cleanup."""
        self._enter(BuddyState.IDLE)
        self._sticky_left = 0
        self._operation_open = False
