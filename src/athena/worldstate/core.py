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

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from athena.protocol.ids import new_id

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
    evidence: dict          # execution/capability proof
    depends_on_paths: tuple[str, ...] = ()   # files this claim covers
    invalidated_by: list[dict] = field(default_factory=list)


class ClaimRegistry:
    """Claims bound to evidence, invalidated by later mutations."""

    def __init__(self) -> None:
        self._claims: dict[str, Claim] = {}

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
        return claim

    def get(self, claim_id: str) -> Claim | None:
        return self._claims.get(claim_id)

    def for_task(self, task_id: str | None) -> list[Claim]:
        return [c for c in self._claims.values() if c.task_id == task_id]

    def invalidate_for_paths(self, changed_paths: list[str]) -> list[Claim]:
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
                claim.invalidated_by.append({"paths": sorted(changed)})
                flipped.append(claim)
                _logger.info("claim %s STALE (%s)", claim.id, claim.text[:60])
        return flipped

    def contradict(self, claim_id: str, because: str) -> Claim | None:
        claim = self._claims.get(claim_id)
        if claim is None:
            return None
        claim.status = ClaimStatus.CONTRADICTED
        claim.invalidated_by.append({"because": because})
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
    probe: Callable[..., Any]      # async callable -> bool
    required: bool = True


class InvariantSet:
    """Runtime safety envelope: probes run after each mutation batch."""

    def __init__(self, *, task_id: str | None = None) -> None:
        self.task_id = task_id
        self.invariants: dict[str, Invariant] = {}
        self.violations: list[dict] = []

    def add(self, description: str, probe: Callable[..., Any],
            *, invariant_id: str | None = None, required: bool = True) -> str:
        iid = invariant_id or new_id("inv")
        self.invariants[iid] = Invariant(
            id=iid, description=description, probe=probe, required=required)
        return iid

    async def check_all(self) -> dict:
        """Run every probe; return a summary and record violations."""
        results: list[dict] = []
        ok_all = True
        for inv in list(self.invariants.values()):
            try:
                passed = bool(await inv.probe())
            except Exception as exc:
                passed = False
                results.append({
                    "invariant": inv.id, "description": inv.description,
                    "passed": False, "error": str(exc),
                    "required": inv.required,
                })
                if inv.required:
                    ok_all = False
                continue
            results.append({
                "invariant": inv.id, "description": inv.description,
                "passed": passed, "required": inv.required,
            })
            if not passed:
                if inv.required:
                    ok_all = False
                self.violations.append({
                    "invariant": inv.id, "description": inv.description,
                })
        return {"ok": ok_all, "results": results,
                "violations": list(self.violations)}


# ---------------------------------------------------------------------------
# Structured task world state
# ---------------------------------------------------------------------------

class TaskWorldState:
    """Execution-grounded view of one task's machine reality."""

    def __init__(self, *, service=None, task_id: str | None = None) -> None:
        self.service = service
        self.task_id = task_id
        self.claims = ClaimRegistry()

    async def snapshot(self, *, workspace_root: str | None = None) -> dict:
        state: dict[str, Any] = {"task_id": self.task_id}

        # Runtime sessions alive?
        sessions = []
        if self.service is not None and hasattr(self.service, "_store_runtime_sessions"):
            store = getattr(self.service, "_store_runtime_sessions", None)
            if store is not None and self.task_id:
                try:
                    rows = await store.list_active(self.task_id)
                    sessions = [
                        {"id": r.get("id"), "runtime": r.get("runtime"),
                         "alive": bool(r.get("alive"))} for r in rows or []]
                except Exception as exc:
                    _logger.warning("world-state session query failed: %s", exc)
        state["runtime_sessions"] = sessions

        # Mutation pressure since last verification.
        if self.service is not None and getattr(self.service, "_store_mutations", None) is not None \
                and self.task_id:
            try:
                rows = await self.service._store_mutations.list_for_task(self.task_id)
                recent = rows[-10:]
                state["recent_mutations"] = [
                    {"resource": r.get("resource"), "operation": r.get("operation"),
                     "status": r.get("status")} for r in recent]
                # P1-37: c is a Claim dataclass — attribute access, not indexing.
                # (The old c["status"] raised TypeError, was silently swallowed,
                #  and the snapshot lost mutation-state information.)
                verified_claims = [
                    c for c in self.claims.for_task(self.task_id)
                    if c.status == ClaimStatus.VERIFIED]
                state["mutations_since_verified_claim"] = (
                    len(rows) if not verified_claims else
                    sum(1 for r in rows if r.get("status") == "COMPLETED"))
            except Exception as exc:
                _logger.warning("world-state mutation query failed: %s", exc)

        # Dirty files (workspace vs HEAD when git present) — best effort.
        if workspace_root and os.path.isdir(workspace_root):
            state["dirty_files"] = await self._dirty_files(workspace_root)

        # Claims with statuses.
        claim_dicts = [
            {"id": c.id, "text": c.text, "status": c.status,
             "evidence": c.evidence}
            for c in self.claims.for_task(self.task_id)
        ]
        state["claims"] = claim_dicts
        contradicted = [c for c in claim_dicts
                        if c["status"] == ClaimStatus.CONTRADICTED]
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
                    capture_output=True, text=True, timeout=5)
                if proc.returncode == 0:
                    return [ln[3:].strip() for ln in proc.stdout.splitlines()
                            if ln.strip()]
            except Exception:
                pass
            return []

        import asyncio

        return await asyncio.get_running_loop().run_in_executor(None, _git)


import os  # noqa: E402  (used by snapshot; kept at bottom to avoid shadowing)
