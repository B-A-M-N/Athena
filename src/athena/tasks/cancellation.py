from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
from collections.abc import Mapping
from typing import Any

from athena.protocol.errors import CancellationUncertain
from athena.protocol.tasks import FINAL_STATUSES, TaskStatus

__all__ = [
    "CancellationManager",
    "CancellationProof",
]

_logger = logging.getLogger("athena.cancellation")


@dataclass
class CancellationProof:
    """Evidence collected while cancelling one task in a tree."""

    task_id: str
    token_signalled: bool = False
    runtime_present: bool = False
    runtime_cancel_requested: bool = False
    runtime_cancel_confirmed: bool = False
    durable_transition_confirmed: bool = False
    error: str | None = None


class CancellationManager:
    """Hierarchical, idempotent cancellation (BUILDSPEC §20, BHV-022/023).

    Each task may have an ``asyncio.Event`` cancellation token checked by the
    kernel at turn boundaries. Cancelling a task sets its own token and
    propagates to every descendant so child work is interrupted (INV-001: work
    still ends through the kernel; this manager only signals and transitions).
    """

    def __init__(
        self,
        *,
        task_manager: Any = None,
        execution_manager: Any = None,
        task_store: Any = None,
    ) -> None:
        self._tasks = task_manager
        self._exec = execution_manager
        self._store = (
            task_store if task_store is not None else getattr(task_manager, "_store", None)
        )
        self._tokens: dict[str, asyncio.Event] = {}
        self._reasons: dict[str, str] = {}
        self._proofs: dict[str, CancellationProof] = {}

    # ------------------------------------------------------------------ #
    def reset(self, task_id: str) -> None:
        self._tokens.pop(task_id, None)
        self._reasons.pop(task_id, None)
        self._proofs.pop(task_id, None)

    def register(self, task_id: str) -> asyncio.Event:
        ev = self._tokens.get(task_id)
        if ev is None:
            ev = asyncio.Event()
            self._tokens[task_id] = ev
        return ev

    def token(self, task_id: str) -> asyncio.Event:
        return self._tokens.get(task_id, asyncio.Event())

    def reason(self, task_id: str) -> str:
        return self._reasons.get(task_id, "")

    def proof(self, task_id: str) -> CancellationProof | None:
        """Return the latest in-process cancellation evidence for a task."""
        return self._proofs.get(task_id)

    def is_cancelled(self, task_id: str) -> bool:
        ev = self._tokens.get(task_id)
        return bool(ev and ev.is_set())

    # ------------------------------------------------------------------ #
    async def cancel(self, task_id: str, reason: str = "cancelled by user") -> TaskStatus:
        return await self._cancel_impl(task_id, reason, root_id=task_id)

    def set_token(self, task_id: str, reason: str = "cancelled") -> None:
        ev = self.register(task_id)
        ev.set()
        self._reasons[task_id] = reason

    async def interrupt(self, task_id: str, reason: str = "externally interrupted") -> TaskStatus:
        """Recoverable interruption (BHV-023); does not set the terminal token.

        The task and every descendant are parked as ``INTERRUPTED`` and MAY be
        resumed later by re-acquiring them (BUILDSPEC §87-89).
        """
        self._reasons[task_id] = reason
        for desc in await self._descendants_of(task_id):
            self.set_token(desc, reason)
            await self._transition_status(desc, TaskStatus.INTERRUPTED)
        await self._transition_status(task_id, TaskStatus.INTERRUPTED)
        return TaskStatus.INTERRUPTED

    async def cancel_tree(self, task_id: str, reason: str = "cancelled") -> None:
        await self.cancel(task_id, reason)

    # ------------------------------------------------------------------ #
    async def _cancel_impl(self, task_id: str, reason: str, *, root_id: str) -> TaskStatus:
        """Cancel a task and recursively every descendant (§20).

        P1-20: execution cancellation runs for EVERY descendant, not only
        the root — a delegated child's task must not be marked cancelled
        while its processes remain alive.
        """
        # Resolve the complete tree before changing any status.  This closes
        # the race where a child remains alive because the root was transitioned
        # first and a later lookup no longer considered it runnable.
        try:
            task_ids = [task_id, *(await self._descendants_of(task_id))]
        except Exception as exc:
            proof = self._proofs.setdefault(task_id, CancellationProof(task_id))
            proof.error = f"cannot establish cancellation tree: {exc}"
            self.set_token(task_id, reason)
            proof.token_signalled = True
            await self._mark_uncertain(proof)
            raise CancellationUncertain(proof.error, cause=exc, task_id=task_id) from exc

        statuses: dict[str, TaskStatus] = {}
        if self._tasks is not None:
            for current in task_ids:
                statuses[current] = _get_status(await self._tasks.get(current))
            if statuses[task_id] in FINAL_STATUSES:
                return statuses[task_id]

        active_ids: list[str] = []
        for current in task_ids:
            if statuses.get(current) in FINAL_STATUSES:
                # Final descendants are evidence only. Their runtimes must not
                # be cancelled a second time during a parent-tree request.
                self._proofs[current] = CancellationProof(
                    current,
                    runtime_cancel_confirmed=True,
                    durable_transition_confirmed=True,
                )
                continue
            self._proofs[current] = CancellationProof(current)
            self.set_token(current, reason)
            self._proofs[current].token_signalled = True
            active_ids.append(current)

        uncertain: list[CancellationProof] = []
        if self._exec is not None:
            for current in active_ids:
                proof = self._proofs[current]
                try:
                    proof.runtime_present = await self._runtime_present(current)
                except Exception as exc:
                    proof.error = f"runtime presence could not be established: {exc}"
                    uncertain.append(proof)
                    continue
                proof.runtime_cancel_requested = True
                try:
                    result = await self._exec.cancel_task(current)
                    proof.runtime_cancel_confirmed = _runtime_cancel_confirmed(result)
                except Exception as exc:
                    proof.error = str(exc)
                    uncertain.append(proof)
                    _logger.warning("runtime cancel failed for task %s: %s", current, exc)
                    continue
                if proof.runtime_present and not proof.runtime_cancel_confirmed:
                    proof.error = "runtime cancellation returned without confirmation"
                    uncertain.append(proof)
        else:
            for current in task_ids:
                proof = self._proofs[current]
                proof.runtime_cancel_confirmed = True

        # A tree is only fully cancelled when every task's owned runtime has
        # either been proven absent or has confirmed cancellation.  Mark
        # uncertain tasks recoverable and refuse to report CANCELLED.
        if uncertain and task_id not in {proof.task_id for proof in uncertain}:
            root_proof = self._proofs[task_id]
            root_proof.error = "descendant cancellation remains uncertain"
            uncertain.insert(0, root_proof)
        uncertain_ids = {proof.task_id for proof in uncertain}
        for current in active_ids:
            proof = self._proofs[current]
            if proof in uncertain:
                await self._mark_uncertain(proof)
                continue
            if current in uncertain_ids:
                continue
            if statuses.get(current) in FINAL_STATUSES:
                proof.durable_transition_confirmed = True
                continue
            await self._transition_status(current, TaskStatus.CANCELLED)
            proof.durable_transition_confirmed = True
        if uncertain:
            details = "; ".join(
                f"{proof.task_id}: {proof.error or 'unconfirmed runtime'}" for proof in uncertain
            )
            raise CancellationUncertain(
                f"cancellation remains uncertain for {len(uncertain)} task(s): {details}",
                task_id=root_id,
                proofs={proof.task_id: proof.__dict__.copy() for proof in uncertain},
            )
        return TaskStatus.CANCELLED

    async def _transition_status(self, task_id: str, status: TaskStatus) -> None:
        if self._tasks is None:
            return
        task = await self._tasks.get(task_id)
        if task is not None and _get_status(task) != status:
            await self._tasks.transition(task_id, status)

    async def _mark_uncertain(self, proof: CancellationProof) -> None:
        """Persist a recoverable state when cancellation cannot be proven."""
        if self._tasks is None:
            return
        task = await self._tasks.get(proof.task_id)
        if task is None:
            proof.error = proof.error or "task disappeared during cancellation"
            raise CancellationUncertain(proof.error, task_id=proof.task_id)
        current = _get_status(task)
        if current in (TaskStatus.INTERRUPTED, TaskStatus.RECOVERY_REQUIRED):
            proof.durable_transition_confirmed = True
            return
        # The explicit CANCELLED case is handled only when it was already
        # durable before this request; never create it here.
        if current is TaskStatus.CANCELLED:
            proof.durable_transition_confirmed = True
            return
        legal = current.legal_transitions()
        target = (
            TaskStatus.RECOVERY_REQUIRED
            if TaskStatus.RECOVERY_REQUIRED in legal
            else TaskStatus.INTERRUPTED
            if TaskStatus.INTERRUPTED in legal
            else None
        )
        if target is None:
            raise CancellationUncertain(
                f"cannot persist uncertain cancellation for {proof.task_id} from {current.value}",
                task_id=proof.task_id,
            )
        await self._tasks.transition(
            proof.task_id, target, reason=proof.error or "cancellation uncertain"
        )
        proof.durable_transition_confirmed = True

    async def _runtime_present(self, task_id: str) -> bool:
        probe = getattr(self._exec, "has_live_runtime", None)
        if callable(probe):
            value = probe(task_id)
            if inspect.isawaitable(value):
                value = await value
            return bool(value)
        sessions = getattr(self._exec, "_task_sessions", None)
        if isinstance(sessions, Mapping) and sessions.get(task_id):
            return True
        executions = getattr(self._exec, "_executions", None)
        if isinstance(executions, Mapping) and task_id in executions.values():
            return True
        # An execution manager without introspection is an authority that may
        # own a runtime. Treat it as present until cancel_task confirms.
        return True

    async def _descendants_of(self, task_id: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        frontier = [task_id]
        while frontier:
            nxt: list[str] = []
            for cur in frontier:
                for child in await self._children_of(cur):
                    if child not in seen:
                        seen.add(child)
                        nxt.append(child)
            out.extend(nxt)
            frontier = nxt
        return out

    async def _children_of(self, task_id: str) -> list[str]:
        if self._store is None:
            return []
        # A failed hierarchy lookup must not be interpreted as “no children”.
        # Doing that would let a database outage mark the parent cancelled
        # while leaving descendant processes alive.  Let the caller fail
        # closed so the cancellation request can be retried and the task is
        # not falsely reported as fully cancelled.
        rows = await self._store.list_children(task_id)
        return [r["id"] for r in rows]


def _runtime_cancel_confirmed(result: Any) -> bool:
    if result is False:
        return False
    if isinstance(result, Mapping) and "confirmed" in result:
        return bool(result["confirmed"])
    confirmed = getattr(result, "confirmed", None)
    return True if confirmed is None else bool(confirmed)


def _get_status(task) -> TaskStatus:
    current = getattr(task, "status", None)
    if isinstance(current, TaskStatus):
        return current
    meta = getattr(task, "metadata", None) or {}
    raw = meta.get("status")
    if raw:
        try:
            return TaskStatus(raw)
        except ValueError:
            pass
    return TaskStatus.CREATED
