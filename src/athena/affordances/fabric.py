"""Task/project overlays and reflection over the capability surface."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any

from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.affordances.optimizer import AffordanceOptimizer
from athena.affordances.store import GeneratedCapabilityStore
from athena.capabilities.registry import CapabilityRegistry, _compile_validator
from athena.protocol.capabilities import Availability, CapabilityDescriptor
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
        global_registry: CapabilityRegistry,
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
        self._persistence_tasks: set[asyncio.Task] = set()
        self._persistence_errors: dict[asyncio.Task, BaseException] = {}
        self._optimizer = AffordanceOptimizer()

    @staticmethod
    def _check(executor: Any) -> None:
        descriptor = getattr(executor, "descriptor", None)
        if not isinstance(descriptor, CapabilityDescriptor):
            raise TypeError("overlay executor must define a CapabilityDescriptor")
        _compile_validator(descriptor.input_schema)
        if descriptor.output_schema is not None:
            _compile_validator(descriptor.output_schema)

    def _install(self, owner_map: dict[str, Any], executor: Any) -> None:
        capability_id = executor.descriptor.id
        if capability_id in owner_map:
            raise ValueError(f"overlay capability '{capability_id}' already registered")
        owner_map[capability_id] = executor

    def register_task(self, task_id: str, executor: Any, *, generated=None) -> None:
        if not task_id:
            raise ValueError("task overlay requires task_id")
        self._check(executor)
        self._install(self._task.setdefault(task_id, {}), executor)
        self._record(executor, generated, "task", task_id)

    def register_project(self, project_id: str, executor: Any, *, generated=None) -> None:
        if not project_id:
            raise ValueError("project overlay requires project_id")
        self._check(executor)
        if generated is not None and self._equivalent(generated, "project", project_id):
            self._history.setdefault(generated.id, []).append(
                {
                    "event": "deduplicated",
                    "scope": "project",
                    "owner": project_id,
                }
            )
            return
        self._install(self._project.setdefault(project_id, {}), executor)
        self._record(executor, generated, "project", project_id)
        self._persist_if_durable(generated, owner=project_id)

    def register_user(self, user_id: str, executor: Any, *, generated=None) -> None:
        if not user_id:
            raise ValueError("user overlay requires user_id")
        self._check(executor)
        if generated is not None and self._equivalent(generated, "user", user_id):
            self._history.setdefault(generated.id, []).append(
                {
                    "event": "deduplicated",
                    "scope": "user",
                    "owner": user_id,
                }
            )
            return
        self._install(self._user.setdefault(user_id, {}), executor)
        self._record(executor, generated, "user", user_id)
        self._persist_if_durable(generated, owner=user_id)

    def _record(
        self, executor: Any, generated: GeneratedCapability | None, scope: str, owner: str
    ) -> None:
        if generated is None:
            return
        self._records[generated.id] = generated
        self._history.setdefault(generated.id, []).append(
            {
                "event": "registered",
                "scope": scope,
                "owner": owner,
                "descriptor": executor.descriptor.id,
                "lifecycle_state": generated.lifecycle_state,
            }
        )

    def _equivalent(
        self,
        generated: GeneratedCapability,
        scope: str,
        owner: str,
    ) -> bool:
        """Avoid installing two active overlays for identical machinery."""
        for existing in self._records.values():
            existing_owner = existing.project_scope if scope == "project" else existing.user_scope
            if (
                existing.scope.value == scope
                and existing_owner == owner
                and existing.lifecycle_state != "DEPRECATED"
                and existing.code_hash == generated.code_hash
                and existing.schema_hash == generated.schema_hash
                and existing.declared_effects == generated.declared_effects
                and existing.required_dependencies == generated.required_dependencies
            ):
                return True
        return False

    def _persist_if_durable(
        self,
        generated: GeneratedCapability | None,
        *,
        owner: str,
    ) -> None:
        if self._store is None or generated is None:
            return
        if generated.scope not in {AffordanceScope.PROJECT, AffordanceScope.USER}:
            return
        try:
            task = asyncio.create_task(self._store.save(generated, owner=owner))
        except RuntimeError:
            _logger.warning(
                "cannot persist generated capability %s without a running loop",
                generated.id,
            )
            return
        self._persistence_tasks.add(task)
        task.add_done_callback(self._persistence_done)

    def _persistence_done(self, task: asyncio.Task) -> None:
        self._persistence_tasks.discard(task)
        if task.cancelled():
            self._persistence_errors[task] = RuntimeError(
                "generated capability persistence task cancelled"
            )
            return
        error = task.exception()
        if error is not None:
            self._persistence_errors[task] = error
            _logger.error(
                "generated capability persistence failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def flush(self) -> None:
        """Wait for scheduled overlay persistence before shutdown."""
        if self._persistence_tasks:
            tasks = tuple(self._persistence_tasks)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self._persistence_tasks.difference_update(tasks)
            for task, result in zip(tasks, results):
                if isinstance(result, BaseException):
                    self._persistence_errors.setdefault(task, result)
        if self._persistence_errors:
            errors = tuple(self._persistence_errors.values())
            self._persistence_errors.clear()
            raise RuntimeError(
                "generated capability persistence failed: "
                + "; ".join(str(error) for error in errors)
            )

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

    async def load_persisted(
        self,
        executor_factory,
        *,
        project_id: str | None = None,
        user_id: str | None = None,
        record_validator=None,
    ) -> list[str]:
        """Rehydrate validated project/user machinery after service startup."""
        if self._store is None:
            return []
        loaded: list[str] = []
        for generated in await self._store.list(project_id=project_id, user_id=user_id):
            if generated.lifecycle_state in {
                "STALE",
                "REVALIDATION_REQUIRED",
                "DEPRECATED",
            }:
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
                        _logger.warning(
                            "skipping stale persisted capability %s",
                            generated.id,
                        )
                        continue
                executor = executor_factory(generated)
                if generated.scope.value == "project":
                    self._install(
                        self._project.setdefault(generated.project_scope or "", {}),
                        executor,
                    )
                    owner = generated.project_scope or ""
                else:
                    owner = str(
                        generated.user_scope or generated.provenance.get("owner") or user_id or ""
                    )
                    self._install(self._user.setdefault(owner, {}), executor)
                self._record(executor, generated, generated.scope.value, owner)
                loaded.append(generated.id)
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("skipping persisted capability %s: %s", generated.id, exc)
        return loaded

    async def persisted_for(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> GeneratedCapability | None:
        """Load one durable generated record with its ownership boundary."""
        if self._store is None:
            record = self._records.get(capability_id)
            if record is None:
                return None
            if record.scope is AffordanceScope.CANDIDATE and record.task_scope != task_id:
                return None
            if record.scope is AffordanceScope.PROJECT and record.project_scope != project_id:
                return None
            if record.scope is AffordanceScope.USER and record.user_scope != user_id:
                return None
            return record
        return await self._store.get(
            capability_id, task_id=task_id, project_id=project_id, user_id=user_id
        )

    async def candidates_for(self, task_id: str) -> list[GeneratedCapability]:
        """List durable generated proposals owned by one task."""
        if not task_id:
            return []
        if self._store is None:
            return [
                record
                for record in self._records.values()
                if record.scope is AffordanceScope.CANDIDATE and record.task_scope == task_id
            ]
        return await self._store.list(task_id=task_id)

    def unregister_task(self, task_id: str) -> None:
        self._task.pop(task_id, None)
        for generated_id, record in list(self._records.items()):
            if record.scope.value == "task" and record.task_scope == task_id:
                self._records.pop(generated_id, None)
                self._history.setdefault(generated_id, []).append(
                    {
                        "event": "unregistered",
                        "scope": "task",
                        "owner": task_id,
                    }
                )

    def unregister_task_capability(self, task_id: str, capability_id: str) -> None:
        """Detach one task capability after an explicit promotion."""
        overlay = self._task.get(task_id)
        if overlay is None or capability_id not in overlay:
            return
        overlay.pop(capability_id)
        self._history.setdefault(capability_id, []).append(
            {
                "event": "promoted",
                "scope": "task",
                "owner": task_id,
            }
        )
        if not overlay:
            self._task.pop(task_id, None)

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
        record = self._records.get(capability_id)
        if record is None or record.scope.value not in {"task", "project", "user"}:
            return False
        record_scope = record.scope.value
        if scope and scope != record_scope:
            return False
        if record_scope == "task":
            if record.task_scope != task_id:
                return False
            self._task.get(task_id or "", {}).pop(capability_id, None)
        elif record_scope == "project":
            if record.project_scope != project_id:
                return False
            if self._store is not None and not await self._store.disable(
                capability_id, owner=project_id
            ):
                return False
            self._project.get(project_id or "", {}).pop(capability_id, None)
        else:
            if record.user_scope != user_id:
                return False
            if self._store is not None and not await self._store.disable(
                capability_id, owner=user_id
            ):
                return False
            self._user.get(user_id or "", {}).pop(capability_id, None)
        self._records[capability_id] = replace(
            record,
            lifecycle_state="DEPRECATED",
            lifecycle_history=tuple(
                list(record.lifecycle_history)
                + [
                    {
                        "event": "deprecated",
                        "scope": record_scope,
                    }
                ]
            )[-100:],
        )
        self._history.setdefault(capability_id, []).append(
            {
                "event": "deprecated",
                "scope": record_scope,
                "owner": record.task_scope or record.project_scope or record.user_scope,
            }
        )
        return True

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
        if record is not None and record.lifecycle_state in {
            "STALE",
            "REVALIDATION_REQUIRED",
            "DEPRECATED",
        }:
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
        executors: dict[str, Any] = {}
        for executor in self.global_registry._by_id.values():  # canonical inventory
            executors[executor.descriptor.id] = executor
        if user_id:
            executors.update(self._user.get(user_id, {}))
        if project_id:
            executors.update(self._project.get(project_id, {}))
        if task_id:
            executors.update(self._task.get(task_id, {}))
        return sorted(
            [
                e.descriptor
                for e in executors.values()
                if e.descriptor.availability is Availability.AVAILABLE
            ],
            key=lambda descriptor: descriptor.id,
        )

    def has(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        try:
            return (
                self.executor_for(
                    capability_id, task_id=task_id, project_id=project_id, user_id=user_id
                ).descriptor.availability
                is Availability.AVAILABLE
            )
        except CapabilityUnavailable:
            return False

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
        return self._record_availability(
            self._records.get(capability_id),
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
        limit: int = 20,
        workspace: WorkspaceSpec | None = None,
    ) -> list[dict[str, Any]]:
        terms = {term.casefold() for term in re.findall(r"[a-zA-Z0-9_.-]+", query)}
        ranked: list[tuple[float, dict[str, Any]]] = []
        for descriptor in self.list_descriptors(
            task_id=task_id, project_id=project_id, user_id=user_id
        ):
            capability_id = descriptor.id.casefold()
            description = descriptor.description.casefold()
            if not terms:
                score = 0
            else:
                # Deterministic lexical ranking: exact capability identity
                # beats descriptive matches, while partial overlap remains
                # useful for queries such as "typescript verification".
                score = sum(
                    3 if term in capability_id else 1 if term in description else 0
                    for term in terms
                )
                if score == 0:
                    continue
            record = self._records.get(descriptor.id)
            scope = record.scope.value if record is not None else "system"
            # A scoped/generated affordance is more useful in the context in
            # which it was discovered.  Historical proof only breaks ties;
            # it never makes an unavailable or stale record searchable.
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
            dependency_available, environment_compatible = self.prerequisite_status(
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
                        "scope": scope,
                        "score": optimized_score,
                        "optimizer": optimizer_metrics,
                        "availability": descriptor.availability.value,
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

    def _record_availability(
        self,
        record: GeneratedCapability | None,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
        workspace: WorkspaceSpec | None,
    ) -> tuple[bool, bool]:
        """Resolve generated prerequisites from current local evidence.

        Search is synchronous, so this intentionally checks only facts that
        can be verified without installing anything: scoped capability
        dependencies, the workspace-local locked dependency environment, and
        the generated record's validation lifecycle. Native capabilities have
        no generated prerequisites and remain available to the optimizer.
        """
        if record is None:
            return True, True

        lifecycle = str(record.lifecycle_state or "").upper()
        validation = str(record.validation_state or "").upper()
        if lifecycle in {
            "STALE",
            "REVALIDATION_REQUIRED",
            "REJECTED",
            "DEPRECATED",
        } or validation not in {"VALIDATED", "PROMOTED"}:
            return False, False

        dependency_available = True
        for capability_id in record.required_capabilities:
            if not self.has(
                capability_id,
                task_id=task_id,
                project_id=project_id,
                user_id=user_id,
            ):
                dependency_available = False
                break

        requirements = tuple(record.required_dependencies or ())
        if requirements:
            if workspace is None:
                dependency_available = False
            else:
                try:
                    from athena.execution.dependencies import (
                        resolve_dependency_environment,
                    )

                    resolve_dependency_environment(
                        workspace.root,
                        requirements,
                        expected_fingerprint=(
                            record.dependency_lock.get("environment_fingerprint")
                            if record.dependency_lock
                            else None
                        ),
                    )
                except (OSError, TypeError, ValueError):
                    dependency_available = False

        # Dependency verification is also the environment check for generated
        # packages. For dependency-free code, lifecycle/validation is the only
        # environment evidence currently recorded at this search boundary.
        environment_compatible = dependency_available
        return dependency_available, environment_compatible

    def describe(
        self,
        capability_id: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        descriptor = self.executor_for(
            capability_id, task_id=task_id, project_id=project_id, user_id=user_id
        ).descriptor
        return {
            "id": descriptor.id,
            "description": descriptor.description,
            "input_schema": descriptor.input_schema,
            "output_schema": descriptor.output_schema,
            "effects": sorted(effect.value for effect in descriptor.effects),
            "origin": descriptor.origin.value,
            "availability": descriptor.availability.value,
        }

    def provenance(self, capability_id: str) -> dict[str, Any] | None:
        record = self._records.get(capability_id)
        return record.to_record() if record is not None else None

    def history(self, capability_id: str) -> list[dict[str, Any]]:
        return list(self._history.get(capability_id, ()))

    def dependencies(self, capability_id: str) -> list[dict[str, Any]]:
        record = self._records.get(capability_id)
        if record is None:
            return []
        return [dependency.__dict__.copy() for dependency in record.required_dependencies]

    def created_this_task(self, task_id: str) -> list[dict[str, Any]]:
        return [
            record.to_record() for record in self._records.values() if record.task_scope == task_id
        ]


__all__ = ["CapabilityFabric"]
