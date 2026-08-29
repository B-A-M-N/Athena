from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from contextlib import asynccontextmanager
from threading import Lock
import time
from typing import Any, Mapping

from athena.protocol.messages import utcnow
from athena.protocol.tasks import ResourceBudget, TaskSpec, UsageSummary

__all__ = [
    "BudgetTracker",
    "BudgetStateUnavailable",
    "Usage",
    "DefaultBudget",
    "exceeded_by_budget",
]


class BudgetStateUnavailable(RuntimeError):
    """Durable budget authority could not be read or decoded.

    A missing task remains a ``KeyError``. This exception identifies an
    existing task whose restrictive budget cannot be established safely.
    """


@dataclass
class Usage:
    """Per-task own-consumption ledger (in-memory, centralised accounting)."""

    iterations: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = field(default_factory=Decimal)
    executions: int = 0
    mutations: int = 0
    children: int = 0
    artifact_bytes: int = 0
    started: datetime = field(default_factory=utcnow)
    wall_time_s: float = 0.0

    def add(self, u: "Usage | UsageSummary | Mapping | None") -> None:
        if u is None:
            return
        self.iterations += _int(u, "iterations")
        self.model_calls += _int(u, "model_calls")
        self.input_tokens += _int(u, "input_tokens")
        self.output_tokens += _int(u, "output_tokens")
        self.cost += _dec(u, "cost", "cost_usd")
        self.executions += _int(u, "executions")
        self.mutations += _int(u, "mutations")
        self.children += _int(u, "children")
        self.artifact_bytes += _int(u, "artifact_bytes")
        self.wall_time_s += _float(u, "wall_time_s")

    def as_usage_summary(self) -> UsageSummary:
        return UsageSummary(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            model_calls=self.model_calls,
            cost_usd=self.cost,
            duration_ms=_duration_ms(self),
            executions=self.executions,
            mutations=self.mutations,
        )


def _int(u: Any, key: str) -> int:
    try:
        val = u.get(key) if hasattr(u, "get") else getattr(u, key)
    except AttributeError:
        val = None
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _float(u: Any, key: str) -> float:
    try:
        val = u.get(key) if hasattr(u, "get") else getattr(u, key)
    except AttributeError:
        val = None
    try:
        return max(0.0, float(val or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _dec(u: Any, *keys: str) -> Decimal:
    for key in keys:
        try:
            val = u.get(key) if hasattr(u, "get") else getattr(u, key)
        except AttributeError:
            continue
        try:
            return Decimal(str(val or "0") or "0")
        except (TypeError, ValueError):
            continue
    return Decimal("0")


def _duration_ms(u: Usage) -> int:
    return int(u.wall_time_s * 1000)


@dataclass(frozen=True)
class DefaultBudget:
    max_agent_iterations: int = 100
    max_children: int = 4
    max_child_depth: int = 1


def exceeded_by_budget(budget: ResourceBudget | None, u: Usage) -> bool:
    if budget is None:
        return False
    if budget.max_agent_iterations and u.iterations >= budget.max_agent_iterations:
        return True
    if budget.max_input_tokens is not None and u.input_tokens >= budget.max_input_tokens:
        return True
    if budget.max_output_tokens is not None and u.output_tokens >= budget.max_output_tokens:
        return True
    if budget.max_cost_usd is not None and u.cost >= budget.max_cost_usd:
        return True
    if budget.max_artifact_bytes is not None and u.artifact_bytes >= budget.max_artifact_bytes:
        return True
    if budget.max_children and u.children >= budget.max_children:
        return True
    if budget.max_wall_time is not None and _elapsed_s(u) >= budget.max_wall_time.total_seconds():
        return True
    return False


def _elapsed_s(u: Usage) -> float:
    # This is active compute time, not time spent waiting for an operator or
    # with the service stopped.
    return u.wall_time_s


class BudgetTracker:
    """Centralised resource accounting (BUILDSPEC §19).

    Consumption is recorded per task in an in-memory ledger guarded by a lock so
    concurrent workers account safely. Because children MUST consume from the
    parent/root budget, ``remaining`` and ``exhausted`` aggregate a task's own
    ledger with that of every descendant reachable through the store's parent
    links (methods that read the store are async).
    """

    def __init__(
        self,
        *,
        task_store: Any = None,
        default: DefaultBudget | None = None,
        budget_resolver: Any = None,
    ) -> None:
        self._store = task_store
        self._budget_resolver = budget_resolver
        self._default = default or DefaultBudget()
        self._lock = Lock()
        self._ledger: dict[str, Usage] = {}
        self._budgets: dict[str, ResourceBudget] = {}
        self._parent: dict[str, str | None] = {}
        self._artifact_reservations: dict[str, int] = {}
        self._model_cost_reservations: dict[str, Decimal] = {}
        self._model_cost_by_task: dict[str, Decimal] = {}
        self._usage_hydrated: set[str] = set()
        self._model_semaphores: dict[str, Any] = {}
        self._model_limits: dict[str, int] = {}
        self._execution_semaphores: dict[str, Any] = {}
        self._execution_limits: dict[str, int] = {}
        # task_id -> (process-local monotonic checkpoint, durable UTC marker)
        self._active_compute: dict[str, tuple[float, datetime]] = {}
        import asyncio

        self._artifact_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Registration / consumption
    # ------------------------------------------------------------------ #
    def register(self, task: TaskSpec) -> None:
        with self._lock:
            self._ledger.setdefault(task.id, Usage())
            self._budgets[task.id] = task.resource_budget or ResourceBudget()
            self._parent[task.id] = task.parent_task_id

    def reset(self, task_id: str) -> None:
        with self._lock:
            self._ledger[task_id] = Usage()

    def consume(self, task_id: str, usage: Any = None, **kw: Any) -> None:
        agent = Usage()
        agent.add(usage)
        if kw:
            agent.add(kw)
        if _all_zero(agent):
            return
        with self._lock:
            entry = self._ledger.setdefault(task_id, Usage())
            entry.add(agent)

    def consume_result(self, task_id: str, summary: UsageSummary) -> None:
        if summary is not None:
            # Live model turns checkpoint their usage before finalization so a
            # restart cannot reset the ledger. Finalization may present the
            # aggregate result again; consume only the delta so this remains
            # idempotent for result attachment after a restart.
            current = self.own(task_id)
            self.consume(
                task_id,
                dict(
                    input_tokens=max(0, summary.input_tokens - current.input_tokens),
                    output_tokens=max(0, summary.output_tokens - current.output_tokens),
                    model_calls=max(0, summary.model_calls - current.model_calls),
                    cost=max(Decimal("0"), summary.cost_usd - current.cost),
                    executions=max(0, summary.executions - current.executions),
                    mutations=max(0, summary.mutations - current.mutations),
                ),
            )

    def record_child(self, parent_id: str) -> None:
        with self._lock:
            self._ledger.setdefault(parent_id, Usage())
            self._ledger[parent_id].children += 1

    async def begin_compute(self, task_id: str) -> None:
        """Start a durable active-compute interval for a running task."""
        await self._hydrate_usage(task_id)
        with self._lock:
            self._active_compute.setdefault(task_id, (time.monotonic(), utcnow()))
        await self._persist_usage(task_id)

    async def end_compute(self, task_id: str) -> None:
        """Checkpoint and close the task's active-compute interval."""
        await self._persist_usage(task_id)
        with self._lock:
            self._active_compute.pop(task_id, None)
        await self._persist_usage(task_id)

    def current(self, task_id: str) -> UsageSummary:
        with self._lock:
            return self._ledger.get(task_id, Usage()).as_usage_summary()

    def own(self, task_id: str) -> Usage:
        with self._lock:
            entry = self._ledger.get(task_id)
            if entry is None:
                return Usage()
            return Usage(
                iterations=entry.iterations,
                model_calls=entry.model_calls,
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cost=entry.cost,
                executions=entry.executions,
                mutations=entry.mutations,
                children=entry.children,
                artifact_bytes=entry.artifact_bytes,
                started=entry.started,
                wall_time_s=entry.wall_time_s,
            )

    # ------------------------------------------------------------------ #
    # Budget resolution
    # ------------------------------------------------------------------ #
    async def budget_of_async(self, task_id: str) -> ResourceBudget:
        await self._ensure_task_budget_metadata(task_id)
        with self._lock:
            return self._budgets[task_id]

    def budget_of(self, task_id: str) -> ResourceBudget:
        with self._lock:
            if task_id in self._budgets:
                return self._budgets[task_id]
        spec = self._resolve_spec(task_id)
        if spec is not None:
            with self._lock:
                self._budgets[task_id] = spec.resource_budget or ResourceBudget()
                self._parent[task_id] = spec.parent_task_id
            return self._budgets[task_id]
        if self._store is not None:
            raise BudgetStateUnavailable(
                f"budget state for {task_id} requires asynchronous durable loading"
            )
        raise KeyError(f"task not found: {task_id}")

    def _resolve_spec(self, task_id: str):
        if self._budget_resolver is not None:
            return self._budget_resolver(task_id)
        return None

    async def _load_from_store_async(self, task_id: str) -> bool:
        if self._store is None:
            raise BudgetStateUnavailable(f"durable budget store unavailable for task {task_id}")
        try:
            row = await self._store.get(task_id)
        except Exception as exc:  # noqa: BLE001 - authority failure must propagate
            raise BudgetStateUnavailable(
                f"could not read durable budget state for task {task_id}"
            ) from exc
        if not row:
            return False
        try:
            raw_budget = row.get("resource_budget")
            rb = _deserialize_budget(raw_budget, strict=True) if raw_budget else ResourceBudget()
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise BudgetStateUnavailable(
                f"malformed durable budget state for task {task_id}"
            ) from exc
        with self._lock:
            self._budgets[task_id] = rb
            self._parent[task_id] = row.get("parent_task_id")
        return True

    async def _ensure_task_budget_metadata(self, task_id: str) -> None:
        """Load one task's budget and parent link before using its lineage."""
        with self._lock:
            if task_id in self._budgets and task_id in self._parent:
                return
        spec = self._resolve_spec(task_id)
        if spec is not None:
            with self._lock:
                self._budgets[task_id] = spec.resource_budget or ResourceBudget()
                self._parent[task_id] = spec.parent_task_id
            return
        if await self._load_from_store_async(task_id):
            return
        raise KeyError(f"task not found: {task_id}")

    # ------------------------------------------------------------------ #
    # Aggregation (async; reads the store via parent links)
    # ------------------------------------------------------------------ #
    async def descendants(self, task_id: str) -> list[str]:
        if self._store is None:
            return []
        rows = await self._store.list_descendants(task_id)
        return [r["id"] for r in rows]

    async def _children_of(self, task_ids: list[str]) -> list[str]:
        if not task_ids or self._store is None:
            return []
        out: list[str] = []
        for task_id in task_ids:
            children = await self._store.list_children(task_id)
            out.extend(r["id"] for r in children)
        return list(dict.fromkeys(out))

    async def total(self, task_id: str) -> Usage:
        await self._hydrate_usage(task_id)
        self._checkpoint_active(task_id)
        own = self.own(task_id)
        agg = Usage()
        agg.add(own)
        children = await self.descendants(task_id)
        for child in children:
            # A fresh tracker only knows the task hierarchy from the store.
            # Restore every descendant before reading its own ledger, or a
            # root query immediately after restart silently forgets child use.
            await self._hydrate_usage(child)
            self._checkpoint_active(child)
            agg.add(self.own(child))
        agg.children = len(children)
        return agg

    async def remaining(self, task_id: str) -> Mapping[str, Any]:
        # Every reported ceiling is hierarchical.  A child must see the
        # smallest remaining capacity across its own and all ancestor ledgers;
        # otherwise preflight checks can admit a call that exceeds the root.
        limits: dict[str, Any] = {
            "iterations": None,
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
            "wall_time_s": None,
            "children": None,
            "artifact_bytes": None,
        }
        for ancestor in await self._ancestor_ids(task_id):
            budget = await self.budget_of_async(ancestor)
            total = await self.total(ancestor)
            artifact_reserved = self._artifact_reservations.get(ancestor, 0)
            model_reserved = self._model_cost_reservations.get(ancestor, Decimal("0"))
            values = {
                "iterations": _cap_remaining(budget.max_agent_iterations, total.iterations),
                "input_tokens": _cap_remaining(budget.max_input_tokens, total.input_tokens),
                "output_tokens": _cap_remaining(budget.max_output_tokens, total.output_tokens),
                "cost_usd": _cap_remaining_dec(
                    budget.max_cost_usd,
                    total.cost + model_reserved,
                ),
                "wall_time_s": (
                    None
                    if budget.max_wall_time is None
                    else max(0.0, budget.max_wall_time.total_seconds() - total.wall_time_s)
                ),
                "children": _cap_remaining(budget.max_children, total.children),
                "artifact_bytes": _cap_remaining(
                    budget.max_artifact_bytes, total.artifact_bytes + artifact_reserved
                ),
            }
            for key, value in values.items():
                if value is None:
                    continue
                limits[key] = value if limits[key] is None else min(limits[key], value)
        return {
            **limits,
        }

    async def exhausted(self, task_id: str) -> bool:
        # A task must also honour its ancestor/root budget: walk the parent
        # chain to the root and evaluate each ancestor against its own
        # aggregate subtree. The most restrictive ceiling (smallest per
        # dimension) already propagates through merged budgets, so once any
        # ancestor's rolled-up consumption is exhausted every descendant stops.
        for anc in await self._ancestor_ids(task_id):
            if exceeded_by_budget(await self.budget_of_async(anc), await self.total(anc)):
                return True
        return False

    async def reserve_artifact(self, task_id: str, size: int) -> None:
        """Reserve logical artifact bytes before an immutable blob is saved."""
        if size < 0:
            raise ValueError("artifact size must be non-negative")
        ancestors = await self._ancestor_ids(task_id)
        async with self._artifact_lock:
            for ancestor in ancestors:
                budget = await self.budget_of_async(ancestor)
                if budget.max_artifact_bytes is None:
                    continue
                used = (await self.total(ancestor)).artifact_bytes
                reserved = self._artifact_reservations.get(ancestor, 0)
                if used + reserved + size > budget.max_artifact_bytes:
                    raise ValueError(
                        f"artifact budget exceeded for task {ancestor}: "
                        f"{used + reserved + size} > {budget.max_artifact_bytes} bytes"
                    )
            for ancestor in ancestors:
                self._artifact_reservations[ancestor] = (
                    self._artifact_reservations.get(ancestor, 0) + size
                )
        for ancestor in ancestors:
            await self._persist_usage(ancestor)

    async def commit_artifact(self, task_id: str, size: int) -> None:
        """Commit bytes to the owning task and remove lineage reservations.

        Descendant consumption is aggregated by :meth:`total`, so copying
        committed bytes into every ancestor would double-count the same
        occurrence against the root budget.
        """
        ancestors = await self._ancestor_ids(task_id)
        async with self._artifact_lock:
            for ancestor in ancestors:
                self._artifact_reservations[ancestor] = max(
                    0, self._artifact_reservations.get(ancestor, 0) - size
                )
            self._ledger.setdefault(task_id, Usage()).artifact_bytes += size
        for ancestor in ancestors:
            await self._persist_usage(ancestor)

    async def release_artifact(self, task_id: str, size: int) -> None:
        ancestors = await self._ancestor_ids(task_id)
        for ancestor in ancestors:
            await self._hydrate_usage(ancestor)
        async with self._artifact_lock:
            for ancestor in ancestors:
                self._artifact_reservations[ancestor] = max(
                    0, self._artifact_reservations.get(ancestor, 0) - size
                )
        for ancestor in ancestors:
            await self._persist_usage(ancestor)

    async def reserve_model_cost(self, task_id: str, amount: Decimal) -> None:
        """Reserve a bounded worst-case model cost across the root lineage."""
        if amount < 0:
            raise ValueError("model cost reservation must be non-negative")
        ancestors = await self._ancestor_ids(task_id)
        async with self._artifact_lock:
            for ancestor in ancestors:
                budget = await self.budget_of_async(ancestor)
                if budget.max_cost_usd is None:
                    continue
                used = (await self.total(ancestor)).cost
                reserved = self._model_cost_reservations.get(ancestor, Decimal("0"))
                if used + reserved + amount > budget.max_cost_usd:
                    raise ValueError(
                        f"model cost budget exceeded for task {ancestor}: "
                        f"{used + reserved + amount} > {budget.max_cost_usd} USD"
                    )
            for ancestor in ancestors:
                self._model_cost_reservations[ancestor] = (
                    self._model_cost_reservations.get(ancestor, Decimal("0")) + amount
                )
            self._model_cost_by_task[task_id] = (
                self._model_cost_by_task.get(task_id, Decimal("0")) + amount
            )
        for ancestor in ancestors:
            await self._persist_usage(ancestor)

    async def release_model_cost(self, task_id: str, amount: Decimal | None = None) -> None:
        """Release outstanding call reservations when a task is finalized."""
        ancestors = await self._ancestor_ids(task_id)
        for ancestor in ancestors:
            await self._hydrate_usage(ancestor)
        async with self._artifact_lock:
            outstanding = self._model_cost_by_task.get(task_id, Decimal("0"))
            release = outstanding if amount is None else min(outstanding, amount)
            remaining = outstanding - release
            if remaining:
                self._model_cost_by_task[task_id] = remaining
            else:
                self._model_cost_by_task.pop(task_id, None)
            for ancestor in ancestors:
                self._model_cost_reservations[ancestor] = max(
                    Decimal("0"),
                    self._model_cost_reservations.get(ancestor, Decimal("0")) - release,
                )
        for ancestor in ancestors:
            await self._persist_usage(ancestor)

    async def reconcile_model_cost(
        self,
        task_id: str,
        *,
        reserved: Decimal,
        actual: Decimal,
    ) -> None:
        """Replace one completed reservation with its actual owner charge.

        Reservations protect every ancestor while a provider call is in
        flight.  Once that call finishes, the reservation is removed
        immediately and only the owner ledger receives the actual cost.  This
        keeps sequential calls from accumulating phantom liability and keeps
        descendant totals from double-counting.
        """
        if reserved < 0 or actual < 0:
            raise ValueError("model cost values must be non-negative")
        ancestors = await self._ancestor_ids(task_id)
        for ancestor in ancestors:
            await self._hydrate_usage(ancestor)
        async with self._artifact_lock:
            outstanding = self._model_cost_by_task.get(task_id, Decimal("0"))
            if reserved > outstanding:
                raise ValueError(
                    f"model cost reconciliation exceeds outstanding reservation for {task_id}"
                )
            remaining = outstanding - reserved
            if remaining:
                self._model_cost_by_task[task_id] = remaining
            else:
                self._model_cost_by_task.pop(task_id, None)
            for ancestor in ancestors:
                self._model_cost_reservations[ancestor] = max(
                    Decimal("0"),
                    self._model_cost_reservations.get(ancestor, Decimal("0")) - reserved,
                )
            self._ledger.setdefault(task_id, Usage()).cost += actual
        for ancestor in ancestors:
            await self._persist_usage(ancestor)

    @asynccontextmanager
    async def model_call_lease(self, task_id: str):
        """Gate concurrent provider calls with the root task's limit."""
        ancestors = await self._ancestor_ids(task_id)
        root = ancestors[-1] if ancestors else task_id
        limits = [
            (await self.budget_of_async(ancestor)).max_parallel_model_calls
            for ancestor in ancestors
        ]
        limit = max(1, min(limits or [ResourceBudget().max_parallel_model_calls]))
        async with self._artifact_lock:
            semaphore = self._model_semaphores.get(root)
            if semaphore is None:
                import asyncio

                semaphore = asyncio.Semaphore(limit)
                self._model_semaphores[root] = semaphore
                self._model_limits[root] = limit
            elif self._model_limits[root] != limit:
                raise ValueError(
                    f"model concurrency limit changed for root task {root}; restart required"
                )
        await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    @asynccontextmanager
    async def execution_lease(self, task_id: str):
        """Gate sibling executions through the shared root budget."""
        ancestors = await self._ancestor_ids(task_id)
        root = ancestors[-1] if ancestors else task_id
        limits = [
            (await self.budget_of_async(ancestor)).max_parallel_executions for ancestor in ancestors
        ]
        limit = max(1, min(limits or [ResourceBudget().max_parallel_executions]))
        async with self._artifact_lock:
            semaphore = self._execution_semaphores.get(root)
            if semaphore is None:
                import asyncio

                semaphore = asyncio.Semaphore(limit)
                self._execution_semaphores[root] = semaphore
                self._execution_limits[root] = limit
            elif self._execution_limits[root] != limit:
                raise ValueError(
                    f"execution concurrency limit changed for root task {root}; restart required"
                )
        await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    async def _ancestor_ids(self, task_id: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        cur: str | None = task_id
        while cur:
            if cur in seen:
                raise BudgetStateUnavailable(f"cyclic task budget lineage at {cur}")
            seen.add(cur)
            await self._ensure_task_budget_metadata(cur)
            chain.append(cur)
            with self._lock:
                parent = self._parent.get(cur)
            cur = parent
        return chain

    async def _hydrate_usage(self, task_id: str) -> None:
        """Restore the last durable checkpoint after a process restart."""
        with self._lock:
            if task_id in self._usage_hydrated:
                return
        if self._store is None or not hasattr(self._store, "get"):
            return
        try:
            row = await self._store.get(task_id)
        except Exception as exc:  # noqa: BLE001 - authority failure must propagate
            raise BudgetStateUnavailable(
                f"could not read durable usage state for task {task_id}"
            ) from exc
        if not row:
            raise KeyError(f"task not found: {task_id}")
        metadata = row.get("metadata") if isinstance(row, Mapping) else None
        checkpoint = metadata.get("_budget_usage") if isinstance(metadata, Mapping) else None
        if not isinstance(checkpoint, Mapping):
            checkpoint = row.get("usage") if isinstance(row, Mapping) else None
        if not isinstance(checkpoint, Mapping):
            with self._lock:
                self._usage_hydrated.add(task_id)
            return
        try:
            restored = Usage(
                iterations=_int(checkpoint, "iterations"),
                model_calls=_int(checkpoint, "model_calls"),
                input_tokens=_int(checkpoint, "input_tokens"),
                output_tokens=_int(checkpoint, "output_tokens"),
                cost=_dec(checkpoint, "cost", "cost_usd"),
                executions=_int(checkpoint, "executions"),
                mutations=_int(checkpoint, "mutations"),
                children=_int(checkpoint, "children"),
                artifact_bytes=_int(checkpoint, "artifact_bytes"),
                wall_time_s=_float(checkpoint, "wall_time_s"),
            )
            reserved_artifact = _int(checkpoint, "reserved_artifact_bytes")
            reserved_model = _dec(checkpoint, "reserved_model_cost")
            outstanding_model = _dec(checkpoint, "outstanding_model_cost")
            active_started = _parse_datetime(checkpoint.get("active_compute_started_at"))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise BudgetStateUnavailable(
                f"malformed durable usage state for task {task_id}"
            ) from exc
        if active_started is not None:
            # A hard stop may interrupt an active interval between checkpoints.
            # Count it conservatively through recovery instead of resetting the
            # wall-time budget.
            restored.wall_time_s += max(0.0, (utcnow() - active_started).total_seconds())
        with self._lock:
            current = self._ledger.setdefault(task_id, Usage())
            if _all_zero(current):
                self._ledger[task_id] = restored
            if reserved_artifact:
                self._artifact_reservations[task_id] = max(
                    self._artifact_reservations.get(task_id, 0), reserved_artifact
                )
            if reserved_model:
                self._model_cost_reservations[task_id] = max(
                    self._model_cost_reservations.get(task_id, Decimal("0")), reserved_model
                )
            if outstanding_model:
                self._model_cost_by_task[task_id] = max(
                    self._model_cost_by_task.get(task_id, Decimal("0")), outstanding_model
                )
            self._usage_hydrated.add(task_id)

    async def _persist_usage(self, task_id: str) -> None:
        store = self._store
        persist = getattr(store, "persist_budget_usage", None)
        self._checkpoint_active(task_id)
        with self._lock:
            active = self._active_compute.get(task_id)
            active_started = active[1].isoformat() if active is not None else None
        if persist is None:
            return
        current = self.own(task_id)
        await persist(
            task_id,
            {
                "iterations": current.iterations,
                "model_calls": current.model_calls,
                "input_tokens": current.input_tokens,
                "output_tokens": current.output_tokens,
                "cost": str(current.cost),
                "executions": current.executions,
                "mutations": current.mutations,
                "children": current.children,
                "artifact_bytes": current.artifact_bytes,
                "wall_time_s": current.wall_time_s,
                "active_compute_started_at": active_started,
                "reserved_artifact_bytes": self._artifact_reservations.get(task_id, 0),
                "reserved_model_cost": str(
                    self._model_cost_reservations.get(task_id, Decimal("0"))
                ),
                "outstanding_model_cost": str(self._model_cost_by_task.get(task_id, Decimal("0"))),
            },
        )

    def _checkpoint_active(self, task_id: str) -> None:
        """Move the current process-local active interval into the ledger."""
        with self._lock:
            active = self._active_compute.get(task_id)
            if active is None:
                return
            started_monotonic, _started_utc = active
            now_monotonic = time.monotonic()
            entry = self._ledger.setdefault(task_id, Usage())
            entry.wall_time_s += max(0.0, now_monotonic - started_monotonic)
            self._active_compute[task_id] = (now_monotonic, utcnow())


def _deserialize_budget(data: Any, *, strict: bool = False) -> ResourceBudget:
    if isinstance(data, ResourceBudget):
        return data
    if not isinstance(data, Mapping):
        if strict:
            raise ValueError("resource budget must be a mapping")
        return ResourceBudget()
    max_cost = data.get("max_cost_usd")
    max_time = data.get("max_wall_time")
    from decimal import Decimal as _Decimal

    defaults = ResourceBudget()

    def integer(key: str, default: int) -> int:
        value = data.get(key)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            if strict:
                raise ValueError(f"invalid {key}") from exc
            return default

    def optional_integer(key: str) -> int | None:
        value = data.get(key)
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            if strict:
                raise ValueError(f"invalid {key}") from exc
            return None

    if max_cost is not None:
        try:
            max_cost = _Decimal(str(max_cost))
        except (TypeError, ValueError, ArithmeticError) as exc:
            if strict:
                raise ValueError("invalid max_cost_usd") from exc
            max_cost = None
    if max_time is not None and max_time != "":
        try:
            max_time = _timedelta_or_none(max_time)
            if max_time is None and strict:
                raise ValueError("invalid max_wall_time")
        except (TypeError, ValueError, ArithmeticError) as exc:
            if strict:
                raise ValueError("invalid max_wall_time") from exc
            max_time = None
    else:
        max_time = None
    return ResourceBudget(
        max_agent_iterations=integer("max_agent_iterations", defaults.max_agent_iterations),
        max_input_tokens=optional_integer("max_input_tokens"),
        max_output_tokens=optional_integer("max_output_tokens"),
        max_cost_usd=max_cost,
        max_wall_time=max_time,
        max_children=integer("max_children", defaults.max_children),
        max_child_depth=integer("max_child_depth", defaults.max_child_depth),
        max_parallel_model_calls=integer(
            "max_parallel_model_calls", defaults.max_parallel_model_calls
        ),
        max_parallel_executions=integer(
            "max_parallel_executions", defaults.max_parallel_executions
        ),
        max_artifact_bytes=integer("max_artifact_bytes", defaults.max_artifact_bytes),
    )


def _int_or_none(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _int_or_default(val: Any, default: int) -> int:
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _timedelta_or_none(val: Any):
    from datetime import timedelta

    if val is None or val == "":
        return None
    try:
        return timedelta(seconds=float(val))
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=utcnow().tzinfo)
    return parsed


def _cap_remaining(cap: int | None, used: int) -> int | None:
    if cap is None:
        return None
    return max(0, cap - used)


def _cap_remaining_dec(cap: Decimal | None, used: Decimal) -> Decimal | None:
    if cap is None:
        return None
    return max(Decimal("0"), cap - used)


def _all_zero(u: Usage) -> bool:
    return (
        u.iterations == 0
        and u.model_calls == 0
        and u.input_tokens == 0
        and u.output_tokens == 0
        and u.cost == 0
        and u.executions == 0
        and u.mutations == 0
        and u.children == 0
        and u.artifact_bytes == 0
    )
