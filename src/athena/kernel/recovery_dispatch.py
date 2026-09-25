"""Kernel-owned recovery mechanics for dispatched capability failures.

This mechanism records bounded speculative/generated recovery evidence and
validates the next model turn against that evidence. It never constructs an
action, calls a capability directly, or finalizes a task; AgentKernel remains
the sole next-action and termination authority.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock

__all__ = ["RecoveryDispatchMechanism"]


class RecoveryDispatchMechanism:
    """Coordinate recovery evidence and admissibility checks."""

    def __init__(
        self,
        *,
        emit: Callable[..., Awaitable[None]],
        update_metadata: Callable[..., Awaitable[Any]],
        candidate_payload: Callable[[CapabilityCallBlock], tuple[list[Any], str] | None],
        failed_fingerprints: Callable[[list[dict[str, Any]]], set[str]],
    ) -> None:
        self._emit = emit
        self._update_metadata = update_metadata
        self._candidate_payload = candidate_payload
        self._failed_fingerprints = failed_fingerprints

    async def record_speculative_failure(
        self, task: Any, state: Any, result: CapabilityResultBlock
    ) -> bool:
        """Retain a bounded Fusion failure record for the next turn."""
        raw = dict(result.metadata or {}).get("failure_record")
        if not isinstance(raw, Mapping) or not raw.get("failed_operation"):
            return False
        if state.speculative_recovery_limit <= state.speculative_recovery_attempts:
            return True
        record = dict(raw)
        record["capability_id"] = result.capability_id
        record["call_id"] = result.call_id
        state.speculative_failure_records.append(record)
        state.speculative_failure_records = state.speculative_failure_records[-4:]
        state.speculative_recovery_pending = True
        await self._emit(
            "SpeculativeFailureObserved",
            {
                "attempt": state.speculative_recovery_attempts,
                "remaining_attempts": max(
                    state.speculative_recovery_limit - state.speculative_recovery_attempts,
                    0,
                ),
                "failure_record": record,
            },
            task,
        )
        return True

    async def record_generated_failure(
        self,
        task: Any,
        state: Any,
        result: CapabilityResultBlock,
        call: CapabilityCallBlock | None = None,
    ) -> bool:
        """Arm one bounded source-repair turn for typed generated failure."""
        failure = (result.metadata or {}).get("generated_failure")
        if not isinstance(failure, Mapping):
            return False
        failure_class = str(failure.get("failure_class") or "")
        recovery_action = str(failure.get("recovery_action") or "")
        if failure.get("repairable") is not True:
            return False
        if failure_class not in {"implementation_failure", "contract_mismatch"}:
            return False
        if recovery_action != "source_repair":
            return False
        target = str(failure.get("capability_id") or result.capability_id or "")
        if not target:
            return False
        if state.generated_recovery_limit <= state.generated_recovery_attempts:
            return True
        state.generated_recovery_records.append(
            {
                "target_capability_id": target,
                "failure_class": failure_class,
                "recovery_action": recovery_action,
                "evidence": dict(failure.get("evidence") or {}),
                "call_id": result.call_id,
                "task_id": task.id,
                "objective": task.objective,
                "failing_input": dict(call.arguments or {}) if call is not None else {},
                "code_hash": failure.get("code_hash"),
                "original_call": (
                    {
                        "call_id": call.call_id,
                        "capability_id": call.capability_id,
                        "arguments": dict(call.arguments or {}),
                    }
                    if call is not None
                    else None
                ),
            }
        )
        if call is not None:
            state.generated_recovery_original = {
                "call_id": call.call_id,
                "capability_id": call.capability_id,
                "arguments": dict(call.arguments or {}),
            }
        state.generated_recovery_records = state.generated_recovery_records[-4:]
        state.generated_recovery_pending = True
        await self._update_metadata(
            task.id,
            {
                "generated_recovery_pending": True,
                "generated_recovery_attempts": state.generated_recovery_attempts,
                "generated_recovery_limit": state.generated_recovery_limit,
                "generated_recovery": state.generated_recovery_records[-1],
                "generated_recovery_original": state.generated_recovery_original,
                "generated_recovery_retried": state.generated_recovery_retried,
                "generated_recovery_repair_fingerprints": list(
                    getattr(state, "generated_recovery_repair_fingerprints", ()) or ()
                ),
            },
        )
        await self._emit(
            "GeneratedFailureObserved",
            {
                "attempt": state.generated_recovery_attempts,
                "remaining_attempts": max(
                    state.generated_recovery_limit - state.generated_recovery_attempts,
                    0,
                ),
                "target_capability_id": target,
                "failure_class": failure_class,
                "evidence": dict(failure.get("evidence") or {}),
            },
            task,
        )
        return True

    async def validate_generated_recovery(
        self, task: Any, state: Any, calls: list[CapabilityCallBlock]
    ) -> str | None:
        """Require one complete repair for the failed generated capability."""
        if not state.generated_recovery_pending:
            return None
        repair_calls = [
            call
            for call in calls
            if call.capability_id == "synthesis"
            and str((call.arguments or {}).get("operation") or "") == "repair"
        ]
        if not repair_calls:
            return "generated recovery requires a complete repair for the failed capability"
        if state.generated_recovery_attempts >= state.generated_recovery_limit:
            return "generated recovery budget exhausted"
        target = str(state.generated_recovery_records[-1].get("target_capability_id") or "")
        matching = [
            call
            for call in repair_calls
            if str((call.arguments or {}).get("capability_id") or "") == target
            and str((call.arguments or {}).get("code") or "").strip()
        ]
        if not matching:
            return "generated recovery requires a complete repair for the failed capability"
        repair_fingerprint = self._fingerprint(
            {"capability_id": target, "code": str((matching[0].arguments or {}).get("code") or "")}
        )
        attempted = set(getattr(state, "generated_recovery_repair_fingerprints", ()) or ())
        if repair_fingerprint in attempted:
            return "generated recovery proposal repeats the failed repair"
        state.generated_recovery_repair_fingerprints = [
            *sorted(attempted),
            repair_fingerprint,
        ][-4:]
        state.generated_recovery_attempts += 1
        state.generated_recovery_pending = False
        await self._update_metadata(
            task.id,
            {
                "generated_recovery_pending": False,
                "generated_recovery_attempts": state.generated_recovery_attempts,
                "generated_recovery_original": state.generated_recovery_original,
                "generated_recovery_retried": state.generated_recovery_retried,
            },
        )
        return None

    @staticmethod
    def _fingerprint(value: Any) -> str:
        import json

        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    def validate_speculative_recovery(
        self, state: Any, calls: list[CapabilityCallBlock]
    ) -> str | None:
        """Reject an unchanged or unexplained speculative recovery proposal."""
        if not state.speculative_recovery_pending:
            return None
        candidate_calls = [
            payload for call in calls if (payload := self._candidate_payload(call)) is not None
        ]
        if not candidate_calls:
            return "speculative recovery requires a materially different fusion proposal"
        if state.speculative_recovery_attempts >= state.speculative_recovery_limit:
            return "speculative recovery budget exhausted"
        failed = self._failed_fingerprints(state.speculative_failure_records)
        if any(not explanation for _, explanation in candidate_calls):
            state.speculative_recovery_rejections += 1
            return "speculative recovery requires changes_from_previous"
        if not any(
            self._fingerprint(proposal) not in failed
            for proposals, _ in candidate_calls
            for proposal in proposals
        ):
            state.speculative_recovery_rejections += 1
            return "speculative recovery proposal repeats the failed approach"
        state.speculative_recovery_attempts += 1
        state.speculative_recovery_pending = False
        return None
