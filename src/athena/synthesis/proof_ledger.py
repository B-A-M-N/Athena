"""Proof/observation ledger mechanism (P1-10 extraction).

Event-derived proof records, observation persistence, and evidence
status, moved verbatim from :mod:`athena.synthesis.engine`. Subordinate
to SynthesisEngine: this module holds no authority of its own — proof
records are derived views; admission and promotion decisions stay on
the engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from athena.affordances.models import GeneratedCapability
from athena.protocol.events import EV, Event

if TYPE_CHECKING:
    from athena.synthesis.models import SyntheticCapability
    from athena.synthesis.engine import SynthesisEngine

_logger = logging.getLogger(__name__)

__all__ = ["ProofLedger"]


class ProofLedger:
    """Proof/observation machinery for generated capabilities.

    Verbatim extraction from ``SynthesisEngine``: engine-owned state
    resolves through ``self._e`` at call time.
    """

    def __init__(self, engine: SynthesisEngine) -> None:
        self._e = engine

    async def observe_event(self, event: Event) -> None:
        """Consume canonical events that prove generated-tool usefulness.

        ``downstream_verifications`` is only incremented when a generated
        capability completed successfully and the same task later emits a
        passing ``VerificationCompleted`` event.  A model result, caller
        metadata, or synthetic counter cannot manufacture this proof.
        """
        changed = self._e._apply_proof_event(event)
        for cap in changed:
            await self._e._persist_observed_proof(cap)

    def _apply_proof_event(self, event: Event) -> tuple[SyntheticCapability, ...]:
        task_id = event.task_id
        if not task_id:
            return ()
        payload = dict(event.payload or {})
        if event.type == EV["CAPABILITY_COMPLETED"]:
            capability_id = str(payload.get("capability_id") or "")
            call_id = str(payload.get("call_id") or "")
            if capability_id in self._e._synthetic and call_id:
                self._e._pending_verification_calls.setdefault(task_id, {})[call_id] = capability_id
            return ()
        if event.type != EV["VERIFICATION_COMPLETED"] or not payload.get("passed"):
            return ()
        pending = self._e._pending_verification_calls.pop(task_id, {})
        changed: list[SyntheticCapability] = []
        for capability_id in pending.values():
            cap = self._e._synthetic.get(capability_id)
            if cap is None:
                continue
            cap.downstream_verifications += 1
            if cap not in changed:
                changed.append(cap)
        return tuple(changed)

    async def replay_event_metrics(self, events) -> None:
        """Rebuild verification metrics from the durable event stream.

        Promoted capabilities survive restart, but the in-memory pending-call
        map does not. Replaying the canonical log restores the metric without
        trusting the previously serialized counter as authority.
        """
        if events is None or not self._e._synthetic:
            return
        for cap in self._e._synthetic.values():
            cap.downstream_verifications = 0
        self._e._pending_verification_calls.clear()
        rowid = 0
        changed: dict[str, SyntheticCapability] = {}
        while True:
            batch = await events.list_recent(after_rowid=rowid, limit=500)
            if not batch:
                break
            for event in batch:
                for cap in self._e._apply_proof_event(event):
                    changed[cap.id] = cap
                rowid = max(rowid, int(getattr(event, "_rowid", rowid)))
            if len(batch) < 500:
                break
        for cap in changed.values():
            await self._e._persist_observed_proof(cap)

    async def _persist_observed_proof(self, cap: SyntheticCapability) -> None:
        if self._e._proof_sink is None:
            return
        try:
            await self._e._proof_sink(cap.id, self._e._proof_record(cap))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning(
                "could not persist event-derived proof for %s: %s",
                cap.id,
                exc,
            )

    async def evidence_status(
        self,
        capability: SyntheticCapability | GeneratedCapability,
        research_store,
    ) -> dict:
        """Check the research revisions a generated capability relies on.

        A source/evidence reference is a proof dependency, not an execution
        permission.  The check is deliberately conservative: a missing
        source/evidence object or a changed captured source hash makes the
        capability stale and therefore unavailable until it is revalidated.
        """
        dependencies = tuple(getattr(capability, "evidence_dependencies", ()) or ())
        if not dependencies:
            return {"status": "CURRENT", "dependencies": []}
        if research_store is None:
            return {
                "status": "REVALIDATION_REQUIRED",
                "dependencies": [
                    {"requirement": dependency.requirement, "status": "research_store_unavailable"}
                    for dependency in dependencies
                ],
            }
        checks: list[dict] = []
        stale = False
        for dependency in dependencies:
            source_id = dependency.source_id
            evidence = None
            if dependency.evidence_id:
                evidence = await research_store.get_evidence(dependency.evidence_id)
                if evidence is None:
                    checks.append(
                        {
                            **dependency.to_record(),
                            "status": "missing_evidence",
                        }
                    )
                    stale = True
                    continue
                if source_id is None:
                    source_id = evidence.source_id
                elif evidence.source_id != source_id:
                    checks.append(
                        {
                            **dependency.to_record(),
                            "status": "evidence_source_mismatch",
                        }
                    )
                    stale = True
                    continue
            source = await research_store.get_source(source_id) if source_id else None
            if source is None:
                checks.append(
                    {
                        **dependency.to_record(),
                        "status": "missing_source",
                    }
                )
                stale = True
                continue
            actual_hash = source.content_hash
            expected_hash = dependency.content_hash
            if expected_hash and actual_hash != expected_hash:
                checks.append(
                    {
                        **dependency.to_record(),
                        "status": "source_revision_changed",
                        "actual_content_hash": actual_hash,
                    }
                )
                stale = True
                continue
            if expected_hash:
                latest = await research_store.latest_source_for_uri(source.canonical_uri)
                if (
                    latest is not None
                    and latest.id != source.id
                    and latest.content_hash != expected_hash
                ):
                    checks.append(
                        {
                            **dependency.to_record(),
                            "source_id": source.id,
                            "status": "source_revision_changed",
                            "actual_content_hash": latest.content_hash,
                            "latest_source_id": latest.id,
                        }
                    )
                    stale = True
                    continue
            checks.append(
                {
                    **dependency.to_record(),
                    "source_id": source.id,
                    "actual_content_hash": actual_hash,
                    "status": "current",
                }
            )
        return {
            "status": "STALE" if stale else "CURRENT",
            "dependencies": checks,
        }

    @staticmethod
    def _proof_record(cap: SyntheticCapability) -> dict:
        live_quality = cap.successes / cap.uses if cap.uses else 0.0
        validation_quality = (
            cap.validation.get("cases_passed", 0) / cap.validation.get("cases_total", 1)
            if cap.validation.get("cases_total")
            else 0.0
        )
        return {
            **dict(cap.validation),
            "lifecycle_state": cap.lifecycle_state,
            "quality_score": round(
                (validation_quality + live_quality) / (2 if cap.uses else 1),
                4,
            ),
            "last_used_at": cap.last_used_at,
            "fixture_count": len(cap.validation_cases or []),
            "fixture_hashes": [
                hashlib.sha256(
                    json.dumps(case, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                for case in (cap.validation_cases or [])
            ],
            "required_capabilities": list(cap.required_capabilities),
            "supersedes": list(cap.supersedes),
            "evidence_dependencies": [
                dependency.to_record() for dependency in cap.evidence_dependencies
            ],
            "distinct_inputs": len(cap.input_signatures),
            "input_signatures": sorted(cap.input_signatures)[:64],
            "distinct_task_contexts": len(cap.task_context_signatures),
            "task_context_signatures": sorted(cap.task_context_signatures)[:64],
            "distinct_environments": len(cap.environment_fingerprints),
            "environment_fingerprints": sorted(cap.environment_fingerprints)[:64],
            "reuse_count": cap.reuse_count,
            "downstream_verifications": cap.downstream_verifications,
            "latency_saved_ms": round(cap.latency_saved_ms, 2),
            "turns_saved": cap.turns_saved,
            # Zero is not evidence that a savings metric was observed. Keep
            # the provenance explicit so promotion/ranking cannot mistake
            # unmeasured fields for a benchmark supplied by the caller.
            "metric_provenance": {
                "reuse_count": "canonical_generated_invocation",
                "downstream_verifications": "canonical_passing_verification",
                "latency_saved_ms": "not_measured",
                "turns_saved": "not_measured",
            },
            "validation_strength": cap.validation.get("tier", "unknown"),
            "family_id": cap.family_id,
            "revision": cap.revision,
            "parent_revision": cap.parent_revision,
            "active_revision": cap.active_revision,
            "superseded_by": cap.superseded_by,
            "usage": {
                "uses": cap.uses,
                "successes": cap.successes,
                "failures": cap.failures,
            },
        }

    def proof_for(self, cap_id: str) -> dict | None:
        """Proof-carrying summary for a synthetic capability."""
        cap = self._e._synthetic.get(cap_id)
        if cap is None:
            return None
        return {
            "id": cap.id,
            "runtime": cap.runtime,
            "validation": cap.validation,
            "uses": cap.uses,
            "successes": cap.successes,
            "failures": cap.failures,
            "provenance": cap.provenance,
            "effects": sorted(getattr(e, "value", str(e)) for e in cap.effects),
            "effective_authority": sorted(cap.effective_effects),
            "required_capabilities": list(cap.required_capabilities),
            "evidence_dependencies": [
                dependency.to_record() for dependency in cap.evidence_dependencies
            ],
            "code_hash": hashlib.sha256(cap.code.encode()).hexdigest(),
            "lifecycle_state": cap.lifecycle_state,
            "quality_score": self._e._proof_record(cap).get("quality_score", 0.0),
            "last_used_at": cap.last_used_at,
            "supersedes": list(cap.supersedes),
            "family_id": cap.family_id,
            "revision": cap.revision,
            "parent_revision": cap.parent_revision,
            "active_revision": cap.active_revision,
            "superseded_by": cap.superseded_by,
            "dependency_lock": self._e._dependency_lock(cap),
        }
