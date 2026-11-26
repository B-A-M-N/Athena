"""Task-history complexity evidence for canonical dispatch batches."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from athena.capabilities.prepared import PreparedCapabilityCall
from athena.protocol.capabilities import CapabilityOrigin, EffectClass

__all__ = ["ComplexityFacts", "ComplexityLedger", "RuntimeEscalation"]


@dataclass
class ComplexityFacts:
    """Mutable per-task facts. A fresh instance prevents cross-task leaks."""

    resources: list[str] = field(default_factory=list)
    mutation_count: int = 0
    execute_observed: bool = False
    opaque_observed: bool = False
    dependency_schema_observed: bool = False
    candidate_verification_failures: int = 0
    capability_failures: int = 0
    infrastructure_failures: int = 0
    candidate_attempt_failures: int = 0

    def to_record(self) -> dict[str, Any]:
        return {
            "resources": list(self.resources),
            "mutation_count": self.mutation_count,
            "execute_observed": self.execute_observed,
            "opaque_observed": self.opaque_observed,
            "dependency_schema_observed": self.dependency_schema_observed,
            "candidate_verification_failures": self.candidate_verification_failures,
            "capability_failures": self.capability_failures,
            "infrastructure_failures": self.infrastructure_failures,
            "candidate_attempt_failures": self.candidate_attempt_failures,
        }


class ComplexityLedger:
    """Deterministic per-task history owned by dispatcher/reality.

    The ledger is intentionally conservative and model-unreachable. It does
    not plan work or choose a route; it records facts from prepared calls so
    complexity can be recognized across turns instead of within one batch.
    """

    def __init__(self, store: dict[str, ComplexityFacts] | None = None) -> None:
        self._store = store if store is not None else {}

    def record_prepared(self, prepared: PreparedCapabilityCall) -> dict[str, Any] | None:
        task_id = str(prepared.request.task_id or "")
        if not task_id or prepared.executor is None:
            return None
        effects = set(prepared.effects)
        mutating = bool(
            effects & {EffectClass.WRITE_LOCAL, EffectClass.DELETE, EffectClass.PRIVILEGED}
        )
        executing = bool(effects & {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS})
        if not (mutating or executing):
            return None
        ledger = self._entry(task_id)
        for key in prepared.resource_keys:
            if key not in ledger.resources:
                ledger.resources.append(key)
        ledger.resources = sorted(ledger.resources)[:64]
        if mutating:
            ledger.mutation_count += 1
        if executing:
            ledger.execute_observed = True
        descriptor = getattr(prepared.executor, "descriptor", None)
        if getattr(descriptor, "origin", None) not in (None, CapabilityOrigin.NATIVE) or str(
            prepared.request.capability_id
        ) in {"execute", "terminal_session", "debugger", "process", "shell", "bash"}:
            ledger.opaque_observed = True
        args = prepared.request.arguments or {}
        operation = str(args.get("operation") or args.get("action") or "").casefold()
        capability_id = str(prepared.request.capability_id).casefold()
        if operation in {
            "install",
            "uninstall",
            "upgrade",
            "migrate",
            "build",
        } or capability_id in {
            "dependency",
            "database",
            "schema",
        }:
            ledger.dependency_schema_observed = True
        return ledger.to_record() if self.is_complex(task_id) else None

    def record_candidate_verification_failure(self, task_id: str) -> bool:
        """Record a real candidate verification failure (complexity signal)."""
        if not task_id:
            return False
        self._entry(task_id).candidate_verification_failures += 1
        return self.is_complex(task_id)

    def record_capability_failure(
        self, task_id: str, *, infrastructure: bool, candidate_attempt: bool = False
    ) -> None:
        """Record ordinary capability failures without escalation semantics."""
        if not task_id:
            return
        ledger = self._entry(task_id)
        ledger.capability_failures += 1
        if infrastructure:
            ledger.infrastructure_failures += 1
        if candidate_attempt:
            ledger.candidate_attempt_failures += 1

    def is_complex(self, task_id: str) -> bool:
        ledger = self._store.get(task_id)
        if not ledger:
            return False
        return bool(
            ledger.mutation_count > 1
            or (ledger.mutation_count and ledger.execute_observed)
            or ledger.opaque_observed
            or ledger.dependency_schema_observed
            or ledger.candidate_verification_failures
        )

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        ledger = self._store.get(task_id)
        return ledger.to_record() if ledger else None

    def reconstruct_from_events(self, task_id: str, events: Any) -> dict[str, Any] | None:
        """Rebuild deterministic complexity facts from canonical event history.

        This does not introduce a second authority: the canonical event log
        remains durable truth. It derives the same conservative facts the live
        dispatcher recorded, without using prose or model claims.
        """
        if not task_id:
            return None
        facts = self._entry(task_id)
        requested: dict[str, dict[str, Any]] = {}
        for event in events or ():
            event_type = str(getattr(event, "type", "") or "")
            payload = dict(getattr(event, "payload", {}) or {})
            capability_id = str(payload.get("capability_id") or "").casefold()
            operation = str(payload.get("operation") or "").casefold()
            if event_type == "CapabilityRequested":
                requested[str(payload.get("call_id") or "")] = {
                    "capability_id": capability_id,
                    "operation": operation,
                    "arguments": dict(payload.get("arguments") or {}),
                }
                continue
            if event_type != "MutationPrepared":
                continue
            resource = str(payload.get("resource") or "")
            if resource and resource not in facts.resources:
                facts.resources.append(resource)
            if operation in {"write", "patch", "mkdir", "copy", "move", "delete"}:
                facts.mutation_count += 1
            if capability_id in {
                "execute",
                "terminal_session",
                "debugger",
                "process",
                "shell",
                "bash",
            }:
                facts.execute_observed = True
                facts.opaque_observed = True
            if operation in {"install", "uninstall", "upgrade", "migrate", "build"} or (
                capability_id in {"dependency", "database", "schema"}
            ):
                facts.dependency_schema_observed = True
        facts.resources = sorted(facts.resources)[:64]
        return facts.to_record()

    def _entry(self, task_id: str) -> ComplexityFacts:
        entry = self._store.get(task_id)
        if entry is None:
            entry = ComplexityFacts()
            self._store[task_id] = entry
        return entry


class RuntimeEscalation:
    """Inspect prepared calls and mark tasks needing a sticky candidate."""

    def __init__(self, dispatcher: Any, ledger: ComplexityLedger | None = None) -> None:
        self._d = dispatcher
        self._ledger = ledger or ComplexityLedger(getattr(dispatcher, "_complexity_ledger", None))

    def _escalate_complex_prepared_batch(
        self,
        prepared_calls: list[PreparedCapabilityCall],
    ) -> bool:
        """Record task-history complexity before any member executes.

        A task already on a sticky candidate has the required isolation and is
        not re-routed. A ledger-complex task that reaches a later transactional
        route is explicitly marked late; RealityGate keeps its compensation
        checkpoint and completion ownership.
        """
        complex_tasks: set[str] = set()
        for prepared in prepared_calls:
            task_id = str(prepared.request.task_id or "")
            if self._ledger.record_prepared(prepared) is not None:
                complex_tasks.add(task_id)
        escalated = False
        for task_id in sorted(complex_tasks):
            active = getattr(self._d._reality_gate, "active_branch", None)
            if callable(active) and active(task_id) is not None:
                continue
            controlled_commit = all(
                prepared.directives is not None
                and prepared.directives.transaction_id
                and getattr(prepared.request.origin, "value", prepared.request.origin)
                == "trusted_orchestration"
                for prepared in prepared_calls
                if prepared.request.task_id == task_id and prepared.executor is not None
            )
            if controlled_commit:
                continue
            escalated = True
            self._d._runtime_speculative_tasks.add(task_id)
            self._d._late_complexity_escalations.add(task_id)
        return escalated
