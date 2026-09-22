"""Neutral work-evidence contracts shared by context and kernel mechanisms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.strategy import EXTERNAL_ACTION, EXECUTION, MUTATION, OBSERVATION

__all__ = ["WorkEvidence", "result_qualifies_as_work_evidence"]


@dataclass(frozen=True)
class WorkEvidence:
    """Typed proof that a capability crossed the requested work boundary."""

    kind: str
    capability_id: str
    operation: str = ""
    call_id: str = ""
    mutation_ref: str | None = None
    artifact_ref: str | None = None
    external_receipt: str | None = None
    research_ready: bool = False
    research_bundle_id: str | None = None
    research_requirement_ids: tuple[str, ...] = ()
    research_evidence_ids: tuple[str, ...] = ()
    research_gap_ids: tuple[str, ...] = ()


_CONTROL_CAPABILITIES = frozenset(
    {
        "capabilities",
        "skills",
        "workflows",
        "reflection",
        "request_input",
        "delegate.status",
        "delegate.collect",
        "schedule.describe",
        "schedule.list",
        "workflow.describe",
        "workflow.list",
    }
)
_READ_OPERATIONS = frozenset(
    {"read", "list", "stat", "get", "exists", "open", "diff", "status", "inspect"}
)
_MUTATION_OPERATIONS = frozenset(
    {
        "write",
        "patch",
        "mkdir",
        "copy",
        "move",
        "delete",
        "remove",
        "update",
        "create",
        "apply",
        "save",
    }
)


def result_qualifies_as_work_evidence(
    result: Any,
    *,
    call: Any = None,
    required_kind: str | None = None,
) -> WorkEvidence | None:
    """Convert one successful result into qualifying, typed work evidence.

    Discovery/control results are intentionally not ordinary work evidence:
    finding a capability, recalling memory, listing a workflow, or asking a
    delegate for status cannot satisfy a task that required the underlying
    observation, execution, mutation, artifact, or external receipt.
    """
    if not bool(getattr(result, "ok", False)):
        status = getattr(getattr(result, "status", None), "value", None)
        if status != "ok":
            return None
    capability_id = str(getattr(result, "capability_id", "") or "")
    base_capability = capability_id.split(".", 1)[0]
    if not capability_id or capability_id in _CONTROL_CAPABILITIES:
        return None
    metadata = dict(getattr(result, "metadata", None) or {})
    arguments = dict(getattr(call, "arguments", None) or {})
    operation = str(
        arguments.get("operation") or arguments.get("action") or metadata.get("operation") or ""
    ).casefold()
    if capability_id.startswith("capabilities."):
        return None
    if capability_id.startswith("skills."):
        return None
    if capability_id.startswith("workflows."):
        return None
    if base_capability == "memory" and operation in {
        "",
        "recall",
        "search",
        "list",
        "get",
        "inspect",
    }:
        return None
    if base_capability == "workflow" and operation in {
        "",
        "list",
        "describe",
        "inspect",
    }:
        return None
    if base_capability == "delegate" and operation == "status":
        return None
    mutation = metadata.get("mutation")
    mutation_ref = None
    if isinstance(mutation, dict):
        mutation_ref = str(mutation.get("mutation_id") or mutation.get("id") or "") or None
    mutation_ref = (
        mutation_ref
        or str(metadata.get("mutation_ref") or metadata.get("mutation_id") or "")
        or None
    )
    artifact_ref = (
        str(getattr(result, "ref_uri", None) or metadata.get("artifact_uri") or "") or None
    )
    receipt = metadata.get("external_receipt") or metadata.get("receipt_id")
    external_receipt = str(receipt) if receipt else None
    research_completion = metadata.get("research_completion")
    research_ready = bool(
        isinstance(research_completion, dict) and research_completion.get("ready") is True
    )
    research_bundle_id = (
        str(research_completion.get("bundle_id"))
        if isinstance(research_completion, dict) and research_completion.get("bundle_id")
        else None
    )
    completion_record = research_completion if isinstance(research_completion, dict) else {}
    research_requirement_ids = tuple(
        str(item) for item in completion_record.get("requirement_ids") or ()
    )
    research_evidence_ids = tuple(str(item) for item in completion_record.get("evidence_ids") or ())
    research_gap_ids = tuple(str(item) for item in completion_record.get("closed_gap_ids") or ())

    capability_leaf = capability_id.rsplit(".", 1)[-1]
    # Canonical receipt path: the dispatcher has already resolved the exact
    # effects.  Prefer this over operation-name heuristics — it is the
    # authority for what the capability was allowed to cause. Match
    # case-insensitively: the dispatcher stamps EffectClass enum values
    # (uppercase), while policy metadata may carry lowercase effect names.
    resolved = metadata.get("resolved_effects")
    if resolved:
        resolved_set = {str(item).casefold() for item in resolved}
        if resolved_set & {"write_local", "delete"}:
            kind = MUTATION
        elif resolved_set & {"network_write", "external_message", "external_publish"}:
            kind = EXTERNAL_ACTION
        elif resolved_set & {"execute", "spawn_process"}:
            kind = EXECUTION
        elif resolved_set & {"read_local", "network_read"}:
            kind = OBSERVATION
        elif artifact_ref is not None and required_kind in {None, "artifact"}:
            kind = "artifact"
        else:
            kind = OBSERVATION
    elif (
        capability_id in {"execute", "shell", "process"}
        or capability_leaf in {"execute", "shell", "process"}
        or operation in {"run", "exec", "execute", "pytest"}
    ):
        kind = EXECUTION
    elif operation in _MUTATION_OPERATIONS or mutation_ref is not None:
        kind = MUTATION
    elif external_receipt is not None:
        kind = EXTERNAL_ACTION
    elif artifact_ref is not None and required_kind in {None, "artifact"}:
        kind = "artifact"
    elif operation in _READ_OPERATIONS or capability_id in {"fs", "git", "research", "http"}:
        kind = OBSERVATION
    else:
        kind = OBSERVATION

    evidence = WorkEvidence(
        kind=kind,
        capability_id=capability_id,
        operation=operation,
        call_id=str(getattr(result, "call_id", "") or ""),
        mutation_ref=mutation_ref,
        artifact_ref=artifact_ref,
        external_receipt=external_receipt,
        research_ready=research_ready,
        research_bundle_id=research_bundle_id,
        research_requirement_ids=research_requirement_ids,
        research_evidence_ids=research_evidence_ids,
        research_gap_ids=research_gap_ids,
    )
    if required_kind is not None and required_kind not in {
        kind,
        "artifact" if artifact_ref else kind,
    }:
        return None
    return evidence
