"""Sandbox validation mechanism (P1-10 extraction).

Sandbox case generation and execution, effect/invariant/resource
expectation checking, and validation progress reporting, moved
verbatim from :mod:`athena.synthesis.engine`. Subordinate to
SynthesisEngine: this module holds no authority of its own — admission
and promotion decisions stay on the engine.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from athena.affordances.validation import ValidationTier
from athena.synthesis.validation_phases import (
    DependencyValidationPhase,
    EvidenceValidationPhase,
    SandboxValidationPhase,
    StaticValidationPhase,
    ValidationContext,
)

if TYPE_CHECKING:
    from athena.protocol.tasks import WorkspaceSpec
    from athena.synthesis.models import SyntheticCapability as SyntheticCapabilityT
    from athena.synthesis.engine import SynthesisEngine


class Validator:
    """Sandbox validation machinery for generated capabilities.

    Verbatim extraction from ``SynthesisEngine``: engine-owned state
    and helpers resolve through ``self._e`` at call time.
    """

    def __init__(self, engine: SynthesisEngine) -> None:
        self._e = engine

    def _historical_cases(self, cap: SyntheticCapabilityT) -> tuple[list[dict], dict[str, dict]]:
        """Load and normalize inherited regression evidence exactly once."""
        historical_failures: list[dict] = []
        historical_ids: set[str] = set()
        for key in ("live_failure_cases", "regression_cases"):
            for raw_case in cap.validation.get(key) or ():
                from athena.synthesis.models import RegressionCase

                case = RegressionCase.from_record(
                    dict(raw_case),
                    capability_family=cap.family_id,
                    revision=cap.revision,
                ).to_record()
                if not case["id"]:
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            case, sort_keys=True, separators=(",", ":"), default=str
                        ).encode()
                    ).hexdigest()
                    case["id"] = f"regression:{fingerprint[:24]}"
                identity = str(case.get("id") or json.dumps(case, sort_keys=True, default=str))
                if identity in historical_ids:
                    continue
                historical_ids.add(identity)
                historical_failures.append(case)
        return historical_failures, {
            str(case["id"]): case for case in historical_failures if case.get("id")
        }

    async def validate(
        self,
        cap: SyntheticCapabilityT,
        cases: list[dict],
        *,
        timeout: float = 15.0,
        tier: ValidationTier | str = ValidationTier.TASK,
        workspace_root: str | None = None,
        workspace: WorkspaceSpec | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        profile: str | None = None,
        task_policy=None,
        task_budget=None,
        generated_call_depth: int = 0,
        generated_call_chain: tuple[str, ...] = (),
    ) -> SyntheticCapabilityT:
        """Coordinate explicit static, dependency, sandbox, and evidence phases."""
        cap.validation_cases = [dict(case) for case in cases or []]
        historical_failures, historical_by_id = self._historical_cases(cap)
        context = ValidationContext(
            engine=self._e,
            cap=cap,
            cases=tuple(cap.validation_cases),
            tier=tier if isinstance(tier, ValidationTier) else ValidationTier(tier),
            timeout=timeout,
            workspace_root=workspace_root,
            workspace=workspace,
            task_id=task_id,
            session_id=session_id,
            profile=profile,
            task_policy=task_policy,
            task_budget=task_budget,
            generated_call_depth=generated_call_depth,
            generated_call_chain=generated_call_chain,
            historical_failures=tuple(historical_failures),
            historical_by_id=historical_by_id,
        )
        phase_result = StaticValidationPhase(context).run()
        if not isinstance(phase_result, ValidationContext):
            return phase_result
        context = phase_result
        phase_result = await DependencyValidationPhase(context).run()
        if not isinstance(phase_result, ValidationContext):
            return phase_result
        context = phase_result
        sandbox = await SandboxValidationPhase(context).run()
        if not isinstance(sandbox, tuple):
            return sandbox
        context, result = sandbox
        return await EvidenceValidationPhase(context, result).run()

    async def _emit_validation_progress(
        self,
        *,
        task_id: str | None,
        capability_id: str,
        completed: int,
        total: int,
    ) -> None:
        """Publish fixture progress using the real validation denominator."""
        if self._e._dispatcher is None or total <= 0:
            return
        emit = getattr(self._e._dispatcher, "emit_progress", None)
        if emit is None:
            return
        try:
            await emit(
                task_id=task_id,
                call_id=f"{capability_id}:validation",
                capability_id=capability_id,
                value=completed,
                total=total,
                unit="fixtures",
                message=(f"generated validation fixture {completed}/{total} complete"),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            # Progress is observational; it cannot change validation truth.
            return
