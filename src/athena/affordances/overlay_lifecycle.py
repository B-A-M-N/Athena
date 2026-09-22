"""Generated-capability overlay registration and lifecycle transitions."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.affordances.persistence import DurableActivationScheduler
from athena.affordances.records import UNUSABLE_LIFECYCLE_STATES
from athena.affordances.store import GeneratedCapabilityStore
from athena.protocol.messages import utcnow
from athena.protocol.capabilities import CapabilityDescriptor
from athena.schema import compile_validator


class GeneratedOverlayLifecycle:
    """Own mutable overlay maps and generated-record lifecycle transitions."""

    def __init__(
        self,
        *,
        task: dict[str, dict[str, Any]],
        project: dict[str, dict[str, Any]],
        user: dict[str, dict[str, Any]],
        records: dict[str, GeneratedCapability],
        history: dict[str, list[dict[str, Any]]],
        store: GeneratedCapabilityStore | None,
        scheduler: DurableActivationScheduler,
        invalidate: Callable[[], None],
    ) -> None:
        self._task = task
        self._project = project
        self._user = user
        self._records = records
        self._history = history
        self._store = store
        self._scheduler = scheduler
        self._invalidate = invalidate

    @staticmethod
    def _check(executor: Any) -> None:
        descriptor = getattr(executor, "descriptor", None)
        if not isinstance(descriptor, CapabilityDescriptor):
            raise TypeError("overlay executor must define a CapabilityDescriptor")
        compile_validator(descriptor.input_schema)
        if descriptor.output_schema is not None:
            compile_validator(descriptor.output_schema)

    @staticmethod
    def _install(owner_map: dict[str, Any], executor: Any) -> None:
        capability_id = executor.descriptor.id
        if capability_id in owner_map:
            raise ValueError(f"overlay capability '{capability_id}' already registered")
        owner_map[capability_id] = executor

    def register_task(self, task_id: str, executor: Any, *, generated=None) -> None:
        if not task_id:
            raise ValueError("task overlay requires task_id")
        self._check(executor)
        self._validate_revision_links(generated, "task", task_id)
        self._install(self._task.setdefault(task_id, {}), executor)
        self._invalidate()
        self._record(executor, generated, "task", task_id)
        self._supersede_active(generated, "task", task_id)

    def register_project(self, project_id: str, executor: Any, *, generated=None) -> None:
        if not project_id:
            raise ValueError("project overlay requires project_id")
        self._check(executor)
        if generated is not None and self._equivalent(generated, "project", project_id):
            self._history.setdefault(generated.id, []).append(
                {"event": "deduplicated", "scope": "project", "owner": project_id}
            )
            return
        self._validate_revision_links(generated, "project", project_id)
        if self._store is not None and generated is not None:
            self._scheduler.schedule(generated, executor, owner=project_id)
            return
        self._install(self._project.setdefault(project_id, {}), executor)
        self._invalidate()
        self._record(executor, generated, "project", project_id)
        self._supersede_active(generated, "project", project_id)

    def register_user(self, user_id: str, executor: Any, *, generated=None) -> None:
        if not user_id:
            raise ValueError("user overlay requires user_id")
        self._check(executor)
        if generated is not None and self._equivalent(generated, "user", user_id):
            self._history.setdefault(generated.id, []).append(
                {"event": "deduplicated", "scope": "user", "owner": user_id}
            )
            return
        self._validate_revision_links(generated, "user", user_id)
        if self._store is not None and generated is not None:
            self._scheduler.schedule(generated, executor, owner=user_id)
            return
        self._install(self._user.setdefault(user_id, {}), executor)
        self._invalidate()
        self._record(executor, generated, "user", user_id)
        self._supersede_active(generated, "user", user_id)

    async def activate_generated_revision(
        self,
        generated: GeneratedCapability,
        executor: Any,
        *,
        owner: str,
    ) -> None:
        """Persist and activate a generated project/user revision."""
        self._check(executor)
        if generated.scope not in {AffordanceScope.PROJECT, AffordanceScope.USER}:
            raise ValueError("generated revision activation requires project or user scope")
        if not owner:
            raise ValueError("generated revision activation requires an owner")
        scope = generated.scope.value
        if self._equivalent(generated, scope, owner):
            self._history.setdefault(generated.id, []).append(
                {"event": "deduplicated", "scope": scope, "owner": owner}
            )
            return
        self._validate_revision_links(generated, scope, owner)
        owner_map = (
            self._project.setdefault(owner, {})
            if generated.scope is AffordanceScope.PROJECT
            else self._user.setdefault(owner, {})
        )
        if generated.id in owner_map:
            raise ValueError(f"overlay capability '{generated.id}' already registered")
        if self._store is not None:
            await self._store.save(generated, owner=owner)
        self._install(owner_map, executor)
        self._invalidate()
        self._record(executor, generated, scope, owner)
        self._supersede_active(generated, scope, owner)

    async def activate_revalidated(
        self,
        generated: GeneratedCapability,
        executor: Any,
        *,
        owner: str,
    ) -> None:
        """Persist a revalidated record before restoring its live overlay."""
        self._check(executor)
        scope = generated.scope
        if scope not in {
            AffordanceScope.TASK,
            AffordanceScope.CANDIDATE,
            AffordanceScope.PROJECT,
            AffordanceScope.USER,
        }:
            raise ValueError("only task/candidate/project/user capabilities can be reactivated")
        if not owner:
            raise ValueError("revalidated capability requires an owner")
        if scope is AffordanceScope.CANDIDATE:
            if self._store is not None:
                await self._store.save(generated, owner=owner)
            self._records[generated.id] = generated
            self._history.setdefault(generated.id, []).append(
                {
                    "event": "revalidated",
                    "scope": scope.value,
                    "owner": owner,
                    "revision": generated.revision,
                }
            )
            self._invalidate()
            return
        owner_map = (
            self._task.setdefault(owner, {})
            if scope is AffordanceScope.TASK
            else self._project.setdefault(owner, {})
            if scope is AffordanceScope.PROJECT
            else self._user.setdefault(owner, {})
        )
        if self._store is not None and scope in {
            AffordanceScope.PROJECT,
            AffordanceScope.USER,
        }:
            await self._store.save(generated, owner=owner)
        owner_map[generated.id] = executor
        self._records[generated.id] = generated
        self._history.setdefault(generated.id, []).append(
            {
                "event": "revalidated",
                "scope": scope.value,
                "owner": owner,
                "revision": generated.revision,
            }
        )
        self._invalidate()

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

    def _validate_revision_links(
        self,
        generated: GeneratedCapability | None,
        scope: str,
        owner: str,
    ) -> None:
        if generated is None:
            return
        for predecessor_id in generated.supersedes:
            predecessor = self._records.get(predecessor_id)
            if predecessor is None:
                continue
            predecessor_owner = (
                predecessor.project_scope
                if scope == "project"
                else predecessor.user_scope
                if scope == "user"
                else predecessor.task_scope
            )
            if predecessor.scope.value != scope or predecessor_owner != owner:
                continue
            if (
                predecessor.family_id
                and generated.family_id
                and predecessor.family_id != generated.family_id
            ):
                raise ValueError(
                    f"generated capability {generated.id} cannot supersede a different family"
                )
            if generated.parent_revision != predecessor.revision:
                raise ValueError(
                    f"generated capability {generated.id} must name predecessor revision "
                    f"{predecessor.revision}"
                )
            if generated.revision <= predecessor.revision:
                raise ValueError(
                    f"generated capability {generated.id} must advance predecessor revision "
                    f"{predecessor.revision}"
                )

    def _supersede_active(
        self,
        generated: GeneratedCapability | None,
        scope: str,
        owner: str,
    ) -> None:
        if generated is None:
            return
        owner_map = (
            self._project.get(owner, {})
            if scope == "project"
            else self._user.get(owner, {})
            if scope == "user"
            else self._task.get(owner, {})
        )
        for predecessor_id in generated.supersedes:
            predecessor = self._records.get(predecessor_id)
            if predecessor is None:
                continue
            predecessor_owner = (
                predecessor.project_scope
                if scope == "project"
                else predecessor.user_scope
                if scope == "user"
                else predecessor.task_scope
            )
            if predecessor.scope.value != scope or predecessor_owner != owner:
                continue
            owner_map.pop(predecessor_id, None)
            history = list(predecessor.lifecycle_history)
            history.append(
                {
                    "event": "lifecycle_transition",
                    "from": predecessor.lifecycle_state,
                    "to": "SUPERSEDED",
                    "replacement": generated.id,
                    "at": utcnow().isoformat(),
                }
            )
            self._records[predecessor_id] = replace(
                predecessor,
                lifecycle_state="SUPERSEDED",
                active_revision=generated.revision,
                superseded_by=generated.id,
                lifecycle_history=tuple(history[-100:]),
            )
            self._history.setdefault(predecessor_id, []).append(
                {
                    "event": "superseded",
                    "scope": scope,
                    "owner": owner,
                    "replacement": generated.id,
                }
            )
        self._invalidate()

    def _equivalent(self, generated: GeneratedCapability, scope: str, owner: str) -> bool:
        """Avoid installing two active overlays for identical machinery."""
        for existing in self._records.values():
            existing_owner = existing.project_scope if scope == "project" else existing.user_scope
            if (
                existing.scope.value == scope
                and existing_owner == owner
                and existing.lifecycle_state not in UNUSABLE_LIFECYCLE_STATES
                and existing.code_hash == generated.code_hash
                and existing.schema_hash == generated.schema_hash
                and existing.declared_effects == generated.declared_effects
                and existing.required_dependencies == generated.required_dependencies
            ):
                return True
        return False

    async def persist_and_activate(
        self,
        generated: GeneratedCapability,
        executor: Any,
        *,
        owner: str,
    ) -> None:
        """Commit durable state before exposing a new live overlay."""
        if self._store is None:
            raise RuntimeError("durable activation requires a generated capability store")
        await self.activate_generated_revision(generated, executor, owner=owner)

    def install_persisted_executor(self, generated, executor, *, user_id=None) -> None:
        """Install one already-validated persisted executor at its scope."""
        if generated.scope.value == "project":
            self._install(self._project.setdefault(generated.project_scope or "", {}), executor)
            owner = generated.project_scope or ""
        else:
            owner = str(generated.user_scope or generated.provenance.get("owner") or user_id or "")
            self._install(self._user.setdefault(owner, {}), executor)
        self._record(executor, generated, generated.scope.value, owner)

    def unregister_task(self, task_id: str) -> None:
        self._invalidate()
        self._task.pop(task_id, None)
        for generated_id, record in list(self._records.items()):
            if record.scope.value == "task" and record.task_scope == task_id:
                self._records.pop(generated_id, None)
                self._history.setdefault(generated_id, []).append(
                    {"event": "unregistered", "scope": "task", "owner": task_id}
                )

    def unregister_task_capability(self, task_id: str, capability_id: str) -> None:
        """Detach one task capability after an explicit promotion."""
        overlay = self._task.get(task_id)
        if overlay is None or capability_id not in overlay:
            return
        self._invalidate()
        overlay.pop(capability_id)
        self._history.setdefault(capability_id, []).append(
            {"event": "promoted", "scope": "task", "owner": task_id}
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
        self._invalidate()
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
                list(record.lifecycle_history) + [{"event": "deprecated", "scope": record_scope}]
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
