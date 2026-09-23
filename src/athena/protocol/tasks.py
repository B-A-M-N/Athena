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


_NO_CAPABILITY_INTERSECTION = "__athena_no_capability_intersection__"
_NO_EFFECT_INTERSECTION = "__athena_no_effect_intersection__"
_NO_MODEL_INTERSECTION = "__athena_no_model_intersection__"


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
    {TaskStatus.COMPLETE, TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# Paused — not currently executing, but may resume. NOT terminal. A paused task
# is still alive: its event stream stays open, wait_for keeps polling, the
# worker may claim it again, and completed_at must NOT be written.
PAUSED_STATUSES = frozenset(
    {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.WAITING_INPUT,
        TaskStatus.INTERRUPTED,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.BLOCKED,
    }
)

# Backward-compatible alias for code written against the old "terminal" notion;
# INTERRUPTED / RECOVERY_REQUIRED / BLOCKED are no longer terminal.
TERMINAL_STATUSES = FINAL_STATUSES


LEGAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.RECOVERY_REQUIRED}
    ),
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.RECOVERY_REQUIRED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_INPUT,
            TaskStatus.BLOCKED,
            TaskStatus.PARTIAL,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
            TaskStatus.COMPLETE,
            TaskStatus.RECOVERY_REQUIRED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.RECOVERY_REQUIRED}
    ),
    TaskStatus.WAITING_INPUT: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.RECOVERY_REQUIRED}
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.RECOVERY_REQUIRED}
    ),
    TaskStatus.INTERRUPTED: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.CANCELLED,
            TaskStatus.RECOVERY_REQUIRED,
        }
    ),
    TaskStatus.RECOVERY_REQUIRED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
}


class AutonomyLevel(str, enum.Enum):
    SUPERVISED = "supervised"
    CODING = "coding"
    AUTONOMOUS = "autonomous"
    OFFLINE = "offline"


class Durability(str, enum.Enum):
    """Persistence contract for a durable write (P1-27).

    Codifies the durability split established by the manager's authority
    commit: authority state commits synchronously and its failure blocks the
    operation; bookkeeping may defer and its failure is logged, never
    surfaced to the caller. Every durable write site declares which side of
    the split it is on, so the contract is auditable at the call site instead
    of living only in comments.
    """

    # The write IS the source of truth being admitted. It commits before the
    # caller observes success, and a failure propagates: the operation did
    # not happen. Examples: task-row insert in TaskManager.create, durable
    # status transitions, input-request answer columns, branch verification
    # records.
    AUTHORITY = "authority"

    # Derived or reproducible state that must not mask or roll back an
    # already-committed authority write. A failure is logged and non-fatal.
    # Examples: budget ledger registration, cancellation-key resets,
    # lifecycle event emission, usage bookkeeping.
    BOOKKEEPING = "bookkeeping"


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
    # A criterion with this flag requires a typed evidence receipt from the
    # canonical research workflow.  A successful capability invocation alone
    # is not enough: the workflow must report a ready bundle.
    evidence_required: bool = False
    # Optional stable research requirement binding. A bundle receipt may only
    # satisfy the criterion when it names this requirement (or its evidence).
    evidence_requirement_id: str | None = None


@dataclass(frozen=True)
class ContextRef:
    kind: str  # session | memory | skill | artifact | context_block | file | task | web
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
    # ``None`` means the caller did not select a backend.  Consumers resolve
    # it to the local backend at the execution boundary; delegation must keep
    # this distinction so an omitted child backend cannot masquerade as an
    # explicit request to downgrade a container parent.
    execution_backend: str | None = None
    network_policy: NetworkPolicy = NetworkPolicy.ALLOW
    mutation_mode: MutationMode = MutationMode.DIRECT
    # Child workspace mode for delegation (SHARED_READ, SHADOW_WRITE, SUBTREE, DETACHED).
    delegate_mode: str | None = None
    # Whether the child is required (parent can't complete without it) vs detached.
    required_child: bool = True
    # Optional revision supplied by a persisted project/workspace index.  A
    # cache policy may use it, but the dispatcher never invents one by hashing
    # the entire workspace synchronously.
    revision: str | None = None


class WorkClass(str, enum.Enum):
    """Service-derived persistence-safe work classification."""

    NON_CODING = "non_coding"
    SIMPLE_EDIT = "simple_edit"
    COMPLEX_CODING = "complex_coding"


class SpeculationDepth(str, enum.Enum):
    """Service-derived candidate isolation depth."""

    NONE = "none"
    SINGLE_CANDIDATE = "single_candidate"
    MULTI_CANDIDATE = "multi_candidate"


class VerificationStrength(str, enum.Enum):
    """Independent-proof floor requested for candidate certification."""

    NONE = "none"
    STANDARD = "standard"
    STRONG = "strong"


@dataclass(frozen=True)
class TaskExecutionPlan:
    """Typed service-owned execution authority (review item 4).

    Replaces private metadata strings with a durable, codec-serializable
    structure so restart, ACP, delegation, and persistence cannot lose the
    admission decision. External callers cannot grant themselves weaker
    authority; the service computes this at intake.
    """

    work_class: WorkClass
    speculation_depth: SpeculationDepth
    isolation_floor: MutationMode
    verification_floor: VerificationStrength

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "TaskExecutionPlan":
        def _enum(enum_type, value, default):
            try:
                return enum_type(value)
            except (TypeError, ValueError):
                return default

        return cls(
            work_class=_enum(WorkClass, raw.get("work_class"), WorkClass.NON_CODING),
            speculation_depth=_enum(
                SpeculationDepth, raw.get("speculation_depth"), SpeculationDepth.NONE
            ),
            isolation_floor=_enum(MutationMode, raw.get("isolation_floor"), MutationMode.DIRECT),
            verification_floor=_enum(
                VerificationStrength,
                raw.get("verification_floor"),
                VerificationStrength.NONE,
            ),
        )

    def to_record(self) -> dict[str, str]:
        return {
            "work_class": self.work_class.value,
            "speculation_depth": self.speculation_depth.value,
            "isolation_floor": self.isolation_floor.value,
            "verification_floor": self.verification_floor.value,
        }


@dataclass(frozen=True)
class ResourceBudget:
    # A bounded default keeps ordinary tasks in the tens; callers with a
    # genuinely long mission must opt into a larger budget explicitly.
    max_agent_iterations: int = 50
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: Decimal | None = None
    max_wall_time: timedelta | None = None
    max_children: int = 4
    max_child_depth: int = 1
    max_parallel_model_calls: int = 4
    max_parallel_executions: int = 16
    max_artifact_bytes: int = 100 * 1024 * 1024

    def merged_with(self, other: "ResourceBudget | None") -> "ResourceBudget":
        if other is None:
            return self
        return ResourceBudget(
            max_agent_iterations=_min_opt(self.max_agent_iterations, other.max_agent_iterations),
            max_input_tokens=_min_opt(self.max_input_tokens, other.max_input_tokens),
            max_output_tokens=_min_opt(self.max_output_tokens, other.max_output_tokens),
            max_cost_usd=_min_opt(self.max_cost_usd, other.max_cost_usd),
            max_wall_time=_min_opt(self.max_wall_time, other.max_wall_time),
            max_children=_min_opt(self.max_children, other.max_children),
            max_child_depth=_min_opt(self.max_child_depth, other.max_child_depth),
            max_parallel_model_calls=_min_opt(
                self.max_parallel_model_calls, other.max_parallel_model_calls
            ),
            max_parallel_executions=_min_opt(
                self.max_parallel_executions, other.max_parallel_executions
            ),
            max_artifact_bytes=_min_opt(self.max_artifact_bytes, other.max_artifact_bytes),
        )


@dataclass(frozen=True)
class ResourceBudgetCeiling:
    """Authority ceiling where an omitted dimension means unbounded.

    ``ResourceBudget`` is the concrete execution budget with safe defaults.
    Persisted authority algebra needs a separate type so an omitted ceiling
    cannot be confused with those defaults.
    """

    max_agent_iterations: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: Decimal | None = None
    max_wall_time: timedelta | None = None
    max_children: int | None = None
    max_child_depth: int | None = None
    max_parallel_model_calls: int | None = None
    max_parallel_executions: int | None = None
    max_artifact_bytes: int | None = None

    def merged_with(self, other: "ResourceBudgetCeiling | None") -> "ResourceBudgetCeiling":
        if other is None:
            return self
        return ResourceBudgetCeiling(
            max_agent_iterations=_min_opt(self.max_agent_iterations, other.max_agent_iterations),
            max_input_tokens=_min_opt(self.max_input_tokens, other.max_input_tokens),
            max_output_tokens=_min_opt(self.max_output_tokens, other.max_output_tokens),
            max_cost_usd=_min_opt(self.max_cost_usd, other.max_cost_usd),
            max_wall_time=_min_opt(self.max_wall_time, other.max_wall_time),
            max_children=_min_opt(self.max_children, other.max_children),
            max_child_depth=_min_opt(self.max_child_depth, other.max_child_depth),
            max_parallel_model_calls=_min_opt(
                self.max_parallel_model_calls, other.max_parallel_model_calls
            ),
            max_parallel_executions=_min_opt(
                self.max_parallel_executions, other.max_parallel_executions
            ),
            max_artifact_bytes=_min_opt(self.max_artifact_bytes, other.max_artifact_bytes),
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
    # Tool use is selected by the current task/context. This flag is an
    # explicit requirement for callers that need a tool-capable route; it is
    # not the default for ordinary conversational turns.
    require_tools: bool = False
    privacy: str = "local-preferred"
    max_cost_usd: Decimal | None = None
    # Routing preference is advisory only; privacy, capability, and cost
    # constraints remain hard filters.  ``balanced`` preserves the default
    # latency/reliability-aware route, while ``latency`` and ``cost`` let a
    # configured role state its operational priority explicitly.
    routing_preference: str = "balanced"
    # Quality floor (P1-16): when set, routing excludes models that DECLARE
    # a tier below this floor. Undeclared models stay selectable — the floor
    # never excludes a model that could not have known about it, so a
    # deployment whose providers declare no tiers is unaffected. Accepts the
    # bare string value ("standard"); invalid values are ignored.
    min_quality_tier: str | None = None
    # Safety-sensitive deployments may require a provider to declare its tier;
    # undeclared metadata is otherwise treated conservatively as advisory.
    require_declared_quality: bool = False
    # Bounded provider failover. The router may use fewer attempts when fewer
    # distinct eligible routes exist; it must never retry indefinitely.
    max_model_attempts: int = 2

    def __post_init__(self) -> None:
        attempts = int(self.max_model_attempts)
        if attempts < 1 or attempts > 8:
            raise ValueError("max_model_attempts must be between 1 and 8")
        object.__setattr__(self, "max_model_attempts", attempts)


@dataclass(frozen=True)
class CapabilityPolicy:
    effects: frozenset[str] = frozenset()
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


def _policy_parts(value: CapabilityPolicy | Mapping[str, Any] | None) -> CapabilityPolicy:
    if isinstance(value, CapabilityPolicy):
        return value
    if not isinstance(value, Mapping):
        return CapabilityPolicy()
    return CapabilityPolicy(
        effects=frozenset(str(item) for item in value.get("effects") or ()),
        allow=tuple(str(item) for item in value.get("allow") or ()),
        ask=tuple(str(item) for item in value.get("ask") or ()),
        deny=tuple(str(item) for item in value.get("deny") or ()),
    )


def effective_capability_policy(
    value: CapabilityPolicy | Mapping[str, Any] | None,
) -> CapabilityPolicy:
    """Return the canonical effective policy used by every authority check.

    Deny is applied once at the boundary so policy evaluation, delegation,
    scheduling, reflection, and intersection cannot each invent subtly
    different ALLOW/ASK semantics.
    """
    policy = _policy_parts(value)
    denied = set(policy.deny)
    allow = set(policy.allow) - denied
    ask = set(policy.ask) - denied
    if "*" in denied:
        allow.clear()
        ask.clear()
    return CapabilityPolicy(
        effects=policy.effects,
        allow=tuple(sorted(allow)),
        ask=tuple(sorted(ask)),
        deny=tuple(sorted(denied)),
    )


# Private compatibility name for older internal callers.
_effective_capability_policy = effective_capability_policy


def _intersect_unrestricted_sets(left: set[str], right: set[str]) -> set[str]:
    """Intersect sets where an empty set is the protocol's universal value."""
    if not left:
        return set(right)
    if not right:
        return set(left)
    return left & right


def intersect_capability_policies(
    left: CapabilityPolicy | Mapping[str, Any] | None,
    right: CapabilityPolicy | Mapping[str, Any] | None,
) -> CapabilityPolicy:
    """Return the authority intersection without widening empty universals.

    ``allow``/``ask`` and ``effects`` use empty-as-universal semantics. ASK is
    retained whenever either side requires approval for a surviving capability;
    deny remains a hard union.
    """
    raw_left = _policy_parts(left)
    raw_right = _policy_parts(right)
    a = _effective_capability_policy(left)
    b = _effective_capability_policy(right)
    a_visible = set(a.allow) | set(a.ask)
    b_visible = set(b.allow) | set(b.ask)
    canceled_left_authority = bool(raw_left.allow or raw_left.ask) and not a_visible
    canceled_right_authority = bool(raw_right.allow or raw_right.ask) and not b_visible
    if canceled_left_authority or canceled_right_authority:
        visible = set()
    else:
        visible = _intersect_unrestricted_sets(a_visible, b_visible)
    ask = (set(a.ask) | set(b.ask)) & visible
    allow = visible - ask
    deny = set(a.deny) | set(b.deny)
    allow -= deny
    ask -= deny
    if "*" in deny:
        allow.clear()
        ask.clear()
    if (
        not allow
        and not ask
        and (a_visible or b_visible or canceled_left_authority or canceled_right_authority)
    ):
        allow.add(_NO_CAPABILITY_INTERSECTION)
    effects = _intersect_unrestricted_sets(set(a.effects), set(b.effects))
    if a.effects and b.effects and not effects:
        effects.add(_NO_EFFECT_INTERSECTION)
    return CapabilityPolicy(
        effects=frozenset(effects),
        allow=tuple(sorted(allow)),
        ask=tuple(sorted(ask)),
        deny=tuple(sorted(deny)),
    )


def capability_policy_covers(
    upper: CapabilityPolicy | Mapping[str, Any] | None,
    lower: CapabilityPolicy | Mapping[str, Any] | None,
) -> bool:
    """Return whether ``lower`` is contained by the ``upper`` ceiling."""
    raw_upper = _policy_parts(upper)
    raw_lower = _policy_parts(lower)
    a = _effective_capability_policy(upper)
    b = _effective_capability_policy(lower)
    upper_allow = set(a.allow)
    upper_ask = set(a.ask)
    lower_allow = set(b.allow)
    lower_ask = set(b.ask)
    lower_is_empty = (
        _NO_CAPABILITY_INTERSECTION in lower_allow or _NO_CAPABILITY_INTERSECTION in lower_ask
    )
    lower_allow.discard(_NO_CAPABILITY_INTERSECTION)
    lower_ask.discard(_NO_CAPABILITY_INTERSECTION)
    upper_visible = upper_allow | upper_ask
    lower_visible = lower_allow | lower_ask
    lower_is_empty = (
        lower_is_empty
        or "*" in b.deny
        or bool(raw_lower.allow or raw_lower.ask)
        and not lower_visible
    )
    upper_is_empty = (
        _NO_CAPABILITY_INTERSECTION in upper_allow
        or _NO_CAPABILITY_INTERSECTION in upper_ask
        or "*" in a.deny
        or bool(raw_upper.allow or raw_upper.ask)
        and not upper_visible
    )
    if upper_is_empty and not lower_is_empty:
        return False
    # Empty allow/ask is the protocol's unrestricted value.  Once a policy
    # names an allow/ask ceiling, preserve the distinction: ASK is weaker than
    # ALLOW for a caller, but it cannot cover a stored autonomous ALLOW.
    # A policy whose every positive rule is cancelled by deny represents the
    # empty authority set. It is narrower than any non-denying ceiling; the
    # absence of visible rules alone must not be confused with the protocol's
    # unrestricted empty policy.
    if upper_visible and not lower_visible and not lower_is_empty:
        return False
    if upper_visible and not lower_allow.issubset(upper_allow):
        return False
    if upper_visible and not lower_ask.issubset(upper_visible):
        return False
    if lower_visible & set(a.deny) or "*" in a.deny and lower_visible:
        return False
    if "*" in a.deny and "*" not in b.deny:
        return False
    if not upper_visible and not lower_visible and "*" in a.deny and "*" not in b.deny:
        return False
    if (
        not upper_visible
        and not lower_visible
        and "*" not in a.deny
        and "*" not in b.deny
        and not lower_is_empty
    ):
        if not set(a.deny).issubset(set(b.deny)):
            return False
    upper_effects = set(a.effects)
    lower_effects = set(b.effects)
    lower_effects_empty = _NO_EFFECT_INTERSECTION in lower_effects
    if lower_effects_empty:
        lower_effects = set()
    if _NO_EFFECT_INTERSECTION in upper_effects:
        return lower_effects_empty
    if (
        upper_effects
        and not lower_effects_empty
        and (not lower_effects or not lower_effects.issubset(upper_effects))
    ):
        return False
    return True


def intersect_resource_budgets(
    left: ResourceBudget | ResourceBudgetCeiling | Mapping[str, Any] | None,
    right: ResourceBudget | ResourceBudgetCeiling | Mapping[str, Any] | None,
) -> ResourceBudgetCeiling:
    """Intersect two budgets; omitted limits remain unbounded, not zero."""
    return _budget_ceiling_from_value(left).merged_with(_budget_ceiling_from_value(right))


def _budget_ceiling_from_value(
    value: ResourceBudget | ResourceBudgetCeiling | Mapping[str, Any] | None,
) -> ResourceBudgetCeiling:
    if isinstance(value, ResourceBudgetCeiling):
        return value
    if isinstance(value, ResourceBudget):
        return ResourceBudgetCeiling(
            max_agent_iterations=value.max_agent_iterations,
            max_input_tokens=value.max_input_tokens,
            max_output_tokens=value.max_output_tokens,
            max_cost_usd=value.max_cost_usd,
            max_wall_time=value.max_wall_time,
            max_children=value.max_children,
            max_child_depth=value.max_child_depth,
            max_parallel_model_calls=value.max_parallel_model_calls,
            max_parallel_executions=value.max_parallel_executions,
            max_artifact_bytes=value.max_artifact_bytes,
        )
    if not isinstance(value, Mapping):
        return ResourceBudgetCeiling()
    values: dict[str, Any] = {}
    for name in (
        "max_agent_iterations",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_usd",
        "max_wall_time",
        "max_children",
        "max_child_depth",
        "max_parallel_model_calls",
        "max_parallel_executions",
        "max_artifact_bytes",
    ):
        if name not in value or value[name] is None:
            continue
        raw = value[name]
        if name == "max_cost_usd":
            raw = Decimal(str(raw))
        elif name == "max_wall_time":
            raw = timedelta(seconds=float(raw))
        values[name] = raw
    return ResourceBudgetCeiling(**values)


def _budget_from_value(
    value: ResourceBudget | ResourceBudgetCeiling | Mapping[str, Any] | None,
) -> ResourceBudgetCeiling:
    """Backward-compatible private alias for callers in older integrations."""
    return _budget_ceiling_from_value(value)


def resource_budget_covers(
    upper: ResourceBudget | ResourceBudgetCeiling | Mapping[str, Any] | None,
    lower: ResourceBudget | ResourceBudgetCeiling | Mapping[str, Any] | None,
) -> bool:
    """Return whether every lower budget limit is within the upper limit."""
    if upper is None:
        return True
    a = _budget_from_value(upper)
    b = _budget_from_value(lower)
    for name in (
        "max_agent_iterations",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_usd",
        "max_wall_time",
        "max_children",
        "max_child_depth",
        "max_parallel_model_calls",
        "max_parallel_executions",
        "max_artifact_bytes",
    ):
        high = getattr(a, name)
        low = getattr(b, name)
        if high is not None and (low is None or low > high):
            return False
    return True


def _model_policy_value(value: ModelPolicy | Mapping[str, Any] | None) -> ModelPolicy:
    if isinstance(value, ModelPolicy):
        return value
    if not isinstance(value, Mapping):
        return ModelPolicy()
    cost = value.get("max_cost_usd")
    return ModelPolicy(
        role=str(value.get("role", "primary")),
        allowed=tuple(str(item) for item in value.get("allowed") or ()),
        require_tools=bool(value.get("require_tools", False)),
        privacy=str(value.get("privacy", "local-preferred")),
        max_cost_usd=Decimal(str(cost)) if cost not in (None, "") else None,
        routing_preference=str(value.get("routing_preference", "balanced")),
        min_quality_tier=(
            str(value["min_quality_tier"]) if value.get("min_quality_tier") is not None else None
        ),
        require_declared_quality=bool(value.get("require_declared_quality", False)),
        max_model_attempts=int(value.get("max_model_attempts", 2)),
    )


def _privacy_rank(value: str) -> int:
    return {"offline": 0, "local": 0, "local-preferred": 1, "local-pref": 1, "remote": 2}.get(
        str(value or "local-preferred"), 0
    )


def _quality_rank(value: str | None) -> int:
    return {"basic": 0, "standard": 1, "advanced": 2, "frontier": 3}.get(str(value), -1)


def intersect_model_policies(
    left: ModelPolicy | Mapping[str, Any] | None,
    right: ModelPolicy | Mapping[str, Any] | None,
) -> ModelPolicy:
    a = _model_policy_value(left)
    b = _model_policy_value(right)
    allowed = tuple(_intersect_unrestricted_sets(set(a.allowed), set(b.allowed)))
    if a.allowed and b.allowed and not allowed:
        allowed = (_NO_MODEL_INTERSECTION,)
    floors = [item for item in (a.min_quality_tier, b.min_quality_tier) if item]
    floor = max(floors, key=_quality_rank) if floors else None
    return ModelPolicy(
        role=a.role if a.role == b.role else b.role if a.role == "primary" else a.role,
        allowed=tuple(sorted(allowed)),
        require_tools=a.require_tools or b.require_tools,
        privacy=(a.privacy if _privacy_rank(a.privacy) <= _privacy_rank(b.privacy) else b.privacy),
        max_cost_usd=_min_opt(a.max_cost_usd, b.max_cost_usd),
        routing_preference=(
            b.routing_preference if b.routing_preference != "balanced" else a.routing_preference
        ),
        min_quality_tier=floor,
        require_declared_quality=a.require_declared_quality or b.require_declared_quality,
        max_model_attempts=min(a.max_model_attempts, b.max_model_attempts),
    )


def model_policy_covers(
    upper: ModelPolicy | Mapping[str, Any] | None,
    lower: ModelPolicy | Mapping[str, Any] | None,
) -> bool:
    a = _model_policy_value(upper)
    b = _model_policy_value(lower)
    if (
        b.allowed not in {(_NO_MODEL_INTERSECTION,), ("__no_model_intersection__",)}
        and a.allowed
        and (not b.allowed or not set(b.allowed).issubset(a.allowed))
    ):
        return False
    if a.max_cost_usd is not None and (b.max_cost_usd is None or b.max_cost_usd > a.max_cost_usd):
        return False
    if _privacy_rank(b.privacy) > _privacy_rank(a.privacy):
        return False
    if a.min_quality_tier and _quality_rank(b.min_quality_tier) < _quality_rank(a.min_quality_tier):
        return False
    if a.require_tools and not b.require_tools:
        return False
    if a.require_declared_quality and not b.require_declared_quality:
        return False
    if b.max_model_attempts > a.max_model_attempts:
        return False
    if a.role != "primary" and b.role != a.role:
        return False
    return True


def capability_id_permitted(capability_id: str, policy: CapabilityPolicy | None) -> bool:
    """Return whether an id may enter a task's visible capability surface.

    This is the same fail-closed ID ceiling the dispatcher enforces at call
    time. Keeping the predicate beside the policy prevents context compilation
    from advertising capabilities that the execution boundary will reject.

    Semantics (P0): visible/callable = allow ∪ ask − deny. An ``ask`` entry
    is callable but carries a task-level forced approval; it must not be
    masked by the presence of an ``allow`` list. ``deny`` is a hard exclude
    that wins over both.
    """
    policy = effective_capability_policy(policy)
    if policy is None:
        return True
    if capability_id in policy.deny or "*" in policy.deny:
        return False
    if policy.allow or policy.ask:
        return capability_id in policy.allow or capability_id in policy.ask
    return True


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
    # ``required_packs`` is an optional durable startup dependency contract.
    # Pack failures remain non-blocking globally, but the service quarantines
    # a resumable task that explicitly names an unavailable pack.
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Service-owned execution authority computed at admission. None is
    # tolerated for legacy rows until normalization; transports cannot mint it.
    execution_plan: TaskExecutionPlan | None = None


@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    cost_usd: Decimal = Decimal(0)
    # False means one or more model attempts completed without a trustworthy
    # provider report or configured pricing. Numeric cost remains useful for
    # budget accounting, but surfaces must display the aggregate as unknown.
    cost_known: bool = True
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
    # ``None`` means the caller selected no transport-local default; the
    # service resolves its configured autonomy exactly once at admission.
    autonomy: AutonomyLevel | None = None
    attachments: tuple[ArtifactRef, ...] = ()
    requested_capabilities: frozenset[str] | None = None
    # Optional full authority controls for interface callers. The legacy
    # requested_capabilities field remains a shorthand for allow-only policy.
    capability_policy: CapabilityPolicy | None = None
    resource_budget: ResourceBudget | None = None
    deadline: datetime | None = None
    # Authority-bearing: explicit mutation mode (defaults to workspace or SUPERVISED default).
    mutation_mode: MutationMode | None = None
    # Authority-bearing: explicit acceptance criteria for the task.
    acceptance_criteria: tuple[Criterion, ...] = ()
    # Descriptive metadata (should not change where effects land or whether a task may complete).
    metadata: Mapping[str, Any] = field(default_factory=dict)


class TrustedTaskMetadata(dict):
    """In-process marker for framework-generated internal task metadata."""

    _athena_trusted = True


__all__ = [
    "TaskStatus",
    "TERMINAL_STATUSES",
    "FINAL_STATUSES",
    "PAUSED_STATUSES",
    "AutonomyLevel",
    "VerificationType",
    "VerificationSpec",
    "Criterion",
    "ContextRef",
    "PathRule",
    "NetworkPolicy",
    "WorkspaceSpec",
    "MutationMode",
    "ResourceBudget",
    "ResourceBudgetCeiling",
    "ModelPolicy",
    "CapabilityPolicy",
    "capability_id_permitted",
    "intersect_capability_policies",
    "capability_policy_covers",
    "intersect_resource_budgets",
    "resource_budget_covers",
    "intersect_model_policies",
    "model_policy_covers",
    "DeliverySpec",
    "WorkClass",
    "SpeculationDepth",
    "VerificationStrength",
    "TaskExecutionPlan",
    "TaskSpec",
    "TrustedTaskMetadata",
    "UsageSummary",
    "MutationRef",
    "TaskResult",
    "AgentRequest",
]
