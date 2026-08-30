from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from athena.protocol.errors import TaskError
from athena.protocol.ids import new_id
from athena.protocol.tasks import (
    CapabilityPolicy,
    ContextRef,
    ResourceBudget,
    TaskResult,
    TaskSpec,
    TaskStatus,
    TERMINAL_STATUSES,
)

__all__ = [
    "DelegationManager",
    "DelegationError",
    "DepthExceeded",
    "ChildLimitExceeded",
]


class DelegationError(TaskError):
    code = "delegation_error"


class DepthExceeded(DelegationError):
    code = "delegation_depth_exceeded"


class ChildLimitExceeded(DelegationError):
    code = "delegation_child_limit_exceeded"


_DEFAULT_MAX_DEPTH = 1
_DEFAULT_MAX_CHILDREN = 4
_NO_CAPABILITY_INTERSECTION = "__athena_no_capability_intersection__"
_NO_EFFECT_INTERSECTION = "__athena_no_effect_intersection__"
_NO_MODEL_INTERSECTION = "__athena_no_model_intersection__"


class DelegationManager:
    """Child-task creation and collection (BUILDSPEC §69-73, BHV-002).

    Delegation is task creation, not a special subagent framework (§69). A child
    is a ``Task`` whose ``parent_task_id`` points at the delegating task, and it
    runs through the SAME :class:`~athena.kernel.kernel.AgentKernel` (INV-001).

    Isolation (§70): the child receives a scoped capability policy and a budget
    derived from the parent, never the parent's full privileges. Children MAY
    hold a local ceiling but aggregate usage rolls up to the ancestor budget
    (§19). Depth is bounded by the ancestor ``max_child_depth``, default 1 (§71).
    """

    def __init__(
        self,
        *,
        task_manager: Any,
        kernel: Any = None,
        budgets: Any = None,
        sessions: Any = None,
        cancellations: Any = None,
        default_max_depth: int = _DEFAULT_MAX_DEPTH,
        default_max_children: int = _DEFAULT_MAX_CHILDREN,
    ) -> None:
        self._tasks = task_manager
        self._kernel = kernel
        self._budgets = budgets
        self._sessions = (
            sessions if sessions is not None else getattr(task_manager, "_sessions", None)
        )
        self._cancellations = (
            cancellations
            if cancellations is not None
            else getattr(task_manager, "_cancellations", None)
        )
        self._default_max_depth = default_max_depth
        self._default_max_children = default_max_children

    # ------------------------------------------------------------------ #
    # DelegateCapability handle (capabilities/delegate.py)
    # ------------------------------------------------------------------ #
    async def spawn_child(
        self,
        *,
        objective: str,
        parent_task_id: str,
        metadata: dict | None = None,
        context: tuple[ContextRef, ...] = (),
    ) -> str:
        parent = await self._tasks.get(parent_task_id)
        # A child carries its OWN fresh session (lineage only, no transcript
        # reuse, P0-15). Its session is created with the parent session as its
        # lineage root so ancestry is preserved without inheriting the parent's
        # full live transcript.
        child_session = new_id("session")
        await self._ensure_session(child_session, parent_id=parent.session_id)
        context_refs = (
            ContextRef(
                kind="task",
                ref=parent.id,
                source_id=parent.id,
                summary=parent.objective,
            ),
            ContextRef(
                kind="task",
                ref=objective,
                source_id=objective,
                summary=objective,
            ),
        ) + tuple(context or ())
        child_spec = TaskSpec(
            # No scope is pre-applied here: delegate/_scope_child performs the
            # SINGLE merge of the parent budget and policy (§71) so depth is
            # decremented exactly once.
            id=new_id("task"),
            objective=objective,
            session_id=child_session,
            parent_task_id=parent.id,
            context_refs=context_refs,
            metadata=dict(metadata or {}),
        )
        created = await self.delegate(parent_task=parent, child_spec=child_spec)
        # Enqueue so the shared worker actually runs the child (P0-16).
        await self._tasks.enqueue(created.id)
        return created.id

    # ------------------------------------------------------------------ #
    async def delegate(self, *, parent_task: TaskSpec, child_spec: TaskSpec, **kw: Any):
        await self._assert_parent_runnable(parent_task)
        await self._enforce_limits(parent_task)

        scoped = self._scope_child(parent_task, child_spec)
        child = replace(
            scoped,
            id=child_spec.id or new_id("task"),
            parent_task_id=parent_task.id,
            session_id=child_spec.session_id or parent_task.session_id,
        )
        created = await self._tasks.create(child)
        if self._budgets is not None:
            self._budgets.record_child(parent_task.id)
            persist_budget = getattr(self._budgets, "_persist_usage", None)
            if persist_budget is not None:
                await persist_budget(parent_task.id)
        return created

    # ------------------------------------------------------------------ #
    async def run_child(self, child_task_id: str) -> TaskResult:
        if self._kernel is None:
            raise DelegationError("no kernel bound; cannot run child task")
        return await self._kernel.run_task(child_task_id)

    async def status_of(self, child_task_id: str) -> TaskStatus:
        """Report the child's current :class:`TaskStatus` (P0-17/status)."""
        task = await self._tasks.get(child_task_id)
        return _status_of(task)

    async def is_descendant(self, parent_task_id: str, child_task_id: str) -> bool:
        """Return whether ``child_task_id`` belongs to the parent's subtree."""
        if not parent_task_id or not child_task_id or parent_task_id == child_task_id:
            return False
        current = child_task_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            task = await self._tasks.get(current)
            if task is None:
                return False
            current = task.parent_task_id
            if current == parent_task_id:
                return True
        return False

    async def get_result(self, child_task_id: str) -> TaskResult | None:
        """Return the child's terminal result, or ``None`` while still active."""
        result = await self._tasks.get_result(child_task_id)
        if result is not None:
            return result
        task = await self._tasks.get(child_task_id)
        status = _status_of(task)
        if status in TERMINAL_STATUSES:
            return TaskResult(task_id=child_task_id, status=status)
        return None

    async def collect(
        self,
        child_task_id: str,
        *,
        timeout: float | None = None,
        poll_seconds: float = 0.2,
    ) -> TaskResult:
        """Wait for the child and return its :class:`TaskResult` (P0-17/collect)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = await self.get_result(child_task_id)
            if result is not None:
                return result
            if deadline is not None and time.monotonic() >= deadline:
                task = await self._tasks.get(child_task_id)
                return TaskResult(task_id=child_task_id, status=_status_of(task))
            await asyncio.sleep(poll_seconds)

    async def cancel_child(
        self, child_task_id: str, *, reason: str = "delegate.cancel"
    ) -> TaskStatus:
        """Cancel a child task (P0-17/cancel)."""
        if self._cancellations is not None:
            try:
                return await self._cancellations.cancel(child_task_id, reason=reason)
            except Exception:
                pass
        task = await self._tasks.get(child_task_id)
        status = _status_of(task)
        if status not in TERMINAL_STATUSES:
            await self._tasks.transition(child_task_id, TaskStatus.CANCELLED)
        return TaskStatus.CANCELLED

    async def collect_results(self, child_task_id: str) -> TaskResult:
        result = await self._tasks.get_result(child_task_id)
        if result is not None:
            return result
        task = await self._tasks.get(child_task_id)
        status = _status_of(task)
        if status in TERMINAL_STATUSES:
            return TaskResult(task_id=child_task_id, status=status)
        result = await self.run_child(child_task_id)
        return result

    async def _ensure_session(self, session_id: str, parent_id: str | None = None) -> None:
        if self._sessions is None:
            return
        existing = await self._sessions.get(session_id)
        if existing is None:
            await self._sessions.create(session_id, parent_id=parent_id)

    # ------------------------------------------------------------------ #
    async def _assert_parent_runnable(self, parent: TaskSpec) -> None:
        status = _status_of(parent)
        if status == TaskStatus.CANCELLED:
            raise DelegationError("parent task is cancelled")
        if status in TERMINAL_STATUSES:
            raise DelegationError(f"cannot delegate from a terminal parent (status={status.value})")
        if status not in (
            TaskStatus.RUNNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_INPUT,
            TaskStatus.CREATED,
            TaskStatus.QUEUED,
        ):
            raise DelegationError(f"parent task not delegable (status={status.value})")

    async def _enforce_limits(self, parent: TaskSpec) -> None:
        depth = await self._depth_of(parent.id)
        max_depth = await self._root_depth(parent.id, default=self._default_max_depth)
        if depth >= max_depth:
            raise DepthExceeded(f"delegation depth {depth} >= limit {max_depth}")
        budget = parent.resource_budget or ResourceBudget()
        max_children = (
            budget.max_children if budget.max_children is not None else self._default_max_children
        )
        if await self._count_children(parent.id) >= max_children:
            raise ChildLimitExceeded(f"parent reached max_children={max_children}")

    async def _root_depth(self, parent_id: str, *, default: int) -> int:
        """The original ancestor chain's authorised ``max_child_depth``.

        Each descendant's budget has this value decremented per level for
        reporting, but a task may still delegate while its absolute tree depth
        is below the root's ceiling, so depth is checked against the topmost
        ancestor's limit (§71).
        """
        cur = parent_id
        root_budget = None
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            try:
                spec = await self._tasks.get(cur)
            except Exception:
                break
            if spec is None:
                break
            if spec.resource_budget is not None:
                root_budget = spec.resource_budget
            cur = spec.parent_task_id
        if root_budget is not None and root_budget.max_child_depth is not None:
            return root_budget.max_child_depth
        return default

    async def _count_children(self, parent_id: str) -> int:
        store = getattr(self._tasks, "_store", None)
        if store is not None:
            try:
                return await store.count_children(parent_id)
            except Exception:
                return 0
        if self._budgets is None:
            return 0
        count = getattr(self._budgets, "child_count", None)
        if callable(count):
            return int(count(parent_id))
        ledgers = getattr(self._budgets, "_ledger", None)
        if isinstance(ledgers, dict):
            entry = ledgers.get(parent_id)
            if entry is not None:
                return int(getattr(entry, "children", 0))
        return 0

    async def _depth_of(self, task_id: str) -> int:
        depth = 0
        seen: set[str] = set()
        cur = task_id
        while cur and cur not in seen:
            seen.add(cur)
            try:
                spec = await self._tasks.get(cur)
            except Exception:
                break
            if spec is None or not spec.parent_task_id:
                break
            depth += 1
            cur = spec.parent_task_id
        return depth

    # ------------------------------------------------------------------ #
    def _scope_child(self, parent: TaskSpec, child_spec: TaskSpec) -> TaskSpec:
        limited_policy = _scope_policy(parent, child_spec.capability_policy)
        budget = _merged_budget(
            parent,
            child_spec.resource_budget,
            default_depth=self._default_max_depth,
            default_children=self._default_max_children,
        )
        return replace(
            child_spec,
            parent_task_id=parent.id,
            workspace=_scope_workspace(parent, child_spec.workspace),
            capability_policy=limited_policy,
            model_policy=_scope_model_policy(parent, child_spec.model_policy),
            resource_budget=budget,
        )


def _scope_policy(parent: TaskSpec, child: CapabilityPolicy | None = None) -> CapabilityPolicy:
    parent = parent or TaskSpec(id="", objective="")
    parent_cap = parent.capability_policy or CapabilityPolicy()
    child_cap = child or CapabilityPolicy()
    parent_allow = set(parent_cap.allow)
    parent_ask = set(parent_cap.ask)
    child_requested = set(child_cap.allow)
    if child_requested:
        # An empty parent allowlist is the protocol's unrestricted value. A
        # child request narrows that open ceiling to the capabilities it asks
        # for; it must not become impossible merely because the parent did
        # not enumerate every native capability.
        allow_set = child_requested & parent_allow if parent_allow else child_requested
        ask_set = parent_ask & child_requested
        outside = child_requested - parent_allow - parent_ask
    else:
        allow_set = parent_allow
        ask_set = parent_ask
        outside = set()
    deny_set = set(parent_cap.deny) | set(child_cap.deny) | outside
    if (parent_allow or parent_ask) and not (allow_set or ask_set):
        # Empty ``allow`` means unrestricted to the dispatcher, so use an
        # impossible sentinel when two non-empty ceilings have no overlap.
        allow_set = {_NO_CAPABILITY_INTERSECTION}
    allow = tuple(sorted(c for c in allow_set if c not in deny_set))
    ask = tuple(sorted(c for c in ask_set if c not in deny_set))
    effects = set(parent_cap.effects)
    if child_cap.effects:
        effects = effects & set(child_cap.effects) if effects else set(child_cap.effects)
    if parent_cap.effects and child_cap.effects and not effects:
        effects = {_NO_EFFECT_INTERSECTION}
    return CapabilityPolicy(
        effects=frozenset(effects),
        allow=allow,
        ask=ask,
        deny=tuple(sorted(deny_set)),
    )


def _scope_model_policy(parent: TaskSpec, child):
    base = _as_model_policy(parent.model_policy)
    child_v = _as_model_policy(child or base)
    allowed = tuple(a for a in child_v.allowed if not base.allowed or a in base.allowed)
    if base.allowed and child_v.allowed and not allowed:
        allowed = (_NO_MODEL_INTERSECTION,)
    return replace(
        base,
        require_tools=bool(base.require_tools or child_v.require_tools),
        privacy=_stricter_privacy(base.privacy, child_v.privacy),
        allowed=allowed,
        max_cost_usd=_intersect_cost(base.max_cost_usd, child_v.max_cost_usd),
        routing_preference=(
            child_v.routing_preference
            if child_v.routing_preference != "balanced"
            else base.routing_preference
        ),
    )


def _as_model_policy(value):
    from athena.protocol.tasks import ModelPolicy

    if isinstance(value, ModelPolicy):
        return value
    return ModelPolicy(
        role=getattr(value, "role", "primary"),
        allowed=tuple(getattr(value, "allowed", ()) or ()),
        require_tools=bool(getattr(value, "require_tools", True)),
        privacy=getattr(value, "privacy", "local-preferred"),
        max_cost_usd=getattr(value, "max_cost_usd", None),
        routing_preference=getattr(value, "routing_preference", "balanced"),
    )


def _intersect_cost(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _scope_workspace(parent: TaskSpec, child):
    """Derive a child workspace strictly contained under the parent (§70).

    The child root MUST be a strict descendant of the parent root (not equal).
    Readable/writable path rules are canonicalized relative to the child root
    and intersected with the parent's real canonical paths.
    """
    from athena.protocol.tasks import MutationMode, WorkspaceSpec

    parent_ws = parent.workspace
    if parent_ws is None:
        return child
    child_ws = child or WorkspaceSpec(id=parent_ws.id + "/child", root="")

    # Canonicalize parent root
    parent_root_canonical = Path(parent_ws.root).resolve()

    # Resolve child root: if empty, derive under parent; if supplied, validate
    root = child_ws.root
    if not root:
        root = str(parent_root_canonical / "tasks" / (child_ws.id or "child"))

    child_root_canonical = Path(root).resolve()

    # Strict descendant check: child must be UNDER parent, not equal, not sibling
    if not _is_strict_descendant(child_root_canonical, parent_root_canonical):
        # Override: force the child root under the parent
        root = str(parent_root_canonical / "tasks" / (child_ws.id or "child"))
        child_root_canonical = Path(root).resolve()

    readable = _restrict_paths(
        parent_ws.readable, child_ws.readable, parent_root_canonical, child_root_canonical
    )
    writable = _restrict_paths(
        parent_ws.writable, child_ws.writable, parent_root_canonical, child_root_canonical
    )
    network = _restrict_network(parent_ws.network_policy, child_ws.network_policy)

    parent_temp = _canonical_workspace_path(parent_ws.temp_root, parent_root_canonical)
    if parent_temp is None:
        parent_temp = parent_root_canonical
    requested_temp = _canonical_workspace_path(child_ws.temp_root, child_root_canonical)
    if requested_temp is None:
        requested_temp = child_root_canonical / ".tmp"
    if not _is_within(requested_temp, parent_temp) or not _is_within(
        requested_temp, parent_root_canonical
    ):
        requested_temp = child_root_canonical / ".tmp"

    parent_backend = parent_ws.execution_backend or "local"
    child_backend = child_ws.execution_backend
    if child_backend is None:
        effective_backend = parent_backend
    else:
        effective_backend = _monotonic_backend(parent_backend, child_backend)

    return WorkspaceSpec(
        id=child_ws.id,
        root=str(child_root_canonical),
        readable=readable,
        writable=writable,
        temp_root=str(requested_temp),
        execution_backend=effective_backend,
        network_policy=network,
        mutation_mode=(
            child_ws.mutation_mode
            if child_ws.mutation_mode is not MutationMode.DIRECT
            else parent_ws.mutation_mode
        ),
        revision=child_ws.revision or parent_ws.revision,
    )


def _is_strict_descendant(child: Path, parent: Path) -> bool:
    """True if `child` is strictly beneath `parent` (not equal)."""
    try:
        child.relative_to(parent)
        return child != parent
    except ValueError:
        return False


def _restrict_paths(parent_rules, child_rules, parent_root: Path, child_root: Path):
    """Return the canonical intersection of two prefix path policies.

    An empty rule tuple means the corresponding workspace root is implicitly
    allowed.  The returned policy is always explicit, including a deny rule
    at ``child_root`` when the intersection is empty.  This matters because
    downstream scope checkers interpret an empty tuple as unrestricted.

    ``Path.resolve(strict=False)`` canonicalizes existing symlink components;
    every candidate is then checked against both roots and both allow sets.
    Deny rules are retained when they overlap the surviving allow region, so
    a parent deny cannot be erased by a child allow rule.
    """
    from athena.protocol.tasks import PathRule

    parent = _canonical_rules(parent_rules, parent_root, parent_root)
    child = _canonical_rules(child_rules, child_root, child_root)
    if not parent:
        parent = [(parent_root, True)]
    if not child:
        child = [(child_root, True)]

    parent_allows = [path for path, allow in parent if allow and _is_within(path, parent_root)]
    child_allows = [path for path, allow in child if allow and _is_within(path, child_root)]
    candidates: list[Path] = []
    for parent_allow in parent_allows:
        for child_allow in child_allows:
            overlap = _prefix_intersection(parent_allow, child_allow)
            if overlap is None:
                continue
            if _is_within(overlap, parent_root) and _is_within(overlap, child_root):
                candidates.append(overlap)

    denies = [
        path
        for path, allow in (*parent, *child)
        if not allow and (_is_within(path, parent_root) or _is_within(path, child_root))
    ]
    surviving: list[Path] = []
    for candidate in _unique_paths(candidates):
        # A deny ancestor/equal to the candidate removes that whole region.
        if any(_is_within(candidate, deny) for deny in denies):
            continue
        surviving.append(candidate)

    out: list[PathRule] = [PathRule(path=str(path), allow=True) for path in surviving]
    for deny in _unique_paths(denies):
        if any(_is_within(deny, candidate) for candidate in surviving):
            out.append(PathRule(path=str(deny), allow=False))
    if not out:
        out.append(PathRule(path=str(child_root), allow=False))
    return tuple(out)


def _canonical_workspace_path(value: str | None, base: Path) -> Path | None:
    if not value:
        return None
    raw = Path(value)
    return (raw if raw.is_absolute() else base / raw).resolve(strict=False)


def _canonical_rules(rules, base: Path, root: Path) -> list[tuple[Path, bool]]:
    result: list[tuple[Path, bool]] = []
    for rule in rules or ():
        if not getattr(rule, "path", None):
            continue
        path = _canonical_workspace_path(str(rule.path), base)
        if path is None:
            continue
        result.append((path, bool(rule.allow)))
    return result


def _prefix_intersection(left: Path, right: Path) -> Path | None:
    if _is_within(left, right):
        return left
    if _is_within(right, left):
        return right
    return None


def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        value = str(path)
        if value not in seen:
            seen.add(value)
            result.append(path)
    return result


def _monotonic_backend(parent: str, child: str) -> str:
    """Reject a delegated backend that weakens the parent's isolation."""
    strength = {
        "local": 0,
        "sandbox": 1,
        "sandboxed-local": 1,
        "verification": 1,
        "shadow": 1,
        "container": 2,
    }
    parent_strength = strength.get(parent)
    child_strength = strength.get(child)
    if parent_strength is None or child_strength is None:
        if child != parent:
            raise ValueError(f"unknown or incompatible delegated execution backend: {child!r}")
        return child
    if child_strength < parent_strength:
        raise ValueError(
            f"delegated execution backend {child!r} is weaker than parent backend {parent!r}"
        )
    return child


def _stricter_privacy(left: str, right: str) -> str:
    rank = {
        "offline": 0,
        "local": 0,
        "local-preferred": 1,
        "local-pref": 1,
        "remote": 2,
    }
    left_value = str(left or "local-preferred")
    right_value = str(right or "local-preferred")
    return left_value if rank.get(left_value, 0) <= rank.get(right_value, 0) else right_value


def _is_within(path: Path, base: Path) -> bool:
    """True if `path` is equal to or beneath `base` (canonical)."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _restrict_network(parent, child):
    from athena.protocol.tasks import NetworkPolicy

    if parent == NetworkPolicy.DENY or child == NetworkPolicy.DENY:
        return NetworkPolicy.DENY
    if parent == NetworkPolicy.RESTRICTED or child == NetworkPolicy.RESTRICTED:
        return NetworkPolicy.RESTRICTED
    return NetworkPolicy.ALLOW


def _merged_budget(
    parent: TaskSpec, child: ResourceBudget | None, *, default_depth: int, default_children: int
) -> ResourceBudget:
    base = parent.resource_budget or ResourceBudget()
    desired = child or ResourceBudget()
    merged = base.merged_with(desired)
    children = merged.max_children if merged.max_children is not None else default_children
    # Decrement the authorised depth exactly once (§71): a child budget at the
    # library default carries no explicit depth ceiling (default == 1), so it
    # must not cap the parent's allowance; only an explicitly raised value
    # tightens it before the single decrement.
    base_depth = base.max_child_depth if base.max_child_depth is not None else default_depth
    explicit = desired.max_child_depth != 1
    if explicit:
        depth = min(base_depth, desired.max_child_depth)
    else:
        depth = base_depth
    return ResourceBudget(
        max_agent_iterations=merged.max_agent_iterations,
        max_input_tokens=merged.max_input_tokens,
        max_output_tokens=merged.max_output_tokens,
        max_cost_usd=merged.max_cost_usd,
        max_wall_time=merged.max_wall_time,
        max_children=children,
        max_child_depth=max(0, depth - 1),
        max_parallel_model_calls=merged.max_parallel_model_calls,
        max_parallel_executions=merged.max_parallel_executions,
        max_artifact_bytes=merged.max_artifact_bytes,
    )


def _status_of(task: TaskSpec) -> TaskStatus:
    if task is None:
        return TaskStatus.CANCELLED
    meta = task.metadata or {}
    raw = meta.get("status")
    if raw:
        try:
            return TaskStatus(raw)
        except ValueError:
            pass
    return TaskStatus.CREATED
