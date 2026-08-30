"""Small, non-mutating Hermes governor/referee boundary.

Hermes is deliberately an adapter seam, not another Athena worker.  An
operator supplies an external evaluator that receives a :class:`ReviewPacket`;
this module validates its bounded response and intersects it with deterministic
proof constraints.  No method here can write, commit, discard, or promote.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HermesDecision(StrEnum):
    PASS = "PASS"
    HOLD = "HOLD"
    REJECT = "REJECT"
    CHALLENGE = "CHALLENGE"
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    MISSION_COMPLETE_SUPPORTED = "MISSION_COMPLETE_SUPPORTED"


@dataclass(frozen=True)
class ReviewPacket:
    """Canonical read-only evidence sent to an external Hermes evaluator."""

    kind: str
    mission: Mapping[str, Any] = field(default_factory=dict)
    work_item: Mapping[str, Any] = field(default_factory=dict)
    risk: Mapping[str, Any] = field(default_factory=dict)
    base_identity: Mapping[str, Any] = field(default_factory=dict)
    candidate_identity: Mapping[str, Any] = field(default_factory=dict)
    diff: str = ""
    frozen_contract_context: str = ""
    verification_results: tuple[Mapping[str, Any], ...] = ()
    release_results: Mapping[str, Any] = field(default_factory=dict)
    producer_models: tuple[str, ...] = ()
    reviewer_history: tuple[Mapping[str, Any], ...] = ()
    resource_usage: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """Return bounded JSON-shaped evidence for transport and persistence."""
        return {
            "kind": self.kind,
            "mission": dict(self.mission),
            "work_item": dict(self.work_item),
            "risk": dict(self.risk),
            "base_identity": dict(self.base_identity),
            "candidate_identity": dict(self.candidate_identity),
            "diff": self.diff,
            "frozen_contract_context": self.frozen_contract_context,
            "verification_results": [dict(item) for item in self.verification_results],
            "release_results": dict(self.release_results),
            "producer_models": list(self.producer_models),
            "reviewer_history": [dict(item) for item in self.reviewer_history],
            "resource_usage": dict(self.resource_usage),
        }

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_record(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class HermesVerdict:
    """A bounded Hermes recommendation; never an authorization token."""

    decision: HermesDecision
    rationale: str = ""
    blockers: tuple[str, ...] = ()
    challenges: tuple[Mapping[str, Any], ...] = ()
    risks: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    packet_hash: str = ""
    requires_human_review: bool = False

    def to_record(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "rationale": self.rationale,
            "blockers": list(self.blockers),
            "challenges": [dict(item) for item in self.challenges],
            "risks": list(self.risks),
            "missing_evidence": list(self.missing_evidence),
            "packet_hash": self.packet_hash,
            "requires_human_review": self.requires_human_review,
        }


HermesEvaluator = Callable[
    [ReviewPacket], Awaitable[Mapping[str, Any] | HermesVerdict] | Mapping[str, Any] | HermesVerdict
]


class HermesReferee:
    """Validate an external Hermes response and only subtract authority."""

    def __init__(self, evaluator: HermesEvaluator | None = None) -> None:
        self._evaluator = evaluator

    async def review(self, packet: ReviewPacket) -> HermesVerdict:
        packet_hash = packet.digest()
        deterministic_error = _deterministic_failure(packet)
        if deterministic_error:
            return HermesVerdict(
                decision=HermesDecision.REJECT,
                rationale=deterministic_error,
                blockers=(deterministic_error,),
                packet_hash=packet_hash,
            )
        if self._evaluator is None:
            reason = "Hermes evaluator is not configured"
            return HermesVerdict(
                decision=HermesDecision.HOLD,
                rationale=reason,
                blockers=(reason,),
                packet_hash=packet_hash,
            )
        try:
            preflight = getattr(self._evaluator, "preflight", None)
            if callable(preflight):
                result = preflight()
                if inspect.isawaitable(result):
                    await result
            raw = self._evaluator(packet)
            if inspect.isawaitable(raw):
                raw = await raw
            verdict = _parse_verdict(raw, packet_hash=packet_hash)
        except Exception as exc:  # external governance must fail closed
            reason = f"Hermes preflight/evaluator failed: {exc}"
            return HermesVerdict(
                decision=HermesDecision.HOLD,
                rationale=reason,
                blockers=(reason,),
                packet_hash=packet_hash,
            )
        if verdict is None:
            reason = "Hermes returned an invalid verdict"
            return HermesVerdict(
                decision=HermesDecision.HOLD,
                rationale=reason,
                blockers=(reason,),
                packet_hash=packet_hash,
            )
        if verdict.requires_human_review and verdict.decision == HermesDecision.PASS:
            verdict = HermesVerdict(
                decision=HermesDecision.READY_FOR_HUMAN_REVIEW,
                rationale=verdict.rationale or "Hermes requires human review",
                blockers=verdict.blockers,
                challenges=verdict.challenges,
                risks=verdict.risks,
                missing_evidence=verdict.missing_evidence,
                packet_hash=verdict.packet_hash,
                requires_human_review=True,
            )
        if packet.kind == "mission" and verdict.decision == HermesDecision.PASS:
            verdict = HermesVerdict(
                decision=HermesDecision.MISSION_COMPLETE_SUPPORTED,
                rationale=verdict.rationale,
                blockers=verdict.blockers,
                challenges=verdict.challenges,
                risks=verdict.risks,
                missing_evidence=verdict.missing_evidence,
                packet_hash=verdict.packet_hash,
                requires_human_review=verdict.requires_human_review,
            )
        if (
            packet.kind == "candidate"
            and verdict.decision == HermesDecision.MISSION_COMPLETE_SUPPORTED
        ):
            return HermesVerdict(
                decision=HermesDecision.HOLD,
                rationale="mission completion verdict cannot authorize a candidate",
                blockers=("wrong Hermes verdict for candidate packet",),
                packet_hash=packet_hash,
            )
        if _is_critical(packet) and verdict.decision == HermesDecision.PASS:
            return HermesVerdict(
                decision=HermesDecision.READY_FOR_HUMAN_REVIEW,
                rationale=verdict.rationale or "critical authority surface requires human review",
                blockers=verdict.blockers,
                challenges=verdict.challenges,
                risks=verdict.risks,
                missing_evidence=verdict.missing_evidence,
                packet_hash=packet_hash,
                requires_human_review=True,
            )
        return verdict


def _parse_verdict(
    raw: Mapping[str, Any] | HermesVerdict | Any,
    *,
    packet_hash: str,
) -> HermesVerdict | None:
    if isinstance(raw, HermesVerdict):
        return HermesVerdict(
            decision=raw.decision,
            rationale=raw.rationale[:2000],
            blockers=tuple(str(item)[:500] for item in raw.blockers[:16]),
            challenges=tuple(dict(item) for item in raw.challenges[:16]),
            risks=tuple(str(item)[:500] for item in raw.risks[:16]),
            missing_evidence=tuple(str(item)[:500] for item in raw.missing_evidence[:16]),
            packet_hash=packet_hash,
            requires_human_review=raw.requires_human_review,
        )
    if not isinstance(raw, Mapping):
        return None
    try:
        decision = HermesDecision(str(raw.get("decision") or ""))
    except ValueError:
        return None
    challenges = raw.get("challenges")
    if not isinstance(challenges, (list, tuple)):
        challenges = []
    bounded_challenges = tuple(dict(item) for item in challenges[:16] if isinstance(item, Mapping))
    return HermesVerdict(
        decision=decision,
        rationale=str(raw.get("rationale") or "")[:2000],
        blockers=_strings(raw.get("blockers")),
        challenges=bounded_challenges,
        risks=_strings(raw.get("risks")),
        missing_evidence=_strings(raw.get("missing_evidence")),
        packet_hash=packet_hash,
        requires_human_review=bool(raw.get("requires_human_review")),
    )


def _deterministic_failure(packet: ReviewPacket) -> str | None:
    if packet.kind == "candidate":
        if not packet.verification_results:
            return "candidate packet has no deterministic verification results"
        if any(item.get("passed") is not True for item in packet.verification_results):
            return "deterministic candidate proof is not complete"
        if packet.release_results.get("review_eligible") is False:
            return "candidate review evidence is not eligible"
    elif packet.kind == "mission":
        if packet.release_results.get("task_status") != "complete":
            return "mission packet has no complete final proof task"
        if packet.release_results.get("review_eligible") is not True:
            return "mission packet has no eligible final review"
    else:
        return f"unsupported Hermes packet kind: {packet.kind}"
    return None


def _is_critical(packet: ReviewPacket) -> bool:
    if str(packet.risk.get("level") or "").lower() in {"high", "critical"}:
        return True
    critical_prefixes = (
        "src/athena/hermes/",
        "src/athena/self_host/",
        "src/athena/release/",
        "src/athena/kernel/",
        "src/athena/policy/",
        "src/athena/reality/",
        "src/athena/shadow/",
        "src/athena/execution/",
        "src/athena/recovery/",
        "src/athena/tasks/",
        "src/athena/state/",
        "src/athena/protocol/",
        ".github/workflows/",
        "tests/contract/",
        "tests/security/",
        "tests/crash/",
    )
    critical_paths = {
        "scripts/release-check",
        "scripts/architecture-lint",
        "SELF_HOSTING.md",
        "SECURITY.md",
        "SPEC.md",
        "BUILDSPEC.md",
        "BEHAVIORSPEC.md",
        "docs/ARCHITECTURE.md",
    }
    paths = packet.risk.get("paths") or ()
    return any(
        (normalized in critical_paths or normalized.startswith(prefix))
        for path in paths
        for normalized in (_normalise_path(path),)
        for prefix in critical_prefixes
    )


def _normalise_path(value: object) -> str:
    path = str(value).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item)[:500] for item in value[:16] if str(item).strip())


__all__ = ["HermesDecision", "HermesEvaluator", "HermesReferee", "HermesVerdict", "ReviewPacket"]
