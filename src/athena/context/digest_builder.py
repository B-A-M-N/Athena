"""Build a compact context digest from bounded read-only projections."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from athena.context.digest import ContextDigest
from athena.kernel.termination import result_qualifies_as_work_evidence
from athena.protocol.messages import (
    ArtifactRefBlock,
    CapabilityCallBlock,
    CapabilityResultBlock,
    FileRefBlock,
    Role,
)
from athena.protocol.tasks import TaskSpec


class ContextDigestBuilder:
    """Translate compiler inputs into typed durable working state.

    Storage and compilation remain separate: this builder only consumes the
    bounded projections handed to it and never queries another subsystem.
    """

    def build(
        self,
        task: TaskSpec,
        *,
        required: Sequence[Any],
        corpus: Sequence[Any],
        previous: ContextDigest | None,
        principal_id: str,
        omitted: Sequence[str] = (),
        compression: Any = None,
        runtime_sessions: Sequence[Mapping[str, Any]] = (),
        child_tasks: Sequence[Mapping[str, Any]] = (),
    ) -> ContextDigest:
        entries = (*required, *corpus)
        current = self._fields(task, entries, runtime_sessions, child_tasks)
        if previous is not None:
            fields = {
                name: _merge(previous.normalized_fields().get(name), current.get(name))
                for name in current
            }
        else:
            fields = current
        anchors = tuple(
            anchor
            for marker in getattr(compression, "markers", ())
            for anchor in (marker.message_ids or marker.selection_ids)
        )
        message_ids = tuple(
            str(getattr(entry.message, "id", ""))
            for entry in entries
            if getattr(entry, "message", None) is not None
        )
        if not anchors:
            anchors = message_ids[:8]
        return ContextDigest(
            task_id=task.id,
            session_id=task.session_id,
            principal_id=principal_id,
            level=(previous.level + 1) if previous is not None else 1,
            parent_digest_id=previous.id if previous is not None else None,
            source_digest_ids=(previous.id,) if previous is not None else (),
            range_start_message_id=message_ids[0] if message_ids else None,
            range_end_message_id=message_ids[-1] if message_ids else None,
            fields=fields,
            transcript_anchors=tuple(dict.fromkeys(anchors))[:16],
            recovery_queries=tuple(dict.fromkeys((task.objective, *omitted)))[:16],
        )

    def _fields(
        self,
        task: TaskSpec,
        entries: Sequence[Any],
        runtime_sessions: Sequence[Mapping[str, Any]],
        child_tasks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        metadata = dict(task.metadata or {})
        capability_results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        decisions: list[str] = []
        evidence: list[Any] = []
        artifacts: list[str] = []
        resources: list[str] = []
        pending_successes: list[CapabilityResultBlock] = []
        pending_failure = False
        for entry in entries:
            provenance = getattr(entry, "provenance", None)
            source_id = getattr(provenance, "source_id", None)
            if source_id:
                resources.append(str(source_id))
            message = getattr(entry, "message", None)
            if message is None:
                continue
            for block in message.blocks:
                if isinstance(block, CapabilityResultBlock):
                    record = {
                        "kind": str((block.metadata or {}).get("work_kind") or "observation"),
                        "capability_id": block.capability_id,
                        "status": "completed" if block.ok else "failed",
                        "proof_reference": (
                            block.ref_uri
                            or (block.metadata or {}).get("result_id")
                            or block.call_id
                        ),
                    }
                    if block.ok:
                        proof = result_qualifies_as_work_evidence(block)
                        if proof is not None:
                            record["kind"] = proof.kind
                            record["proof_reference"] = (
                                proof.artifact_ref
                                or proof.mutation_ref
                                or proof.external_receipt
                                or record["proof_reference"]
                            )
                            capability_results.append(record)
                            pending_successes.append(block)
                    else:
                        failures.append({**record, "error": (block.error or "")[:600]})
                        pending_failure = True
                    evidence.append(record)
                if isinstance(block, (ArtifactRefBlock, FileRefBlock)):
                    uri = str(getattr(block, "uri", "") or "")
                    if uri:
                        resources.append(uri)
                        if isinstance(block, ArtifactRefBlock):
                            artifacts.append(uri)
                if isinstance(block, CapabilityCallBlock):
                    # A plan to use a capability is not evidence and cannot
                    # close a pending result/conclusion group.
                    continue
            if getattr(message, "role", None) is Role.USER:
                pending_successes.clear()
                pending_failure = False
            elif getattr(message, "role", None) is Role.ASSISTANT:
                durable = bool((message.metadata or {}).get("durable_decision"))
                has_capability_call = any(
                    isinstance(item, CapabilityCallBlock) for item in message.blocks
                )
                if durable or (
                    pending_successes and not pending_failure and not has_capability_call
                ):
                    text = message.conversation_text().strip().replace("\n", " ")
                    if text:
                        decisions.append(text[:600])
                    pending_successes.clear()
                    pending_failure = False
            if getattr(entry, "category", "") == "artifact":
                artifacts.append(str(source_id or getattr(entry, "name", "")))

        criteria_states = metadata.get("acceptance_state") or metadata.get("_acceptance_state")
        acceptance: list[dict[str, Any]] = []
        for criterion in task.acceptance_criteria:
            raw = (
                criteria_states.get(criterion.id) if isinstance(criteria_states, Mapping) else None
            )
            if isinstance(raw, bool):
                raw = {"status": "satisfied" if raw else "unresolved"}
            raw = raw if isinstance(raw, Mapping) else {}
            status = str(raw.get("status") or "unresolved").lower()
            if status not in {"satisfied", "unresolved", "failed"}:
                status = "unresolved"
            acceptance.append(
                {
                    "id": criterion.id,
                    "required": criterion.required,
                    "status": status,
                    "proof_reference": raw.get("proof_reference"),
                }
            )
        runtime_state = [dict(item) for item in runtime_sessions[:8]]
        if not runtime_state and metadata.get("_runtime_recovery_hint"):
            runtime_state.append(dict(metadata["_runtime_recovery_hint"]))
        return {
            "objective": task.objective,
            "constraints": [
                {
                    "id": criterion.id,
                    "required": criterion.required,
                    "description": criterion.description,
                }
                for criterion in task.acceptance_criteria
            ],
            "decisions": _unique(decisions),
            "completed_work": _unique_records(capability_results),
            "pending_work": list(metadata.get("pending_work") or ()),
            "files_resources": _unique(resources + artifacts),
            "runtime_state": runtime_state,
            "child_tasks": [dict(item) for item in child_tasks[:8]]
            or list(metadata.get("child_tasks") or ()),
            "evidence": _unique_records(evidence),
            "artifacts": _unique(artifacts),
            "failures": _unique_records(failures),
            "open_questions": list(metadata.get("open_questions") or ()),
            "acceptance_state": acceptance,
        }


def _merge(old: Any, new: Any) -> Any:
    if isinstance(old, list) and isinstance(new, list):
        return _unique(old + new)
    if old and not new:
        return old
    return new if new else old or []


def _unique(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, sort_keys=True, default=str)
        if value and marker not in seen:
            seen.add(marker)
            result.append(value)
    return result[:16]


def _unique_records(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(value) for value in _unique(list(values))]


__all__ = ["ContextDigestBuilder"]
