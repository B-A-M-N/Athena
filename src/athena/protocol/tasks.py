"""Universal task model.

All autonomous work MUST ultimately become a Task (INV-002). TaskSpec is the
universal autonomous-work definition; runtime state belongs in the persisted
Task record.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from athena.protocol.artifacts import ArtifactRef
from athena.protocol.messages import utcnow


class TaskStatus(str, enum.Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_INPUT = "WAITING_INPUT"
    BLOCKED = "BLOCKED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    COMPLETE = "COMPLETE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

    def legal_transitions(self) -> frozenset["TaskStatus"]:
        return LEGAL_TRANSITIONS.get(self, frozenset())


# Truly final — the lifecycle is over, no resumption. A task in one of these
# states is finished: event streams close, wait_for returns, the worker stops,
# and completed_at is written.
FINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETE, TaskStatus.PARTIAL, TaskStatus.FAILED,
     TaskStatus.CANCELLED}
)

# Paused — not currently executing, but may resume. NOT terminal. A paused task
# is still alive: its event stream stays open, wait_for keeps polling, the
# worker may claim it again, and completed_at must NOT be written.
PAUSED_STATUSES = frozenset(
    {TaskStatus.WAITING_APPROVAL, TaskStatus.WAITING_INPUT,
     TaskStatus.INTERRUPTED, TaskStatus.RECOVERY_REQUIRED, TaskStatus.BLOCKED}
)

# Backward-compatible alias for code written against the old "terminal" notion;
# INTERRUPTED / RECOVERY_REQUIRED / BLOCKED are no longer terminal.
TERMINAL_STATUSES = FINAL_STATUSES


LEGAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.WAITING_APPROVAL, TaskStatus.WAITING_INPUT, TaskStatus.BLOCKED,
        TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.CANCELLED,
        TaskStatus.INTERRUPTED, TaskStatus.COMPLETE, TaskStatus.RECOVERY_REQUIRED,
    }),
    TaskStatus.WAITING_APPROVAL: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.WAITING_INPUT: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.INTERRUPTED: frozenset({
        TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.RECOVERY_REQUIRED,
    }),
    TaskStatus.RECOVERY_REQUIRED: frozenset({
        TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
}


class AutonomyLevel(str, enum.Enum):
    SUPERVISED = "supervised"
    CODING = "coding"
    AUTONOMOUS = "autonomous"
    OFFLINE = "offline"


class VerificationType(str, enum.Enum):
    COMMAND = "command"
    FILE = "file"
    ARTIFACT_PREDICATE = "artifact_predicate"
    CAPABILITY_CHECK = "capability_check"
    MODEL_JUDGMENT = "model_judgment"
    MANUAL = "manual"


@dataclass(frozen=True)
class VerificationSpec:
    type: VerificationType
    command: str | None = None
    path: str | None = None
    predicate: str | None = None
    capability: str | None = None


@dataclass(frozen=True)
class Criterion:
    id: str
    description: str
    verification: VerificationSpec | None = None
    required: bool = True


@dataclass(frozen=True)
class ContextRef:
    kind: str          # session | memory | skill | artifact | file | task | web
    ref: str
    source_id: str | None = None
    summary: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class PathRule:
    path: str
    allow: bool = True


class NetworkPolicy(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    RESTRICTED = "restricted"


class MutationMode(str, enum.Enum):
    """Where project mutations are allowed to land.

    ``DIRECT`` preserves the ordinary workspace contract for callers that
    explicitly opt into immediate mutations.  ``SPECULATIVE`` makes the
    execution authority lazily create a task-local candidate workspace before
    the first project-sensitive mutation.  ``READ_ONLY`` rejects project
    mutations at that same authority boundary.
    """

    DIRECT = "direct"
    SPECULATIVE = "speculative"
    READ_ONLY = "read_only"


@dataclass(frozen=True)
class WorkspaceSpec:
    id: str
    root: str
    readable: tuple[PathRule, ...] = ()
    writable: tuple[PathRule, ...] = ()
    temp_root: str | None = None
    execution_backend: str = "local"
    network_policy: NetworkPolicy = NetworkPolicy.ALLOW
    mutation_mode: MutationMode = MutationMode.DIRECT


@dataclass(frozen=True)
class ResourceBudget:
    max_agent_iterations: int = 100
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: Decimal | None = None
    max_wall_time: timedelta | None = None
    max_children: int = 4
    max_child_depth: int = 1
    max_parallel_model_calls: int = 4
    max_parallel_executions: int = 4
    max_artifact_bytes: int = 10 * 1024 * 1024

    def merged_with(self, other: "ResourceBudget | None") -> "ResourceBudget":
        if other is None:
            return self
        return ResourceBudget(
            max_agent_iterations=min(self.max_agent_iterations, other.max_agent_iterations),
            max_input_tokens=_min_opt(self.max_input_tokens, other.max_input_tokens),
            max_output_tokens=_min_opt(self.max_output_tokens, other.max_output_tokens),
            max_cost_usd=_min_opt(self.max_cost_usd, other.max_cost_usd),
            max_wall_time=_min_opt(self.max_wall_time, other.max_wall_time),
            max_children=min(self.max_children, other.max_children),
            max_child_depth=min(self.max_child_depth, other.max_child_depth),
            max_parallel_model_calls=min(self.max_parallel_model_calls, other.max_parallel_model_calls),
            max_parallel_executions=min(self.max_parallel_executions, other.max_parallel_executions),
            max_artifact_bytes=min(self.max_artifact_bytes, other.max_artifact_bytes),
        )


def _min_opt(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


@dataclass(frozen=True)
class ModelPolicy:
    role: str = "primary"
    allowed: tuple[str, ...] = ()
    require_tools: bool = True
    privacy: str = "local-preferred"
    max_cost_usd: Decimal | None = None


@dataclass(frozen=True)
class CapabilityPolicy:
    effects: frozenset[str] = frozenset()
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliverySpec:
    channel: str | None = None
    destination: str | None = None


@dataclass(frozen=True)
class TaskSpec:
    id: str
    objective: str
    acceptance_criteria: tuple[Criterion, ...] = ()
    session_id: str | None = None
    parent_task_id: str | None = None
    context_refs: tuple[ContextRef, ...] = ()
    workspace: WorkspaceSpec | None = None
    capability_policy: CapabilityPolicy = CapabilityPolicy()
    model_policy: ModelPolicy = ModelPolicy()
    resource_budget: ResourceBudget = ResourceBudget()
    deadline: datetime | None = None
    delivery: DeliverySpec | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    cost_usd: Decimal = Decimal(0)
    duration_ms: int = 0
    executions: int = 0
    mutations: int = 0


@dataclass(frozen=True)
class MutationRef:
    id: str
    resource: str
    operation: str
    reversible: bool = False


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: TaskStatus
    summary: str = ""
    evidence: tuple[ContextRef, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    mutations: tuple[MutationRef, ...] = ()
    unresolved: tuple[str, ...] = ()
    usage: UsageSummary = UsageSummary()
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class AgentRequest:
    prompt: str
    session_id: str | None = None
    task_id: str | None = None
    workspace: WorkspaceSpec | None = None
    model_policy: ModelPolicy | None = None
    autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED
    attachments: tuple[ArtifactRef, ...] = ()
    requested_capabilities: frozenset[str] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


__all__ = [
    "TaskStatus", "TERMINAL_STATUSES", "FINAL_STATUSES", "PAUSED_STATUSES",
    "AutonomyLevel", "VerificationType",
    "VerificationSpec", "Criterion", "ContextRef", "PathRule", "NetworkPolicy",
    "WorkspaceSpec", "MutationMode", "ResourceBudget", "ModelPolicy", "CapabilityPolicy",
    "DeliverySpec", "TaskSpec", "UsageSummary", "MutationRef", "TaskResult",
    "AgentRequest",
]
