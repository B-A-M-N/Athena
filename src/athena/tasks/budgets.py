from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from threading import Lock
from typing import Any, Mapping

from athena.protocol.messages import utcnow
from athena.protocol.tasks import ResourceBudget, TaskSpec, UsageSummary

__all__ = [
    "BudgetTracker",
    "Usage",
    "DefaultBudget",
    "exceeded_by_budget",
]


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
    if u.wall_time_s > 0:
        return int(u.wall_time_s * 1000)
    return int((utcnow() - u.started).total_seconds() * 1000)


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
    if budget.max_children and u.children >= budget.max_children:
        return True
    if budget.max_wall_time is not None and _elapsed_s(u) >= budget.max_wall_time.total_seconds():
        return True
    return False


def _elapsed_s(u: Usage) -> float:
    return u.wall_time_s if u.wall_time_s > 0 else (utcnow() - u.started).total_seconds()


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
            self.consume(
                task_id,
                dict(input_tokens=summary.input_tokens,
                     output_tokens=summary.output_tokens,
                     model_calls=summary.model_calls,
                     cost=summary.cost_usd,
                     executions=summary.executions,
                     mutations=summary.mutations),
            )

    def record_child(self, parent_id: str) -> None:
        with self._lock:
            self._ledger.setdefault(parent_id, Usage())
            self._ledger[parent_id].children += 1

    def current(self, task_id: str) -> UsageSummary:
        with self._lock:
            return self._ledger.get(task_id, Usage()).as_usage_summary()

    def own(self, task_id: str) -> Usage:
        with self._lock:
            entry = self._ledger.get(task_id)
            if entry is None:
                return Usage()
            return Usage(
                iterations=entry.iterations, model_calls=entry.model_calls,
                input_tokens=entry.input_tokens, output_tokens=entry.output_tokens,
                cost=entry.cost, executions=entry.executions, mutations=entry.mutations,
                children=entry.children, artifact_bytes=entry.artifact_bytes,
                started=entry.started, wall_time_s=entry.wall_time_s,
            )

    # ------------------------------------------------------------------ #
    # Budget resolution
    # ------------------------------------------------------------------ #
    async def budget_of_async(self, task_id: str) -> ResourceBudget:
        with self._lock:
            if task_id in self._budgets:
                return self._budgets[task_id]
        spec = self._resolve_spec(task_id)
        if spec is not None:
            with self._lock:
                self._budgets[task_id] = spec.resource_budget or ResourceBudget()
                self._parent[task_id] = spec.parent_task_id
            return self._budgets[task_id]
        if await self._load_from_store_async(task_id):
            with self._lock:
                return self._budgets[task_id]
        return ResourceBudget()

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
        return ResourceBudget()

    def _resolve_spec(self, task_id: str):
        if self._budget_resolver is not None:
            return self._budget_resolver(task_id)
        return None

    async def _load_from_store_async(self, task_id: str) -> bool:
        if self._store is None:
            return False
        try:
            row = await self._store.get(task_id)
        except Exception:
            return False
        if not row:
            return False
        rb = _deserialize_budget(row.get("resource_budget")) if row.get("resource_budget") else ResourceBudget()
        with self._lock:
            self._budgets[task_id] = rb
            self._parent[task_id] = row.get("parent_task_id")
        return True

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
        own = self.own(task_id)
        agg = Usage()
        agg.add(own)
        children = await self.descendants(task_id)
        for child in children:
            agg.add(self.own(child))
        agg.children = len(children)
        return agg

    async def remaining(self, task_id: str) -> Mapping[str, Any]:
        budget = await self.budget_of_async(task_id)
        total = await self.total(task_id)
        cost_remaining = _cap_remaining_dec(
            budget.max_cost_usd if budget.max_cost_usd is not None else None,
            total.cost)
        return {
            "iterations": _cap_remaining(budget.max_agent_iterations, total.iterations),
            "input_tokens": _cap_remaining(budget.max_input_tokens, total.input_tokens),
            "output_tokens": _cap_remaining(budget.max_output_tokens, total.output_tokens),
            "cost_usd": cost_remaining,
            "children": _cap_remaining(budget.max_children, total.children),
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

    async def _ancestor_ids(self, task_id: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        cur = task_id
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            with self._lock:
                parent = self._parent.get(cur)
            if parent:
                cur = parent
                continue
            with self._lock:
                budgets_have = cur in self._budgets
            if budgets_have:
                break
            spec = self._resolve_spec(cur)
            if spec is None:
                break
            with self._lock:
                self._parent[cur] = spec.parent_task_id
                self._budgets[cur] = spec.resource_budget or ResourceBudget()
            cur = spec.parent_task_id
        return chain


def _deserialize_budget(data: Any) -> ResourceBudget:
    if isinstance(data, ResourceBudget):
        return data
    if not isinstance(data, Mapping):
        return ResourceBudget()
    max_cost = data.get("max_cost_usd")
    max_time = data.get("max_wall_time")
    from decimal import Decimal as _Decimal

    return ResourceBudget(
        max_agent_iterations=int(data.get("max_agent_iterations") or 0),
        max_input_tokens=_int_or_none(data.get("max_input_tokens")),
        max_output_tokens=_int_or_none(data.get("max_output_tokens")),
        max_cost_usd=_Decimal(str(max_cost)) if max_cost is not None else None,
        max_wall_time=_timedelta_or_none(max_time),
        max_children=int(data.get("max_children") or 0),
        max_child_depth=int(data.get("max_child_depth") or 0),
        max_parallel_model_calls=int(data.get("max_parallel_model_calls") or 0),
        max_parallel_executions=int(data.get("max_parallel_executions") or 0),
        max_artifact_bytes=int(data.get("max_artifact_bytes") or 0),
    )


def _int_or_none(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _timedelta_or_none(val: Any):
    from datetime import timedelta

    if val is None or val == "":
        return None
    try:
        return timedelta(seconds=float(val))
    except (TypeError, ValueError):
        return None


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
        u.iterations == 0 and u.model_calls == 0 and u.input_tokens == 0
        and u.output_tokens == 0 and u.cost == 0 and u.executions == 0
        and u.mutations == 0 and u.children == 0 and u.artifact_bytes == 0
    )