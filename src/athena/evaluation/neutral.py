"""A neutral harness for comparing agent implementations.

The harness deliberately knows nothing about Athena or Hermes.  An adapter
receives a fixed case and returns an outcome record; the harness validates the
record, measures only declared dimensions, and emits a comparable report.
It never treats model prose as proof of completion: completion and safety are
explicit fields supplied by the implementation's evidence adapter.
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class EvaluationCase:
    """One fixed task in a cross-system evaluation."""

    id: str
    prompt: str
    required_evidence: tuple[str, ...] = ()
    forbidden_safety_events: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.prompt.strip():
            raise ValueError("evaluation case requires a non-empty id and prompt")
        if len(self.prompt) > 64_000:
            raise ValueError("evaluation prompt exceeds the bounded case limit")


@dataclass(frozen=True)
class EvaluationOutcome:
    """Implementation-neutral result with deterministic quality dimensions."""

    case_id: str
    status: str
    evidence: tuple[str, ...] = ()
    safety_events: tuple[str, ...] = ()
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    capability_calls: int = 0
    synthesis_attempts: int = 0
    delegation_count: int = 0
    approvals: int = 0
    operator_interventions: int = 0
    latency_ms: float = 0.0
    cost_usd: Decimal | None = None
    recovered: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("evaluation outcome requires case_id")
        if self.status not in {"complete", "partial", "failed", "blocked"}:
            raise ValueError(f"unsupported evaluation status: {self.status}")
        for name in (
            "model_calls",
            "tool_calls",
            "prompt_tokens",
            "output_tokens",
            "capability_calls",
            "synthesis_attempts",
            "delegation_count",
            "approvals",
            "operator_interventions",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost_usd must be non-negative")

    @property
    def safety_passed(self) -> bool:
        return not self.safety_events

    def to_record(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "evidence": list(self.evidence),
            "safety_events": list(self.safety_events),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "capability_calls": self.capability_calls,
            "synthesis_attempts": self.synthesis_attempts,
            "delegation_count": self.delegation_count,
            "approvals": self.approvals,
            "operator_interventions": self.operator_interventions,
            "latency_ms": round(self.latency_ms, 3),
            "cost_usd": str(self.cost_usd) if self.cost_usd is not None else None,
            "recovered": self.recovered,
            "metadata": dict(self.metadata),
        }


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("evaluation cost_usd is not numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("evaluation cost_usd must be finite and non-negative")
    return parsed


def outcome_from_record(case: EvaluationCase, value: Mapping[str, Any]) -> EvaluationOutcome:
    """Validate an adapter record and attach the case identity."""
    if str(value.get("case_id") or case.id) != case.id:
        raise ValueError("evaluation adapter returned the wrong case_id")
    return EvaluationOutcome(
        case_id=case.id,
        status=str(value.get("status") or "failed"),
        evidence=tuple(str(item) for item in value.get("evidence") or ()),
        safety_events=tuple(str(item) for item in value.get("safety_events") or ()),
        model_calls=int(value.get("model_calls") or 0),
        tool_calls=int(value.get("tool_calls") or 0),
        prompt_tokens=int(value.get("prompt_tokens") or 0),
        output_tokens=int(value.get("output_tokens") or 0),
        capability_calls=int(value.get("capability_calls") or 0),
        synthesis_attempts=int(value.get("synthesis_attempts") or 0),
        delegation_count=int(value.get("delegation_count") or 0),
        approvals=int(value.get("approvals") or 0),
        operator_interventions=int(value.get("operator_interventions") or 0),
        latency_ms=float(value.get("latency_ms") or 0.0),
        cost_usd=_decimal(value.get("cost_usd")),
        recovered=bool(value.get("recovered")),
        metadata=dict(value.get("metadata") or {}),
    )


def _missing_evidence(case: EvaluationCase, outcome: EvaluationOutcome) -> tuple[str, ...]:
    present = set(outcome.evidence)
    return tuple(item for item in case.required_evidence if item not in present)


def compare_delegation_outcomes(
    case: EvaluationCase,
    *,
    single_agent: EvaluationOutcome,
    delegated: EvaluationOutcome,
) -> dict[str, Any]:
    """Compare delegation against a single-agent run on one fixed case."""
    report = compare_outcomes(case, {"single_agent": single_agent, "delegated": delegated})
    single_complete = bool(report["systems"]["single_agent"]["eligible_as_complete"])
    delegated_complete = bool(report["systems"]["delegated"]["eligible_as_complete"])
    child_evidence = tuple(str(item) for item in (delegated.metadata.get("child_evidence") or ()))
    return {
        **report,
        "comparison": {
            "single_agent_completed": single_complete,
            "delegated_completed": delegated_complete,
            "completion_delta": int(delegated_complete) - int(single_complete),
            "delegation_count": delegated.delegation_count,
            "child_evidence": list(child_evidence),
            "child_evidence_verified": bool(
                delegated_complete and set(case.required_evidence).issubset(set(child_evidence))
            ),
            "materially_contributed": None,
            "conclusion": (
                "delegation_qualified_on_fixed_case"
                if delegated_complete and single_complete and child_evidence
                else "inconclusive"
            ),
        },
    }


def compare_outcomes(
    case: EvaluationCase,
    outcomes: Mapping[str, EvaluationOutcome],
) -> dict[str, Any]:
    """Produce a side-by-side report without inventing a single quality score."""
    systems: dict[str, Any] = {}
    for system, outcome in sorted(outcomes.items()):
        missing = _missing_evidence(case, outcome)
        forbidden = set(case.forbidden_safety_events)
        safety_violation = tuple(event for event in outcome.safety_events if event in forbidden)
        systems[system] = {
            **outcome.to_record(),
            "required_evidence_satisfied": not missing,
            "missing_evidence": list(missing),
            "forbidden_safety_events": list(safety_violation),
            "eligible_as_complete": (
                outcome.status == "complete" and not missing and not safety_violation
            ),
        }
    return {"case": case.id, "systems": systems}


Adapter = Callable[[EvaluationCase], Mapping[str, Any] | EvaluationOutcome | Awaitable[Any]]


class NeutralEvaluationHarness:
    """Run fixed cases through operator-supplied adapters."""

    def __init__(self, *, max_cases: int = 256) -> None:
        if not 1 <= max_cases <= 4096:
            raise ValueError("max_cases must be between 1 and 4096")
        self.max_cases = max_cases

    async def run(
        self,
        cases: Sequence[EvaluationCase],
        adapters: Mapping[str, Adapter],
    ) -> dict[str, Any]:
        if not cases or len(cases) > self.max_cases:
            raise ValueError("evaluation case count is outside the bounded range")
        if not adapters:
            raise ValueError("at least one evaluation adapter is required")
        reports = []
        for case in cases:
            outcomes: dict[str, EvaluationOutcome] = {}
            for name, adapter in sorted(adapters.items()):
                started = time.perf_counter()
                value = adapter(case)
                if inspect.isawaitable(value):
                    value = await value
                elapsed_ms = (time.perf_counter() - started) * 1000
                if isinstance(value, EvaluationOutcome):
                    outcome = value
                    if outcome.case_id != case.id:
                        raise ValueError("evaluation adapter returned the wrong case_id")
                elif isinstance(value, Mapping):
                    outcome = outcome_from_record(case, value)
                else:
                    raise TypeError("evaluation adapter must return a mapping or outcome")
                # Wall-clock timing belongs to the harness. An adapter may
                # report its own measured value in metadata, but cannot forge
                # the primary latency comparison dimension.
                outcome = replace(outcome, latency_ms=elapsed_ms)
                outcomes[name] = outcome
            reports.append(compare_outcomes(case, outcomes))
        return {
            "cases": reports,
            "by_case": {str(report["case"]): report for report in reports},
            "case_count": len(reports),
            "systems": sorted(adapters),
        }

    @staticmethod
    def efficiency_regressions(
        baseline: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        max_latency_ratio: float = 1.25,
        max_cost_ratio: float = 1.25,
        max_model_call_delta: int = 1,
        max_tool_call_delta: int = 2,
        max_prompt_token_delta: int = 512,
        max_output_token_delta: int = 512,
        max_capability_call_delta: int = 2,
        max_synthesis_attempt_delta: int = 1,
        max_delegation_delta: int = 1,
        max_approval_delta: int = 1,
        max_operator_intervention_delta: int = 1,
    ) -> list[str]:
        """Compare declared efficiency dimensions without judging quality."""
        failures: list[str] = []
        candidate_cases = candidate.get("cases") or [
            {"case": case_id, "systems": value.get("systems", {})}
            for case_id, value in (candidate.get("by_case") or {}).items()
            if isinstance(value, Mapping)
        ]
        for case in candidate_cases:
            case_id = str(case.get("case") or "")
            old = (baseline.get("by_case") or {}).get(case_id, {})
            new = (candidate.get("by_case") or {}).get(case_id, {})
            for system, current in (new.get("systems") or {}).items():
                previous = (old.get("systems") or {}).get(system)
                if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
                    continue
                if float(current.get("latency_ms") or 0) > max_latency_ratio * max(
                    1.0, float(previous.get("latency_ms") or 0)
                ):
                    failures.append(f"{case_id}/{system}: latency regression")
                if (
                    int(current.get("model_calls") or 0) - int(previous.get("model_calls") or 0)
                    > max_model_call_delta
                ):
                    failures.append(f"{case_id}/{system}: model-call regression")
                if (
                    int(current.get("tool_calls") or 0) - int(previous.get("tool_calls") or 0)
                    > max_tool_call_delta
                ):
                    failures.append(f"{case_id}/{system}: tool-call regression")
                dimensions = (
                    ("prompt_tokens", max_prompt_token_delta, "prompt-token"),
                    ("output_tokens", max_output_token_delta, "output-token"),
                    ("capability_calls", max_capability_call_delta, "capability-call"),
                    ("synthesis_attempts", max_synthesis_attempt_delta, "synthesis-attempt"),
                    ("delegation_count", max_delegation_delta, "delegation"),
                    ("approvals", max_approval_delta, "approval"),
                    (
                        "operator_interventions",
                        max_operator_intervention_delta,
                        "operator-intervention",
                    ),
                )
                for dimension, maximum_delta, label in dimensions:
                    if (
                        int(current.get(dimension) or 0) - int(previous.get(dimension) or 0)
                        > maximum_delta
                    ):
                        failures.append(f"{case_id}/{system}: {label} regression")
                old_cost = _decimal(previous.get("cost_usd")) or Decimal("0")
                new_cost = _decimal(current.get("cost_usd")) or Decimal("0")
                if new_cost > old_cost * Decimal(str(max_cost_ratio)) and old_cost > 0:
                    failures.append(f"{case_id}/{system}: cost regression")
        return failures


def dumps_report(report: Mapping[str, Any]) -> str:
    """Serialize a report for durable comparison artifacts."""
    return json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "EvaluationCase",
    "EvaluationOutcome",
    "NeutralEvaluationHarness",
    "compare_outcomes",
    "compare_delegation_outcomes",
    "dumps_report",
    "outcome_from_record",
]
