"""Task/project overlays and reflection over the capability surface."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.affordances.discovery import AUTOMATIC_DISCLOSURE
from athena.affordances.optimizer import AffordanceOptimizer
from athena.affordances.overlay_lifecycle import GeneratedOverlayLifecycle
from athena.affordances.persistence import DurableActivationScheduler
from athena.affordances.reflection_ports import FabricReflectionPorts
from athena.affordances.records import GeneratedRecordStore, UNUSABLE_LIFECYCLE_STATES
from athena.affordances.search import CapabilitySearch
from athena.affordances.store import GeneratedCapabilityStore
from athena.protocol.capabilities import (
    Availability,
    CapabilityDescriptor,
    CapabilityInventory,
)
from athena.protocol.errors import CapabilityUnavailable
from athena.protocol.tasks import WorkspaceSpec

_logger = logging.getLogger("athena.affordances")


class CapabilityFabric:
    """Effective capability surface: task overlay over project over global.

    Overlay registration validates descriptors immediately but does not put
    them in the global registry.  Task overlays are removed by the service at
    task finalization, preventing generated machinery from leaking across
    principals or tasks.
    """

    def __init__(
        self,
        global_registry: CapabilityInventory,
        *,
        store: GeneratedCapabilityStore | None = None,
    ) -> None:
        self.global_registry = global_registry
        self._store = store
        self._task: dict[str, dict[str, Any]] = {}
        self._project: dict[str, dict[str, Any]] = {}
        self._user: dict[str, dict[str, Any]] = {}
        self._records: dict[str, GeneratedCapability] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._scheduler = DurableActivationScheduler(self)
        self._lifecycle = GeneratedOverlayLifecycle(
            task=self._task,
            project=self._project,
            user=self._user,
            records=self._records,
            history=self._history,
            store=self._store,
            scheduler=self._scheduler,
            invalidate=self._invalidate_availability,
        )
        self._record_store = GeneratedRecordStore(self)
        self._optimizer = AffordanceOptimizer()
        self._reflection = FabricReflectionPorts(self)
        self._search = CapabilitySearch(
            records=lambda: self._records,
            list_descriptors=self.list_descriptors,
            prerequisite_status=self.prerequisite_status,
            optimizer=self._optimizer,
        )
        self._availability_cache: dict[tuple[Any, ...], tuple[bool, bool]] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        """Monotonic revision of effective overlays and generated evidence."""
        return self._generation

    def _invalidate_availability(self) -> None:
        self._availability_cache.clear()
        self._generation += 1

    def register_task(self, task_id: str, executor: Any, *, generated=None) -> None:
        self._lifecycle.register_task(task_id, executor, generated=generated)

    def register_project(self, project_id: str, executor: Any, *, generated=None) -> None:
        self._lifecycle.register_project(project_id, executor, generated=generated)

    def register_user(self, user_id: str, executor: Any, *, generated=None) -> None:
        self._lifecycle.register_user(user_id, executor, generated=generated)

    async def activate_generated_revision(
        self,
        generated: GeneratedCapability,
        executor: Any,
        *,
        owner: str,
    ) -> None:
        return await self._lifecycle.activate_generated_revision(generated, executor, owner=owner)

    async def activate_revalidated(
        self,
        generated: GeneratedCapability,
        executor: Any,
        *,
        owner: str,
    ) -> None:
        return await self._lifecycle.activate_revalidated(generated, executor, owner=owner)

    async def persist_and_activate(
        self, generated: GeneratedCapability, executor: Any, *, owner: str
    ) -> None:
        return await self._lifecycle.persist_and_activate(generated, executor, owner=owner)

    async def flush(self) -> None:
        """Wait for scheduled overlay persistence before shutdown."""
        await self._scheduler.flush()

    async def update_generated_proof(
        self,
        capability_id: str,
        proof_record: dict[str, Any],
    ) -> None:
        """Persist execution proof for a durable generated capability.

        Task-local proof is kept in memory until the capability becomes a
        durable candidate. Once that candidate exists, event-derived proof
        updates must reach its store record too, or promotion after a later
        restart would lose the verification evidence.
        """
        record = self._records.get(capability_id)
        if record is None or record.scope not in {
            AffordanceScope.TASK,
            AffordanceScope.CANDIDATE,
            AffordanceScope.PROJECT,
            AffordanceScope.USER,
        }:
            return
        updated = replace(record, proof_record=dict(proof_record))
        usage = dict(proof_record.get("usage") or {})
        updated = replace(
            updated,
            lifecycle_state=str(proof_record.get("lifecycle_state") or updated.lifecycle_state),
            quality_score=float(proof_record.get("quality_score") or updated.quality_score),
            use_count=int(usage.get("uses", updated.use_count)),
            success_count=int(usage.get("successes", updated.success_count)),
            failure_count=int(usage.get("failures", updated.failure_count)),
            last_used_at=proof_record.get("last_used_at") or updated.last_used_at,
            lifecycle_history=tuple(
                list(updated.lifecycle_history)
                + [
                    {
                        "event": "proof_updated",
                        "usage": usage,
                        "quality_score": proof_record.get("quality_score"),
                    }
                ]
            )[-100:],
        )
        self._records[capability_id] = updated
        # Proof quality and usage affect discovery/strategy scores, so they
        # are part of the same revision boundary as overlay registration.
        self._invalidate_availability()
        owner = (
            updated.project_scope
            if updated.scope is AffordanceScope.PROJECT
            else updated.user_scope
            if updated.scope is AffordanceScope.USER
            else updated.task_scope
        ) or str(updated.provenance.get("owner") or "")
        if self._store is None or not owner:
            return
        if updated.scope is AffordanceScope.TASK:
            # Ordinary task-local calls are not durable. Only continue when
            # the lifecycle has already retained a candidate with this id.
            candidate = await self._store.get(capability_id, task_id=owner)
            if candidate is None:
                return
        await self._store.update_proof(capability_id, dict(proof_record))
        self._history.setdefault(capability_id, []).append(
            {
                "event": "proof_updated",
                "scope": updated.scope.value,
                "owner": owner,
                "usage": dict(proof_record.get("usage") or {}),
            }
        )

    async def persist_generated_candidate(
        self,
        generated: GeneratedCapability,
    ) -> None:
        """Retain a proven task capability as a reviewable candidate.

        Candidates are durable records, not active overlays.  They therefore
        do not enter the task/project/user executor maps and cannot become
        callable merely because a task used them repeatedly.  Promotion still
        requires the explicit synthesis operation and a fresh target-scope
        validation pass.
        """
        if self._store is None or generated.scope is not AffordanceScope.CANDIDATE:
            return
        owner = generated.task_scope or str(generated.provenance.get("task_id") or "")
        if not owner:
            raise RuntimeError(f"candidate {generated.id} has no owning task")
        history = list(generated.lifecycle_history)
        history.append(
            {
                "event": "candidate_created",
                "owner": owner,
                "quality_score": generated.quality_score,
                "use_count": generated.use_count,
            }
        )
        await self._store.save(
            replace(generated, lifecycle_history=tuple(history[-100:])),
            owner=owner,
        )
        self._history.setdefault(generated.id, []).append(
            {
                "event": "candidate_created",
                "scope": "candidate",
                "owner": owner,
            }
        )

    def install_persisted_executor(self, generated, executor, *, user_id=None) -> None:
        """Install one already-validated persisted executor at its scope."""
        self._lifecycle.install_persisted_executor(generated, executor, user_id=user_id)

    async def load_persisted(
        self,
        executor_factory,
        *,
        project_id: str | None = None,
        user_id: str | None = None,
        record_validator=None,
    ) -> list[str]:
        return await self._record_store.load_persisted(
            executor_factory,
            project_id=project_id,
            user_id=user_id,
            record_validator=record_validator,
        )

    async def persisted_for(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> GeneratedCapability | None:
        return await self._record_store.persisted_for(
            capability_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        )

    async def candidates_for(self, task_id: str) -> list[GeneratedCapability]:
        return await self._record_store.candidates_for(task_id)

    def unregister_task(self, task_id: str) -> None:
        self._lifecycle.unregister_task(task_id)

    def unregister_task_capability(self, task_id: str, capability_id: str) -> None:
        """Detach one task capability after an explicit promotion."""
        self._lifecycle.unregister_task_capability(task_id, capability_id)

    async def deprecate(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        scope: str | None = None,
    ) -> bool:
        """Retire one generated overlay while keeping its provenance record."""
        return await self._lifecycle.deprecate(
            capability_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            scope=scope,
        )

    def executor_for(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> Any:
        if task_id and capability_id in self._task.get(task_id, {}):
            executor = self._task[task_id][capability_id]
        elif project_id and capability_id in self._project.get(project_id, {}):
            executor = self._project[project_id][capability_id]
        elif user_id and capability_id in self._user.get(user_id, {}):
            executor = self._user[user_id][capability_id]
        else:
            executor = self.global_registry.executor_for(capability_id)
        record = self._records.get(capability_id)
        if record is not None and record.lifecycle_state in UNUSABLE_LIFECYCLE_STATES:
            raise CapabilityUnavailable(
                f"capability '{capability_id}' requires revalidation "
                f"({record.lifecycle_state.lower()})"
            )
        if executor.descriptor.availability is not Availability.AVAILABLE:
            raise CapabilityUnavailable(
                f"capability '{capability_id}' is {executor.descriptor.availability.value}"
            )
        return executor

    def list_descriptors(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> list[CapabilityDescriptor]:
        return self._reflection.list_descriptors(
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        )

    def has(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        return self._reflection.has(
            capability_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        )

    def prerequisite_status(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        workspace: WorkspaceSpec | None = None,
    ) -> tuple[bool, bool]:
        """Return current dependency and environment readiness for discovery.

        This is advisory only. Invocation still resolves the executor and
        applies policy through the dispatcher. Keeping the check here means
        reflection and ranking use the same generated-capability evidence
        instead of each inventing its own availability rules.
        """
        return self._reflection.prerequisite_status(
            capability_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            workspace=workspace,
        )

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
        return self._search.search(
            query,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            limit=limit,
            workspace=workspace,
            mode=mode,
        )

    def describe(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        return self._reflection.describe(
            capability_id,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
        )

    def provenance(self, capability_id: str) -> dict[str, Any] | None:
        return self._reflection.provenance(capability_id)

    def history(self, capability_id: str) -> list[dict[str, Any]]:
        return self._reflection.history(capability_id)

    def dependencies(self, capability_id: str) -> list[dict[str, Any]]:
        return self._reflection.dependencies(capability_id)

    def created_this_task(self, task_id: str) -> list[dict[str, Any]]:
        return self._reflection.created_this_task(task_id)


__all__ = ["CapabilityFabric"]
