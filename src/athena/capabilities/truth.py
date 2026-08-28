"""Model-visible, execution-grounded truth/proof inspection."""

from __future__ import annotations

import json
from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


class TruthCapability:
    """Expose one normalized view over Athena's existing proof surfaces.

    This is deliberately a read model, not a second truth engine. Claims,
    research records, invariant results, and generated proof records remain
    authoritative in their existing stores; this capability makes their
    relationships inspectable by the same model loop.
    """

    descriptor = CapabilityDescriptor(
        id="truth",
        description=(
            "Inspect execution-grounded claims and the evidence that supports "
            "them, including research sources, invariants, artifacts, and "
            "generated-capability validation proofs. Operations: "
            "status/explain/dependencies/stale."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "status",
                        "explain",
                        "dependencies",
                        "stale",
                    ],
                },
                "claim_id": {"type": "string", "maxLength": 128},
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, service: Any) -> None:
        self._service = service

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        if not request.task_id:
            return _result(request, ok=False, error="truth requires a task scope")
        operation = str((request.arguments or {}).get("operation") or "")
        claim_id = str((request.arguments or {}).get("claim_id") or "") or None
        try:
            workspace = getattr(context, "workspace", None)
            state = await self._service.world_state(request.task_id).snapshot(
                workspace_root=getattr(workspace, "root", None),
            )
            graph = await self._graph(request.task_id, state, workspace)
            if operation == "stale":
                graph = {
                    "task_id": request.task_id,
                    "status": "STALE" if graph["stale"] else "CURRENT",
                    "claims": graph["stale_claims"],
                    "capabilities": graph["stale_capabilities"],
                }
            elif operation == "explain":
                if claim_id is None:
                    return _result(request, ok=False, error="explain requires claim_id")
                claim = next(
                    (item for item in graph["claims"] if item["id"] == claim_id),
                    None,
                )
                if claim is None:
                    return _result(request, ok=False, error=f"claim not found: {claim_id}")
                graph = claim
            elif operation == "dependencies":
                if claim_id is None:
                    return _result(
                        request,
                        ok=False,
                        error="dependencies requires claim_id",
                    )
                claim = next(
                    (item for item in graph["claims"] if item["id"] == claim_id),
                    None,
                )
                if claim is None:
                    return _result(request, ok=False, error=f"claim not found: {claim_id}")
                graph = {
                    "task_id": request.task_id,
                    "claim_id": claim_id,
                    "status": claim.get("status"),
                    "dependencies": list(claim.get("proof_refs") or []),
                    "research_evidence": list(claim.get("research_evidence") or []),
                }
            elif operation != "status":
                return _result(request, ok=False, error=f"unknown operation: {operation}")
            return _result(request, output=json.dumps(graph, default=str))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))

    async def _graph(self, task_id: str, state: dict, workspace) -> dict[str, Any]:
        research = getattr(self._service, "_research_store", None)
        evidence_by_claim: dict[str, list[dict[str, Any]]] = {}
        if research is not None:
            for evidence in await research.list_evidence(
                task_id=task_id,
                project_id=getattr(workspace, "id", None),
                limit=200,
            ):
                record = evidence.to_record()
                claim_id = record.get("claim_id")
                if claim_id:
                    evidence_by_claim.setdefault(str(claim_id), []).append(record)

        claims: list[dict[str, Any]] = []
        for claim in state.get("claims", []):
            record = dict(claim)
            linked = evidence_by_claim.get(str(record["id"]), [])
            record["proof_refs"] = _claim_proof_refs(record, linked)
            if linked:
                record["research_evidence"] = linked
            claims.append(record)

        generated = []
        fabric = getattr(self._service, "_fabric", None)
        if fabric is not None:
            for record in fabric.created_this_task(task_id):
                generated.append(
                    {
                        "id": record.get("id"),
                        "kind": "capability_validation",
                        "status": record.get("lifecycle_state") or "UNKNOWN",
                        "code_hash": record.get("code_hash"),
                        "schema_hash": record.get("schema_hash"),
                        "evidence_dependencies": record.get("evidence_dependencies", []),
                        "proof": record.get("proof_record", {}),
                    }
                )

        stale_claims = [
            claim for claim in claims if claim.get("status") in {"STALE", "CONTRADICTED"}
        ]
        stale_capabilities = [
            capability
            for capability in generated
            if capability.get("status") in {"STALE", "REVALIDATION_REQUIRED", "DEPRECATED"}
        ]
        proof_graph = _proof_graph(
            claims=claims,
            generated=generated,
            invariants=state.get("invariants", []),
            invariant_results=state.get("invariant_results", []),
            observations=state.get("observations", []),
        )
        return {
            "task_id": task_id,
            "status": "STALE" if stale_claims or stale_capabilities else "CURRENT",
            "claims": claims,
            "invariants": state.get("invariants", []),
            "invariant_results": state.get("invariant_results", []),
            "observations": state.get("observations", []),
            "generated_capabilities": generated,
            "stale": bool(stale_claims or stale_capabilities),
            "stale_claims": stale_claims,
            "stale_capabilities": stale_capabilities,
            "proof_graph": proof_graph,
        }


def _claim_proof_refs(
    claim: dict[str, Any],
    research_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize durable evidence handles without guessing at truth."""
    refs: list[dict[str, Any]] = []
    evidence = claim.get("evidence") or {}
    if isinstance(evidence, dict):
        for key, kind in (
            ("execution_id", "execution"),
            ("artifact_uri", "artifact"),
            ("branch", "shadow_branch"),
            ("checkpoint", "checkpoint"),
            ("mutation_id", "mutation"),
        ):
            if evidence.get(key):
                refs.append({"kind": kind, "subject": evidence[key]})
    refs.extend(
        {
            "kind": "evidence",
            "subject": item["id"],
            "source_id": item.get("source_id"),
        }
        for item in research_evidence
    )
    return refs


def _proof_graph(*, claims, generated, invariants, invariant_results, observations=()):
    """Build a transport graph from existing proof records.

    This intentionally has no independent truth semantics. It gives callers
    stable nodes and edges for explaining *why* a claim/capability is current
    or stale while the authoritative stores continue to own each record.
    """
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []

    def node(kind: str, subject: Any, **metadata: Any) -> str:
        key = f"{kind}:{subject}"
        nodes.setdefault(key, {"id": key, "kind": kind, "subject": subject})
        if metadata:
            nodes[key].update(metadata)
        return key

    def edge(relation: str, source: str, target: str) -> None:
        value = {"relation": relation, "source": source, "target": target}
        if value not in edges:
            edges.append(value)

    for claim in claims:
        claim_node = node(
            "claim",
            claim.get("id"),
            status=claim.get("status"),
            text=claim.get("text"),
        )
        for ref in claim.get("proof_refs", []):
            ref_node = node(
                str(ref.get("kind") or "proof"),
                ref.get("subject"),
                source_id=ref.get("source_id"),
            )
            edge("SUPPORTS", ref_node, claim_node)

    for capability in generated:
        capability_node = node(
            "capability_validation",
            capability.get("id"),
            status=capability.get("status"),
            code_hash=capability.get("code_hash"),
            schema_hash=capability.get("schema_hash"),
        )
        proof = capability.get("proof") or {}
        if proof:
            proof_node = node(
                "validation_proof",
                capability.get("id"),
                all_passed=proof.get("all_passed"),
            )
            edge("VERIFIED_BY", capability_node, proof_node)
        for dependency in capability.get("evidence_dependencies", []):
            subject = (
                dependency.get("evidence_id")
                or dependency.get("source_id")
                or dependency.get("requirement")
            )
            if subject:
                dependency_node = node("evidence_dependency", subject)
                edge("DEPENDS_ON", capability_node, dependency_node)
        for predecessor in capability.get("supersedes", []):
            edge(
                "SUPERSEDES",
                capability_node,
                node("capability_validation", predecessor),
            )

    for invariant in invariants or []:
        invariant_id = invariant.get("id") or invariant.get("invariant")
        if not invariant_id:
            continue
        invariant_node = node(
            "invariant",
            invariant_id,
            description=invariant.get("description"),
        )
        for result in invariant_results or []:
            if result.get("invariant_id") != invariant_id:
                continue
            result_node = node(
                "invariant_result",
                result.get("id"),
                passed=result.get("passed"),
            )
            edge("VERIFIED_BY", invariant_node, result_node)

    for index, observation in enumerate(observations or []):
        observation_node = node(
            "observation",
            observation.get("event_id") or f"observation-{index}",
            type=observation.get("type"),
            payload=observation.get("payload"),
        )
        # The observation is a causal input to the maintenance run. It is
        # intentionally not marked as proof of the claim by itself.
        edge(
            "OBSERVED",
            observation_node,
            node("task_observation", observation.get("event_id") or index),
        )

    return {"nodes": list(nodes.values()), "edges": edges}


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


__all__ = ["TruthCapability"]
