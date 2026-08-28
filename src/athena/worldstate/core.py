"""World state: claims, evidence, invariants, and structured task reality.

Three cooperating pieces of the fusion thesis:

1. ClaimRegistry  — every assertion ("all tests pass") is bound to the
   evidence that proved it (execution id, command, exit code, revision).
   When the workspace changes afterward, affected claims become STALE:
   not false, but no longer trustworthy without reverification.

2. InvariantSet   — continuous invariants checked AFTER each mutation,
   giving autonomous work a runtime safety envelope instead of
   end-of-task verification alone.

3. TaskWorldState — a structured, execution-grounded snapshot of what is
   actually true for a task right now: dirty files, mutation counts,
   session liveness, verified/contradicted claims. The model perceives
   maintained machine state instead of re-reading noisy logs.

All three read and write canonical durable stores; none execute anything.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from athena.protocol.ids import new_id

if TYPE_CHECKING:  # pragma: no cover
    from athena.worldstate.store import WorldStateStore

__all__ = ["ClaimRegistry", "InvariantSet", "TaskWorldState"]

_logger = logging.getLogger("athena.worldstate")


class ClaimStatus:
    VERIFIED = "VERIFIED"
    STALE = "STALE"
    CONTRADICTED = "CONTRADICTED"


@dataclass
class Claim:
    id: str
    task_id: str | None
    text: str
    status: str
    evidence: dict  # execution/capability proof
    depends_on_paths: tuple[str, ...] = ()  # files this claim covers
    invalidated_by: list[dict] = field(default_factory=list)


class ClaimRegistry:
    """Claims bound to evidence, invalidated by later mutations.

    With ``store=None`` (default) the registry is purely in-memory exactly as
    before. With a ``WorldStateStore``, every record/flip is also persisted
    (best-effort background writes; call :meth:`flush` to await them).
    """

    def __init__(self, *, store: WorldStateStore | None = None) -> None:
        self._claims: dict[str, Claim] = {}
        self._store = store
        self._pending: set[asyncio.Task] = set()

    # -- persistence helpers -------------------------------------------------

    def _persist(self, coro) -> None:
        """Schedule a durable write without blocking the sync API."""
        if self._store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop: in-memory only (matches legacy behavior)
        task = loop.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        """Await any outstanding persistence writes."""
        if self._pending:
            await asyncio.gather(*list(self._pending))

    def record(
        self,
        *,
        text: str,
        evidence: dict,
        task_id: str | None = None,
        depends_on_paths: tuple[str, ...] = (),
    ) -> Claim:
        claim = Claim(
            id=new_id("claim"),
            task_id=task_id,
            text=text,
            status=ClaimStatus.VERIFIED,
            evidence=dict(evidence),
            depends_on_paths=tuple(depends_on_paths or ()),
        )
        self._claims[claim.id] = claim
        if self._store is not None:
            self._persist(
                self._store.save_claim(
                    {
                        "id": claim.id,
                        "task_id": claim.task_id,
                        "text": claim.text,
                        "status": claim.status,
                        "evidence": claim.evidence,
                        "depends_on_paths": list(claim.depends_on_paths),
                    }
                )
            )
        return claim

    def get(self, claim_id: str) -> Claim | None:
        return self._claims.get(claim_id)

    def for_task(self, task_id: str | None) -> list[Claim]:
        return [c for c in self._claims.values() if c.task_id == task_id]

    async def load_from_store(self, task_id: str | None = None) -> int:
        """Hydrate in-memory claims from the durable store (restart path)."""
        from athena.worldstate.store import WorldStateStore

        if not isinstance(self._store, WorldStateStore):
            return 0
        await self.flush()
        records = await self._store.claims_for_task(task_id)
        for rec in records:
            claim = Claim(
                id=rec["id"],
                task_id=rec.get("task_id"),
                text=rec.get("text") or "",
                status=rec.get("status") or ClaimStatus.VERIFIED,
                evidence=rec.get("evidence") or {},
                depends_on_paths=tuple(rec.get("depends_on_paths") or ()),
                invalidated_by=list(rec.get("invalidated_by") or []),
            )
            self._claims[claim.id] = claim
        return len(records)

    def invalidate_for_paths(
        self,
        changed_paths: list[str],
        *,
        mutation_id: str | None = None,
        mutation_sequence: int | None = None,
        mutation_event_sequence: int | None = None,
    ) -> list[Claim]:
        """Mark claims STALE when a path they depend on has been mutated.

        Returns the claims whose status changed so callers can surface it.
        """
        changed = set(changed_paths)
        flipped: list[Claim] = []
        for claim in self._claims.values():
            if claim.status != ClaimStatus.VERIFIED:
                continue
            if any(_paths_overlap(claim.depends_on_paths, p) for p in changed):
                claim.status = ClaimStatus.STALE
                reason: dict[str, Any] = {"paths": sorted(changed)}
                if mutation_id is not None:
                    reason["mutation_id"] = mutation_id
                if mutation_sequence is not None:
                    reason["mutation_sequence"] = mutation_sequence
                if mutation_event_sequence is not None:
                    reason["mutation_event_sequence"] = mutation_event_sequence
                claim.invalidated_by.append(reason)
                flipped.append(claim)
                if self._store is not None and claim.task_id is not None:
                    self._persist(
                        self._store.invalidate_for_paths(
                            claim.task_id,
                            sorted(changed),
                            mutation_id=mutation_id,
                            mutation_sequence=mutation_sequence,
                            mutation_event_sequence=mutation_event_sequence,
                        )
                    )
                _logger.info("claim %s STALE (%s)", claim.id, claim.text[:60])
        return flipped

    def contradict(self, claim_id: str, because: str) -> Claim | None:
        claim = self._claims.get(claim_id)
        if claim is None:
            return None
        claim.status = ClaimStatus.CONTRADICTED
        claim.invalidated_by.append({"because": because})
        if self._store is not None:
            self._persist(self._store.mark_contradicted(claim.id, because))
        return claim


def _paths_overlap(patterns: tuple[str, ...], path: str) -> bool:
    if not patterns:
        return True  # a claim with no scoping depends on the whole workspace
    for pat in patterns:
        if not pat or pat == "*":
            return True
        if path == pat or path.startswith(pat.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Continuous invariants
# ---------------------------------------------------------------------------


@dataclass
class Invariant:
    id: str
    description: str
    probe: Callable[..., Any] | None  # async callable -> bool
    required: bool = True


class InvariantSet:
    """Runtime safety envelope: probes run after each mutation batch."""

    def __init__(self, *, task_id: str | None = None, store: WorldStateStore | None = None) -> None:
        self.task_id = task_id
        self._store = store
        self.invariants: dict[str, Invariant] = {}
        self.violations: list[dict] = []
        self._pending: set[asyncio.Task] = set()

    def add(
        self,
        description: str,
        probe: Callable[..., Any] | None = None,
        *,
        invariant_id: str | None = None,
        required: bool = True,
        definition: dict[str, Any] | None = None,
    ) -> str:
        iid = invariant_id or new_id("inv")
        self.invariants[iid] = Invariant(
            id=iid, description=description, probe=probe, required=required
        )
        if self._store is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(
                    self._store.save_invariant(
                        {
                            "id": iid,
                            "task_id": self.task_id,
                            "description": description,
                            "definition": definition or {"type": "callable"},
                            "required": required,
                        }
                    )
                )
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        return iid

    async def flush(self) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending))

    async def check_all(self) -> dict:
        """Run every probe; return a summary and record violations."""
        results: list[dict] = []
        ok_all = True
        for inv in list(self.invariants.values()):
            if inv.probe is None:
                passed = False
                detail = {
                    "invariant": inv.id,
                    "description": inv.description,
                    "passed": False,
                    "error": "invariant has no executable probe",
                    "required": inv.required,
                }
                results.append(detail)
                if inv.required:
                    ok_all = False
                    self.violations.append(
                        {
                            "invariant": inv.id,
                            "description": inv.description,
                        }
                    )
                continue
            try:
                passed = bool(await inv.probe())
            except Exception as exc:  # noqa: BLE001 - invariant probes are user-defined
                passed = False
                _logger.warning("invariant %s probe failed: %s", inv.id, exc)
                results.append(
                    {
                        "invariant": inv.id,
                        "description": inv.description,
                        "passed": False,
                        "error": str(exc),
                        "required": inv.required,
                    }
                )
                if inv.required:
                    ok_all = False
                continue
            results.append(
                {
                    "invariant": inv.id,
                    "description": inv.description,
                    "passed": passed,
                    "required": inv.required,
                }
            )
            if not passed:
                if inv.required:
                    ok_all = False
                self.violations.append(
                    {
                        "invariant": inv.id,
                        "description": inv.description,
                    }
                )
        if self._store is not None and self.task_id is not None:
            for result in results:
                await self._store.record_invariant_result(
                    {
                        "id": new_id("inv-result"),
                        "invariant_id": result["invariant"],
                        "task_id": self.task_id,
                        "passed": result["passed"],
                        "error": result.get("error"),
                        "details": result,
                    }
                )
            await self.flush()
        return {"ok": ok_all, "results": results, "violations": list(self.violations)}


# ---------------------------------------------------------------------------
# Structured task world state
# ---------------------------------------------------------------------------


class TaskWorldState:
    """Execution-grounded view of one task's machine reality."""

    def __init__(self, *, service=None, task_id: str | None = None) -> None:
        self.service = service
        self.task_id = task_id
        self.claims = ClaimRegistry(store=getattr(service, "_world_state_store", None))
        self._claims_loaded = False

    async def snapshot(self, *, workspace_root: str | None = None) -> dict:
        state: dict[str, Any] = {"task_id": self.task_id}

        if not self._claims_loaded:
            await self.claims.load_from_store(self.task_id)
            self._claims_loaded = True

        # Runtime sessions alive?
        sessions = []
        if self.service is not None and hasattr(self.service, "_store_runtime_sessions"):
            store = getattr(self.service, "_store_runtime_sessions", None)
            if store is not None and self.task_id:
                try:
                    rows = await store.list_active(self.task_id)
                    sessions = [
                        {
                            "id": r.get("id"),
                            "runtime": r.get("runtime"),
                            "alive": bool(r.get("alive")),
                        }
                        for r in rows or []
                    ]
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    _logger.warning("world-state session query failed: %s", exc)
        state["runtime_sessions"] = sessions

        # A scheduler-triggered maintenance task receives the originating
        # WatchObserved envelope on TaskCreated/TaskStarted. Keep that bounded
        # observation in world state so verification can explain its cause
        # without reopening the global event stream or trusting prose.
        state["observations"] = []
        if self.service is not None and self.task_id:
            events = getattr(self.service, "_store_events", None)
            if events is not None:
                try:
                    for event in await events.list_for_task(self.task_id):
                        trigger = (event.payload or {}).get("trigger_event")
                        if isinstance(trigger, dict):
                            state["observations"].append(
                                {
                                    "event_id": trigger.get("id"),
                                    "type": trigger.get("type"),
                                    "payload": dict(trigger.get("payload") or {}),
                                    "received_at": event.timestamp.isoformat(),
                                }
                            )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    _logger.warning("world-state observation query failed: %s", exc)

        if (
            self.service is not None
            and getattr(self.service, "_world_state_store", None) is not None
            and self.task_id
        ):
            try:
                state["invariants"] = await self.service._world_state_store.invariants_for_task(
                    self.task_id
                )
                state[
                    "invariant_results"
                ] = await self.service._world_state_store.invariant_results_for_task(self.task_id)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("world-state invariant query failed: %s", exc)

        # Mutation pressure since last verification.
        if (
            self.service is not None
            and getattr(self.service, "_store_mutations", None) is not None
            and self.task_id
        ):
            try:
                rows = await self.service._store_mutations.list_for_task(self.task_id)
                recent = rows[-10:]
                state["recent_mutations"] = [
                    {
                        "resource": r.get("resource"),
                        "operation": r.get("operation"),
                        "status": r.get("status"),
                    }
                    for r in recent
                ]
                verified_claims = [
                    c
                    for c in self.claims.for_task(self.task_id)
                    if c.status == ClaimStatus.VERIFIED
                ]
                # Claims pin the event sequence at which their evidence was
                # established. Counting every mutation after *any* verified
                # claim makes a fresh claim look stale immediately and was
                # especially misleading when several claims existed. Use the
                # latest durable verification boundary; legacy claims without
                # a boundary conservatively report all completed mutations.
                boundaries = [
                    int(c.evidence["mutation_sequence"])
                    for c in verified_claims
                    if isinstance(c.evidence, dict)
                    and str(c.evidence.get("mutation_sequence", "")).isdigit()
                ]
                completed = [r for r in rows if r.get("status") == "COMPLETED"]
                baseline = max(boundaries, default=0)
                state["mutations_since_verified_claim"] = sum(
                    1
                    for mutation in completed
                    if mutation.get("sequence") is None or int(mutation["sequence"]) > baseline
                )
                state["last_mutation_sequence"] = max(
                    (
                        int(mutation["sequence"])
                        for mutation in completed
                        if mutation.get("sequence") is not None
                    ),
                    default=0,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("world-state mutation query failed: %s", exc)

        # Dirty files (workspace vs HEAD when git present) — best effort.
        if workspace_root and os.path.isdir(workspace_root):
            state["dirty_files"] = await self._dirty_files(workspace_root)

        # Claims with statuses.
        claim_dicts = [
            {"id": c.id, "text": c.text, "status": c.status, "evidence": c.evidence}
            for c in self.claims.for_task(self.task_id)
        ]
        state["claims"] = claim_dicts
        contradicted = [c for c in claim_dicts if c["status"] == ClaimStatus.CONTRADICTED]
        stale = [c for c in claim_dicts if c["status"] == ClaimStatus.STALE]
        state["unknown"] = [c["text"] for c in stale]
        state["contradictions"] = contradicted
        return state

    @staticmethod
    async def _dirty_files(root: str) -> list[str]:
        import subprocess

        def _git():
            try:
                proc = subprocess.run(
                    ["git", "-C", root, "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if proc.returncode == 0:
                    return [ln[3:].strip() for ln in proc.stdout.splitlines() if ln.strip()]
            except (OSError, subprocess.SubprocessError):
                return []
            return []

        import asyncio

        return await asyncio.get_running_loop().run_in_executor(None, _git)
