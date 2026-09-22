"""Synthesis shared data types (review item 30).

SyntheticCapability and RegressionCase live here so helper, promotion, and
validation modules import types from a neutral sibling instead of the engine
implementation module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from athena.affordances.models import DependencyRequirement, EvidenceDependency

_GENERATED_EFFECTIVE_AUTHORITY = frozenset(
    {
        "READ_LOCAL",
        "EXECUTE",
    }
)


@dataclass
class SyntheticCapability:
    """A generated, validated, task-scoped executable capability."""

    id: str
    name: str
    description: str
    code: str  # python source defining `def run(args)`
    input_schema: dict
    effects: frozenset  # declared effect envelope
    task_id: str | None
    provenance: dict  # originating task/call ids
    validation: dict  # test results from sandbox run
    runtime: str = "python"
    uses: int = 0
    successes: int = 0
    failures: int = 0
    validation_cases: list[dict] | None = None
    required_dependencies: tuple[DependencyRequirement, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    evidence_dependencies: tuple[EvidenceDependency, ...] = ()
    input_signatures: set[str] = field(default_factory=set)
    task_context_signatures: set[str] = field(default_factory=set)
    environment_fingerprints: set[str] = field(default_factory=set)
    reuse_count: int = 0
    downstream_verifications: int = 0
    latency_saved_ms: float = 0.0
    turns_saved: int = 0
    # This is calculated by Athena's sandbox contract, not trusted from the
    # generated source or its declared effects.
    effective_effects: frozenset[str] = _GENERATED_EFFECTIVE_AUTHORITY
    output_schema: dict | None = None
    lifecycle_state: str = "DRAFT"
    family_id: str = ""
    revision: int = 1
    parent_revision: int | None = None
    active_revision: int | None = None
    supersedes: tuple[str, ...] = ()
    superseded_by: str | None = None
    dependency_lock: dict = field(default_factory=dict)
    last_used_at: str | None = None
    id_generated: bool = False


@dataclass(frozen=True)
class RegressionCase:
    """Durable, revision-aware record of a live generated failure.

    ``args`` remains in the serialized form for replay compatibility, while
    ``input`` is the canonical audit field.  A repair inherits only cases
    whose ``resolved_by_revision`` is empty; old records are normalized with
    the predecessor's identity rather than being silently discarded.
    """

    id: str
    capability_family: str
    revision_first_seen: int
    source: str
    input: Mapping[str, object]
    environment_fingerprint: str | None
    expected_contract: Mapping[str, object]
    observed_failure: str
    failure_class: str
    resolved_by_revision: int | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "capability_family": self.capability_family,
            "revision_first_seen": self.revision_first_seen,
            "source": self.source,
            "input": dict(self.input),
            # ``args`` is the stable replay alias used by pre-existing repair
            # requests and by the validation runner.
            "args": dict(self.input),
            "environment_fingerprint": self.environment_fingerprint,
            "expected_contract": dict(self.expected_contract),
            "observed_failure": self.observed_failure,
            "failure_class": self.failure_class,
            "resolved_by_revision": self.resolved_by_revision,
        }

    @classmethod
    def from_record(
        cls,
        record: Mapping[str, object],
        *,
        capability_family: str,
        revision: int,
    ) -> "RegressionCase":
        raw_input = record.get("input", record.get("args", {}))
        input_value = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        raw_revision = record.get("revision_first_seen")
        raw_resolved = record.get("resolved_by_revision")
        raw_contract = record.get("expected_contract")
        return cls(
            id=str(record.get("id") or ""),
            capability_family=str(record.get("capability_family") or capability_family),
            revision_first_seen=(int(str(raw_revision)) if raw_revision is not None else revision),
            source=str(record.get("source") or "live_failure"),
            input=input_value,
            environment_fingerprint=(
                str(record["environment_fingerprint"])
                if record.get("environment_fingerprint")
                else None
            ),
            expected_contract=dict(raw_contract) if isinstance(raw_contract, Mapping) else {},
            observed_failure=str(record.get("observed_failure") or ""),
            failure_class=str(record.get("failure_class") or "implementation_failure"),
            resolved_by_revision=(int(str(raw_resolved)) if raw_resolved is not None else None),
        )
