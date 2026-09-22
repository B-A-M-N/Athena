"""Durable generated-record queries and startup rehydration.

Subordinate to :class:`athena.affordances.fabric.CapabilityFabric`. This module
owns storage-backed lookup/listing and validated executor rehydration; it never
changes lifecycle state, authorizes invocation, or installs unvalidated code.
"""

from __future__ import annotations

import logging

from athena.affordances.models import AffordanceScope, GeneratedCapability

_logger = logging.getLogger("athena.affordances")

UNUSABLE_LIFECYCLE_STATES = frozenset(
    {
        "STALE",
        "DEGRADED",
        "REVALIDATION_REQUIRED",
        "REJECTED",
        "SUPERSEDED",
        "DEPRECATED",
    }
)

__all__ = ["GeneratedRecordStore", "UNUSABLE_LIFECYCLE_STATES"]


class GeneratedRecordStore:
    """Storage-backed generated-record queries and startup rehydration."""

    def __init__(self, fabric) -> None:
        self._fabric = fabric

    async def persisted_for(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> GeneratedCapability | None:
        """Load one durable generated record with its ownership boundary."""
        store = self._fabric._store
        if store is None:
            record = self._fabric._records.get(capability_id)
            if record is None:
                return None
            if record.scope is AffordanceScope.CANDIDATE and record.task_scope != task_id:
                return None
            if record.scope is AffordanceScope.PROJECT and record.project_scope != project_id:
                return None
            if record.scope is AffordanceScope.USER and record.user_scope != user_id:
                return None
            return record
        return await store.get(
            capability_id, task_id=task_id, project_id=project_id, user_id=user_id
        )

    async def candidates_for(self, task_id: str) -> list[GeneratedCapability]:
        """List durable generated proposals owned by one task."""
        store = self._fabric._store
        if not task_id:
            return []
        if store is None:
            return [
                record
                for record in self._fabric._records.values()
                if record.scope is AffordanceScope.CANDIDATE and record.task_scope == task_id
            ]
        return await store.list(task_id=task_id)

    async def load_persisted(
        self,
        executor_factory,
        *,
        project_id: str | None = None,
        user_id: str | None = None,
        record_validator=None,
    ) -> list[str]:
        """Rehydrate validated project/user machinery after service startup."""
        store = self._fabric._store
        if store is None:
            return []
        loaded: list[str] = []
        for generated in await store.list(project_id=project_id, user_id=user_id):
            if generated.lifecycle_state in UNUSABLE_LIFECYCLE_STATES:
                _logger.info(
                    "skipping unavailable generated capability %s (%s)",
                    generated.id,
                    generated.lifecycle_state,
                )
                continue
            if generated.validation_state not in {"VALIDATED", "PROMOTED"}:
                _logger.warning("skipping unvalidated persisted capability %s", generated.id)
                continue
            if not generated.proof_record.get("all_passed", False):
                _logger.warning("skipping persisted capability without proof %s", generated.id)
                continue
            try:
                if record_validator is not None:
                    verdict = record_validator(generated)
                    if hasattr(verdict, "__await__"):
                        verdict = await verdict
                    if not verdict:
                        _logger.warning("skipping stale persisted capability %s", generated.id)
                        continue
                executor = executor_factory(generated)
                self._fabric.install_persisted_executor(
                    generated,
                    executor,
                    user_id=user_id,
                )
                loaded.append(generated.id)
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("skipping persisted capability %s: %s", generated.id, exc)
        return loaded
