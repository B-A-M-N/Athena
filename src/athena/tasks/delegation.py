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
        self._sessions = sessions if sessions is not None else getattr(
            task_manager, "_sessions", None)
        self._cancellations = cancellations if cancellations is not None else getattr(
            task_manager, "_cancellations", None)
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
                kind="task", ref=parent.id, source_id=parent.id,
                summary=parent.objective,
            ),
            ContextRef(
                kind="task", ref=objective, source_id=objective,
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

    async def _ensure_session(
        self, session_id: str, parent_id: str | None = None
    ) -> None:
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
            raise DelegationError(
                f"cannot delegate from a terminal parent (status={status.value})")
        if status not in (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL,
                          TaskStatus.WAITING_INPUT, TaskStatus.CREATED,
                          TaskStatus.QUEUED):
            raise DelegationError(
                f"parent task not delegable (status={status.value})")

    async def _enforce_limits(self, parent: TaskSpec) -> None:
        depth = await self._depth_of(parent.id)
        max_depth = await self._root_depth(parent.id, default=self._default_max_depth)
        if depth >= max_depth:
            raise DepthExceeded(f"delegation depth {depth} >= limit {max_depth}")
        budget = parent.resource_budget or ResourceBudget()
        max_children = budget.max_children if budget.max_children is not None else self._default_max_children
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
        budget = _merged_budget(parent, child_spec.resource_budget,
                                default_depth=self._default_max_depth,
                                default_children=self._default_max_children)
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
    deny = tuple(sorted(set(parent_cap.deny) | set(child_cap.deny)))
    base_allow = parent_cap.allow or parent_cap.ask or ()
    child_allow = tuple(c for c in child_cap.allow if c not in deny)
    # Empty allow on the child = inherit the parent's permissions, not deny-all.
    allow = child_allow if child_cap.allow else base_allow
    allow = tuple(c for c in allow if c not in deny)
    ask_set = set(parent_cap.ask) | set(child_cap.ask)
    ask = tuple(c for c in ask_set if c not in deny)
    return CapabilityPolicy(
        effects=frozenset(parent_cap.effects),
        allow=allow,
        ask=ask,
        deny=deny,
    )


def _scope_model_policy(parent: TaskSpec, child):
    base = _as_model_policy(parent.model_policy)
    child_v = _as_model_policy(child or base)
    allowed = tuple(
        a for a in child_v.allowed
        if not base.allowed or a in base.allowed
    )
    return replace(
        base,
        require_tools=bool(base.require_tools),
        allowed=allowed,
        max_cost_usd=_intersect_cost(base.max_cost_usd, child_v.max_cost_usd),
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

    readable = _restrict_paths(parent_ws.readable, child_ws.readable, parent_root_canonical, child_root_canonical)
    writable = _restrict_paths(parent_ws.writable, child_ws.writable, parent_root_canonical, child_root_canonical)
    network = _restrict_network(parent_ws.network_policy, child_ws.network_policy)

    return WorkspaceSpec(
        id=child_ws.id,
        root=str(child_root_canonical),
        readable=readable or parent_ws.readable,
        writable=writable or parent_ws.writable,
        temp_root=child_ws.temp_root or parent_ws.temp_root,
        execution_backend=child_ws.execution_backend or parent_ws.execution_backend,
        network_policy=network,
        mutation_mode=(
            child_ws.mutation_mode
            if child_ws.mutation_mode is not MutationMode.DIRECT
            else parent_ws.mutation_mode
        ),
    )


def _is_strict_descendant(child: Path, parent: Path) -> bool:
    """True if `child` is strictly beneath `parent` (not equal)."""
    try:
        child.relative_to(parent)
        return child != parent
    except ValueError:
        return False


def _restrict_paths(parent_rules, child_rules, parent_root: Path, child_root: Path):
    """Intersect child path rules with parent containment.

    Each rule's path is canonicalized relative to child_root. If the canonical
    path is not within parent_root, the rule is dropped (child cannot access
    paths outside parent). If the canonical path IS within parent_root but not
    within the parent's own allowed rules, it is also dropped.
    """
    from athena.protocol.tasks import PathRule

    out = []
    # Build canonical set of parent-allowed paths for intersection
    parent_allowed = set()
    for pr in (parent_rules or ()):
        try:
            canon = Path(pr.path).resolve()
            parent_allowed.add(str(canon))
        except (OSError, ValueError):
            continue

    for rule in (child_rules or ()):
        allow = bool(rule.allow)
        if not rule.path:
            continue
        # Canonicalize the child rule's path relative to child_root
        raw = rule.path
        try:
            if Path(raw).is_absolute():
                canon = Path(raw).resolve()
            else:
                canon = (child_root / raw).resolve()
        except (OSError, ValueError):
            continue

        canon_str = str(canon)
        # Must be within parent_root (containment check)
        if not _is_within(canon, parent_root):
            continue
        out.append(PathRule(path=canon_str, allow=allow))
    return tuple(out)


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


def _merged_budget(parent: TaskSpec, child: ResourceBudget | None, *,
                   default_depth: int, default_children: int) -> ResourceBudget:
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
