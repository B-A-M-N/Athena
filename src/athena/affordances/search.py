"""Read-only capability search and ranking projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from athena.affordances.discovery import (
    AUTOMATIC_DISCLOSURE,
    EXPLICIT_REFLECTION_SEARCH,
    capability_search_tokens,
    capability_synonyms,
)
from athena.affordances.models import GeneratedCapability
from athena.affordances.optimizer import AffordanceOptimizer
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.tasks import WorkspaceSpec

__all__ = ["CapabilitySearch"]


class CapabilitySearch:
    """Rank visible capabilities without owning inventory or authorization."""

    def __init__(
        self,
        *,
        records: Callable[[], Mapping[str, GeneratedCapability]],
        list_descriptors: Callable[..., list[CapabilityDescriptor]],
        prerequisite_status: Callable[..., tuple[bool, bool]],
        optimizer: AffordanceOptimizer,
    ) -> None:
        self._records = records
        self._list_descriptors = list_descriptors
        self._prerequisite_status = prerequisite_status
        self._optimizer = optimizer

    def search(
        self,
        query: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        limit: int = 12,
        workspace: WorkspaceSpec | None = None,
        mode: str = AUTOMATIC_DISCLOSURE,
    ) -> list[dict[str, Any]]:
        explicit = mode == EXPLICIT_REFLECTION_SEARCH
        terms = capability_search_tokens(query, include_weak=explicit)
        records = self._records()
        ranked: list[tuple[float, dict[str, Any]]] = []
        for descriptor in self._list_descriptors(
            task_id=task_id, project_id=project_id, user_id=user_id
        ):
            capability_id = capability_search_tokens(descriptor.id)
            descriptor_tags = capability_search_tokens(descriptor.tags)
            record = records.get(descriptor.id)
            record_name = capability_search_tokens(getattr(record, "name", ""))
            record_terms = capability_search_tokens(getattr(record, "description", ""))
            aliases = frozenset().union(
                *(capability_synonyms(part) for part in descriptor.id.casefold().split("."))
            )
            description = capability_search_tokens(descriptor.description, include_weak=explicit)
            identity_hits = terms & (capability_id | record_name)
            tag_hits = terms & descriptor_tags
            alias_hits = terms & aliases
            description_hits = terms & (description | record_terms)
            # Descriptor prose is a weak signal under automatic disclosure:
            # it can supplement a strong identity/tag/synonym hit, or stand
            # alone only when two meaningful words agree.
            strong_hits = identity_hits | tag_hits | alias_hits
            if explicit:
                if not (strong_hits or description_hits):
                    continue
            elif not strong_hits and len(description_hits) < 2:
                continue
            partial_hits = {
                term
                for term in terms
                if len(term) >= 6
                and any(
                    candidate != term and len(candidate) >= 6 and candidate.startswith(term)
                    for candidate in capability_id | descriptor_tags | aliases | description
                )
            }
            score = (
                len(identity_hits) * 12
                + len(tag_hits) * 10
                + len(alias_hits) * 8
                + len(description_hits) * 2
                + len(partial_hits)
            )
            if score <= 0:
                continue
            scope = record.scope.value if record is not None else "system"
            scope_bonus = {
                "task": 1.0,
                "project": 0.75,
                "user": 0.5,
                "system": 0.0,
            }.get(scope, 0.0)
            proof_bonus = 0.0
            if record is not None:
                proof_bonus = min(1.0, max(0.0, record.quality_score))
                proof_bonus += min(1.0, record.success_count / 10.0)
            dependency_available, environment_compatible = self._prerequisite_status(
                descriptor.id,
                task_id=task_id,
                project_id=project_id,
                user_id=user_id,
                workspace=workspace,
            )
            optimized_score, optimizer_metrics = self._optimizer.score(
                lexical=score + scope_bonus + proof_bonus,
                descriptor=descriptor,
                record=record,
                dependency_available=dependency_available,
                environment_compatible=environment_compatible,
            )
            ranked.append(
                (
                    optimized_score,
                    {
                        "id": descriptor.id,
                        "description": descriptor.description,
                        "origin": descriptor.origin.value,
                        "effects": sorted(effect.value for effect in descriptor.effects),
                        "output_schema": dict(descriptor.output_schema or {}),
                        "scope": scope,
                        "score": optimized_score,
                        "optimizer": optimizer_metrics,
                        "availability": descriptor.availability.value,
                        "tags": sorted(descriptor.tags),
                        "proof": dict(record.proof_record) if record is not None else {},
                        "validation_state": (record.validation_state if record is not None else ""),
                        **(
                            {"lifecycle_state": record.lifecycle_state}
                            if record is not None
                            else {}
                        ),
                    },
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
        return [item for _, item in ranked[: max(limit, 0)]]
