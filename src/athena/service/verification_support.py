"""Bounded evidence projection for acceptance and judge verification."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from athena.execution.environment import VerificationEnvironment
from athena.kernel.verifiers import CompositeVerifier
from athena.self_host.gates import SelfHostGateBundle

_logger = logging.getLogger("athena.service.verification_support")

__all__ = ["VerificationSupport"]


class VerificationSupport:
    """Collect canonical task evidence without owning verification decisions."""

    def __init__(
        self,
        *,
        events: Any,
        executions: Any,
        mutations: Any,
        research: Any,
        get_result: Callable[[str], Awaitable[Any]],
        world_state: Callable[[str], Any],
    ) -> None:
        self._events = events
        self._executions = executions
        self._mutations = mutations
        self._research = research
        self._get_result = get_result
        self._world_state = world_state

    def build_verifier(
        self,
        *,
        execution: Any,
        dispatcher: Any,
        artifact_store: Any,
        capability_registry: Any,
        model_registry: Any,
        evidence_provider: Any = None,
        inference_broker: Any = None,
    ) -> Any:
        """Build the canonical composite verifier without owning decisions."""
        return CompositeVerifier(
            execution=execution,
            dispatcher=dispatcher,
            artifact_store=artifact_store,
            capability_registry=capability_registry,
            model_registry=model_registry,
            evidence_provider=evidence_provider,
            inference_broker=inference_broker,
            verification_environment_resolver=self.resolve_verification_environment,
        )

    def resolve_verification_environment(self, task: Any) -> VerificationEnvironment | None:
        """Validate persisted self-host proof authority against the current checkout."""
        metadata = task.metadata or {}
        if not bool(metadata.get("_athena_self_host")):
            return None
        record = metadata.get("_athena_verification_environment")
        bundle_record = metadata.get("_athena_gate_bundle")
        if not isinstance(record, Mapping) or not isinstance(bundle_record, Mapping):
            raise ValueError("self-host proof is missing its trusted authority bundle")
        project_root = str(record.get("project_root") or "")
        bundle_root = str(bundle_record.get("project_root") or "")
        if not project_root or project_root != bundle_root:
            raise ValueError("self-host proof authority roots do not match")
        current_bundle = SelfHostGateBundle.capture(project_root, allow_dirty=True)
        if current_bundle.gate_bundle_hash != str(bundle_record.get("gate_bundle_hash") or ""):
            raise ValueError("self-host proof gate bundle is stale")
        expected = VerificationEnvironment.from_project(
            project_root, include_project_root=True, include_rust=True, task_id=task.id
        )
        return VerificationEnvironment.from_record(record, expected=expected)

    async def evidence_for(self, task: Any) -> dict[str, Any]:
        evidence: dict[str, Any] = {"objective": task.objective, "task_id": task.id}
        events_store = self._events() if callable(self._events) else self._events
        if events_store is not None:
            events = await events_store.list_for_task(task.id)
            evidence["events"] = [
                {
                    "sequence": event.sequence,
                    "type": event.type,
                    "payload": dict(event.payload or {}),
                }
                for event in events[-100:]
            ]
        execution_store = self._executions() if callable(self._executions) else self._executions
        if execution_store is not None:
            evidence["executions"] = [
                dict(row) for row in (await execution_store.list_for_task(task.id))[-25:]
            ]
        mutation_store = self._mutations() if callable(self._mutations) else self._mutations
        if mutation_store is not None:
            evidence["mutations"] = [
                dict(row) for row in (await mutation_store.list_for_task(task.id))[-25:]
            ]
        result = await self._get_result(task.id)
        if result is not None:
            evidence["result"] = {
                "status": result.status.value,
                "summary": result.summary,
                "unresolved": list(result.unresolved),
                "artifacts": [getattr(ref, "uri", str(ref)) for ref in result.artifacts],
            }
        result_data = evidence.get("result", {})
        research_store = self._research() if callable(self._research) else self._research
        if research_store is not None:
            try:
                workspace_id = task.workspace.id if task.workspace else None
                evidence["research"] = {
                    "sources": [
                        source.to_record()
                        for source in await research_store.list_sources(
                            task_id=task.id, project_id=workspace_id, limit=50
                        )
                    ],
                    "evidence": [
                        item.to_record()
                        for item in await research_store.list_evidence(
                            task_id=task.id, project_id=workspace_id, limit=75
                        )
                    ],
                    "gaps": [
                        gap.to_record()
                        for gap in await research_store.list_gaps(task_id=task.id, limit=100)
                    ],
                }
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("research evidence lookup failed: %s", exc)
        try:
            evidence["world_state"] = await self._world_state(task.id).snapshot(
                workspace_root=task.workspace.root if task.workspace else None
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("world-state evidence lookup failed: %s", exc)
        return {
            "evidence": evidence,
            "world_state": evidence.get("world_state", {}),
            "artifacts": result_data.get("artifacts", []),
            "unresolved_failures": result_data.get("unresolved", []),
        }
