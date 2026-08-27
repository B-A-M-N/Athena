"""Canonical event architecture.

Events are the canonical observation API (BUILDSPEC section 78-83). They are
immutable, have monotonic per-task sequence numbers, stable IDs, and support
replay. No interface-specific callback forests.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.artifacts import ArtifactRef
from athena.protocol.messages import utcnow


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    sequence: int
    timestamp: datetime
    task_id: str | None = None
    session_id: str | None = None
    schema_version: int = 1
    payload: Mapping[str, Any] = field(default_factory=dict)
    causal_id: str | None = None


class EventCategory(str, enum.Enum):
    TASK_CREATED = "TaskCreated"
    TASK_QUEUED = "TaskQueued"
    TASK_STARTED = "TaskStarted"
    TASK_STATE_CHANGED = "TaskStateChanged"
    CONTEXT_BUILD_STARTED = "ContextBuildStarted"
    CONTEXT_BUILT = "ContextBuilt"
    CONTEXT_COMPRESSED = "ContextCompressed"
    MODEL_REQUEST_STARTED = "ModelRequestStarted"
    MODEL_DELTA = "ModelDelta"
    MODEL_REASONING_DELTA = "ModelReasoningDelta"
    MODEL_RESPONSE_COMPLETED = "ModelResponseCompleted"
    MODEL_REQUEST_FAILED = "ModelRequestFailed"
    CAPABILITY_REQUESTED = "CapabilityRequested"
    CAPABILITY_VALIDATED = "CapabilityValidated"
    POLICY_DECISION_MADE = "PolicyDecisionMade"
    APPROVAL_REQUESTED = "ApprovalRequested"
    APPROVAL_RESOLVED = "ApprovalResolved"
    CAPABILITY_STARTED = "CapabilityStarted"
    CAPABILITY_PROGRESS = "CapabilityProgress"
    CAPABILITY_COMPLETED = "CapabilityCompleted"
    CAPABILITY_FAILED = "CapabilityFailed"
    RUNTIME_SESSION_CREATED = "RuntimeSessionCreated"
    RUNTIME_STATE_LOST = "RuntimeStateLost"
    EXECUTION_STARTED = "ExecutionStarted"
    STDOUT_CHUNK = "StdoutChunk"
    STDERR_CHUNK = "StderrChunk"
    EXECUTION_EXITED = "ExecutionExited"
    EXECUTION_INTERRUPTED = "ExecutionInterrupted"
    EXECUTION_TIMED_OUT = "ExecutionTimedOut"
    ARTIFACT_CREATED = "ArtifactCreated"
    MUTATION_RECORDED = "MutationRecorded"
    MUTATION_RECORD_FAILED = "MutationRecordFailed"
    MEMORY_CANDIDATE_CREATED = "MemoryCandidateCreated"
    MEMORY_WRITTEN = "MemoryWritten"
    SKILL_ACTIVATED = "SkillActivated"
    SKILL_CANDIDATE_CREATED = "SkillCandidateCreated"
    CHILD_TASK_CREATED = "ChildTaskCreated"
    CHILD_TASK_COMPLETED = "ChildTaskCompleted"
    TASK_COMPLETED = "TaskCompleted"
    TASK_PARTIAL = "TaskPartial"
    TASK_BLOCKED = "TaskBlocked"
    TASK_FAILED = "TaskFailed"
    TASK_CANCELLED = "TaskCancelled"
    TASK_INTERRUPTED = "TaskInterrupted"
    STRATEGY_SELECTED = "StrategySelected"
    AFFORDANCE_GAP_DETECTED = "AffordanceGapDetected"


# Core event type names (BUILDSPEC section 82).
EV: dict[str, str] = {member.name: member.value for member in EventCategory}


def make_event(
    type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    sequence: int | None = None,
    id: str | None = None,
    timestamp: datetime | None = None,
    causal_id: str | None = None,
) -> Event:
    from athena.protocol.ids import new_id
    return Event(
        id=id or new_id("evt"),
        type=type,
        sequence=sequence if sequence is not None else 0,
        timestamp=timestamp or utcnow(),
        task_id=task_id,
        session_id=session_id,
        payload=dict(payload or {}),
        causal_id=causal_id,
    )


__all__ = ["Event", "EventCategory", "make_event", "EV", "ArtifactRef"]
