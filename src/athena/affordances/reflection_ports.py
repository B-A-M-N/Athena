"""Read-only inventory projections used by :class:`CapabilityFabric`."""

from __future__ import annotations

from typing import Any

from athena.affordances.discovery import dependency_state_fingerprint
from athena.affordances.models import GeneratedCapability
from athena.affordances.records import UNUSABLE_LIFECYCLE_STATES
from athena.execution.dependencies import resolve_dependency_environment
from athena.protocol.capabilities import Availability, CapabilityDescriptor
from athena.protocol.errors import CapabilityUnavailable
from athena.protocol.tasks import WorkspaceSpec

__all__ = ["FabricReflectionPorts"]


class FabricReflectionPorts:
    """Calculate visibility and prerequisite evidence without invoking tools."""

    def __init__(self, fabric: Any) -> None:
        self._fabric = fabric

    def list_descriptors(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> list[CapabilityDescriptor]:
        fabric = self._fabric
        executors: dict[str, Any] = {}
        for executor in fabric.global_registry.iter_executors():
            executors[executor.descriptor.id] = executor
        if user_id:
            executors.update(fabric._user.get(user_id, {}))
        if project_id:
            executors.update(fabric._project.get(project_id, {}))
        if task_id:
            executors.update(fabric._task.get(task_id, {}))
        return sorted(
            [
                executor.descriptor
                for executor in executors.values()
                if executor.descriptor.availability is Availability.AVAILABLE
                and (
                    fabric._records.get(executor.descriptor.id) is None
                    or fabric._records[executor.descriptor.id].lifecycle_state
                    not in UNUSABLE_LIFECYCLE_STATES
                )
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
                self._fabric.executor_for(
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
        fabric = self._fabric
        record = fabric._records.get(capability_id)
        key = (
            fabric._generation,
            getattr(fabric.global_registry, "generation", 0),
            capability_id,
            task_id,
            project_id,
            user_id,
            getattr(workspace, "root", None),
            str(record.lifecycle_state) if record is not None else "",
            str(record.validation_state) if record is not None else "",
            str(record.code_hash) if record is not None else "",
            str(record.schema_hash) if record is not None else "",
            tuple(sorted(str(effect) for effect in record.declared_effects))
            if record is not None
            else (),
            tuple(sorted(record.required_capabilities)) if record is not None else (),
            tuple(d.key() for d in (record.required_dependencies or ())) if record else (),
            str(record.dependency_lock.get("environment_fingerprint"))
            if record is not None
            else "",
            dependency_state_fingerprint(workspace, record),
        )
        cached = fabric._availability_cache.get(key)
        if cached is not None:
            return cached
        result = self._record_availability(
            record,
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            workspace=workspace,
        )
        if len(fabric._availability_cache) >= 2048:
            fabric._availability_cache.clear()
        fabric._availability_cache[key] = result
        return result

    def _record_availability(
        self,
        record: GeneratedCapability | None,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
        workspace: WorkspaceSpec | None,
    ) -> tuple[bool, bool]:
        if record is None:
            return True, True
        lifecycle = str(record.lifecycle_state or "").upper()
        validation = str(record.validation_state or "").upper()
        if lifecycle in UNUSABLE_LIFECYCLE_STATES or validation not in {"VALIDATED", "PROMOTED"}:
            return False, False
        dependency_available = all(
            self.has(
                capability_id,
                task_id=task_id,
                project_id=project_id,
                user_id=user_id,
            )
            for capability_id in record.required_capabilities
        )
        requirements = tuple(record.required_dependencies or ())
        if requirements:
            if workspace is None:
                dependency_available = False
            else:
                try:
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
        return dependency_available, dependency_available

    def describe(self, capability_id: str, **scope: str | None) -> dict[str, Any]:
        descriptor = self._fabric.executor_for(capability_id, **scope).descriptor
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
        record = self._fabric._records.get(capability_id)
        return record.to_record() if record is not None else None

    def history(self, capability_id: str) -> list[dict[str, Any]]:
        return list(self._fabric._history.get(capability_id, ()))

    def dependencies(self, capability_id: str) -> list[dict[str, Any]]:
        record = self._fabric._records.get(capability_id)
        if record is None:
            return []
        return [dependency.__dict__.copy() for dependency in record.required_dependencies]

    def created_this_task(self, task_id: str) -> list[dict[str, Any]]:
        return [
            record.to_record()
            for record in self._fabric._records.values()
            if record.task_scope == task_id
        ]
