"""Operator projection mechanism for the service façade (P1-10).

Stable read/projection views over canonical durable state, moved verbatim
from ``athena.service.service``. This is a subordinate mechanism, not a
second authority: policy, stores, the compiler, artifacts, and capability
dispatch all resolve through the owning :class:`AthenaService` instance
(``self._svc``). These views never mutate state and never become a second
execution path; the CLI renders them verbatim.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from athena.protocol.events import make_event
from athena.protocol.ids import new_id
from athena.protocol.memory import MemoryScope

__all__ = ["OperatorQueryService"]

_logger = logging.getLogger("athena.service")


class OperatorQueryService:
    """Operator projections (permissions, diff, artifacts, generated caps)."""

    def __init__(self, service: Any) -> None:
        self._svc = service

    async def operator_permissions(self) -> dict:
        """Active policy grants plus pending approval requests."""
        grants: list[dict] = []
        if self._svc._policy is not None:
            try:
                for g in self._svc._policy.approvals.list_active():
                    grants.append(
                        {
                            "approval_id": g.id,
                            "scope": getattr(g.scope, "value", str(g.scope)),
                            "capability": g.capability,
                            "resource_pattern": g.resource_pattern,
                            "task_id": g.task_id,
                            "session_id": g.session_id,
                            "expires_at": (g.expires_at.isoformat() if g.expires_at else None),
                        }
                    )
            except Exception as exc:
                _logger.warning("list_active grants failed: %s", exc)
        pending: list[dict] = []
        if self._svc._store_approvals is not None:
            try:
                for rec in await self._svc._store_approvals.list_pending():
                    pending.append(
                        {
                            "approval_id": rec.get("id"),
                            "capability_id": rec.get("capability_id"),
                            "arguments": rec.get("arguments"),
                            "created_at": rec.get("created_at"),
                        }
                    )
            except Exception as exc:
                _logger.warning("list_pending approvals failed: %s", exc)
        return {"active_grants": grants, "pending": pending}

    async def operator_diff(self, *, limit: int = 25) -> list[dict]:
        """Recent file mutations from the write-ahead mutation ledger."""
        if self._svc._store_mutations is None:
            return []
        try:
            rows = await self._svc._store_mutations.list_recent(limit=limit)
        except Exception as exc:
            _logger.warning("mutation listing failed: %s", exc)
            return []
        return [
            {
                "id": r.get("id"),
                "task_id": r.get("task_id"),
                "resource": r.get("resource"),
                "operation": r.get("operation"),
                "status": r.get("status"),
                "reversible": bool(r.get("reversible")),
                "before_ref": r.get("before_ref") or r.get("before_state"),
                "after_state": r.get("after_state"),
                "created_at": r.get("created_at"),
            }
            for r in rows
            if isinstance(r, dict)
        ]

    async def undo_mutation(self, mutation_id: str) -> dict:
        """Roll back one completed mutation through the RollbackExecutor."""
        if self._svc._store_mutations is None:
            return {"status": "error", "error": "mutation store unavailable"}
        from athena.state.rollback import RollbackExecutor

        executor = RollbackExecutor(self._svc._store_mutations, self._svc._artifacts)
        try:
            outcome = await executor.execute_inverse(mutation_id)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        # Emit an event so the surface and audit trail see the rollback.
        try:
            sink = self._svc._forward_events(self._svc._require_events())
            await sink(
                make_event(
                    "MutationRolledBack",
                    {
                        "mutation_id": mutation_id,
                        "outcome": outcome.get("status"),
                        "rollback_id": outcome.get("rollback_id"),
                    },
                )
            )
        except Exception as exc:
            _logger.warning("rollback event emission failed: %s", exc)
        return outcome

    async def operator_context_summary(self, session_id: str | None = None) -> dict:
        """What the model would actually see next turn (bounded-context view)."""
        info: dict = {"session_id": session_id}
        if session_id and self._svc._store_messages is not None:
            try:
                info["message_count"] = await self._svc._store_messages.count_session_messages(
                    session_id
                )
            except Exception as exc:
                _logger.warning("session message count failed: %s", exc)
        if self._svc._compiler is not None:
            try:
                window = getattr(self._svc._compiler, "context_window", None)
                reserve = getattr(self._svc._compiler, "reserve_output", None)
                recent = getattr(self._svc._compiler, "recent_verbatim_turns", None)
                info["window"] = int(window) if window else None
                info["reserve_output"] = int(reserve) if reserve else None
                info["recent_verbatim_turns"] = int(recent) if recent else None
            except Exception:
                pass
        return info

    async def operator_artifacts(self, *, limit: int = 50) -> list[dict]:
        """Artifact index across all tasks (evidence view)."""
        if self._svc._artifacts is None:
            return []
        try:
            refs = await self._svc._artifacts.list(limit=limit)
        except Exception as exc:
            _logger.warning("artifact listing failed: %s", exc)
            return []
        out: list[dict] = []
        for ref in refs:
            out.append(
                {
                    "uri": getattr(ref, "uri", None),
                    "name": getattr(ref, "name", None),
                    "mime_type": getattr(ref, "mime_type", None),
                    "kind": getattr(ref, "kind", None),
                    "task_id": getattr(ref, "task_id", None),
                    "producer": getattr(ref, "producer", None),
                }
            )
        return out

    async def operator_generated_capabilities(self, task_id: str | None = None) -> list[dict]:
        """Review candidates for one task through the canonical synthesis API."""
        result = await self._invoke_synthesis({"operation": "candidates"}, task_id=task_id)
        return result["value"]

    async def operator_memory_candidates(self, *, limit: int = 100) -> list[dict]:
        """Project pending memory lessons into the shared review surface."""
        if self._svc._memory is None:
            return []
        records = await self._svc._memory.list_pending_candidates(limit=limit)
        return [_memory_candidate_view(record) for record in records]

    async def operator_candidates(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Return one bounded review queue for every learned candidate kind.

        This is a projection only.  Promotion still dispatches to the owning
        memory, skill, workflow, or generated-capability gate; this method
        merely prevents operators from having to know which hidden store owns
        a candidate.
        """
        rows: list[dict[str, Any]] = []
        rows.extend(await self.operator_memory_candidates())

        skills = getattr(self._svc, "_skill_lifecycle", None)
        if skills is not None:
            list_candidates = getattr(skills, "list_candidates", None)
            if callable(list_candidates):
                rows.extend(await list_candidates())

        workflows = getattr(self._svc, "_workflow_store", None)
        if workflows is not None and task_id:
            try:
                for workflow in await workflows.list(
                    task_id=task_id,
                    project_id=getattr(self._svc._default_workspace, "id", None),
                    user_id=self._svc.config.cache_namespace,
                ):
                    if getattr(getattr(workflow, "scope", None), "value", None) != "candidate":
                        continue
                    provenance = dict(getattr(workflow, "provenance", {}) or {})
                    rows.append(
                        {
                            "id": workflow.id,
                            "type": "workflow",
                            "name": workflow.name,
                            "source_task": workflow.task_scope,
                            "evidence": list(provenance.get("observations") or ()),
                            "observation_count": int(
                                provenance.get("successful_observations") or 0
                            ),
                            "last_observed_at": provenance.get("last_observed_at"),
                            "proposed_scope": "project",
                            "trust": "agent_curated",
                            "conflicts": [],
                            "required_action": "operator_review",
                            "lifecycle_state": workflow.lifecycle_state,
                            "description": workflow.description,
                            "proof": provenance,
                        }
                    )
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("workflow candidate projection failed: %s", exc)

        if task_id:
            try:
                generated = await self.operator_generated_capabilities(task_id)
                rows.extend(
                    {
                        **dict(row),
                        "id": row.get("id") or row.get("capability_id"),
                        "type": "generated",
                        "source_task": task_id,
                        "required_action": "operator_review",
                    }
                    for row in generated
                )
            except (RuntimeError, ValueError) as exc:
                _logger.info("generated candidate projection unavailable: %s", exc)
        # Stable ordering makes the operator view and evidence reproducible.
        rows.sort(key=lambda item: (str(item.get("type") or ""), str(item.get("id") or "")))
        return rows

    async def operator_candidate_item(
        self, candidate_id: str, task_id: str | None = None
    ) -> dict[str, Any] | None:
        """Inspect one candidate from the shared queue."""
        for item in await self.operator_candidates(task_id):
            if str(item.get("id") or item.get("capability_id") or "") == candidate_id:
                return item
        return None

    async def operator_promote_candidate(
        self,
        candidate_id: str,
        *,
        target_scope: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Promote a candidate through its owning subsystem's proof gate."""
        item = await self.operator_candidate_item(candidate_id, task_id)
        if item is None:
            return {"status": "error", "error": "candidate not found or not visible"}
        kind = str(item.get("type") or "")
        if kind == "memory":
            return await self.operator_promote_memory_candidate(
                candidate_id, target_scope, item.get("scope_id")
            )
        if kind == "skill":
            lifecycle = getattr(self._svc, "_skill_lifecycle", None)
            promote = getattr(lifecycle, "promote_candidate", None)
            if not callable(promote):
                return {"status": "error", "error": "skill candidate lifecycle unavailable"}
            skill_id = await promote(candidate_id, task_id=task_id, authorized=True)
            if skill_id is None:
                return {"status": "error", "error": "skill candidate proof gate refused promotion"}
            return {"status": "promoted", "candidate_id": candidate_id, "skill_id": skill_id}
        if kind == "workflow":
            return await self._promote_workflow_candidate(
                candidate_id, target_scope, task_id=task_id
            )
        if kind in {"generated", "capability", "synthesis"}:
            return await self.operator_promote_generated_capability(
                candidate_id, target_scope, task_id
            )
        return {"status": "error", "error": f"unsupported candidate type: {kind}"}

    async def _promote_workflow_candidate(
        self, workflow_id: str, scope: str, *, task_id: str | None
    ) -> dict[str, Any]:
        if not task_id or self._svc._dispatcher is None:
            return {"status": "error", "error": "workflow promotion requires a current task"}
        from athena.protocol.capabilities import (
            CapabilityRequest,
            CapabilityRequestOrigin,
            CapabilityResult,
            CapabilityResultStatus,
        )

        result = await self._svc._dispatcher.dispatch(
            CapabilityRequest(
                capability_id="workflow",
                arguments={"operation": "promote", "workflow_id": workflow_id, "scope": scope},
                task_id=task_id,
                call_id=new_id("operator-workflow"),
                origin=CapabilityRequestOrigin.USER_DIRECT,
            ),
            workspace=self._svc._default_workspace,
            profile=self._svc.config.autonomy_level,
        )
        if not isinstance(result, CapabilityResult):
            return {"status": "error", "error": "workflow promotion requires approval"}
        if result.status is not CapabilityResultStatus.OK:
            return {
                "status": "error",
                "error": result.error or "workflow proof gate refused promotion",
            }
        try:
            value = json.loads(result.output or "null")
        except (TypeError, ValueError):
            value = result.output
        return {"status": "promoted", "candidate_id": workflow_id, "workflow": value}

    async def operator_deprecate_candidate(
        self, candidate_id: str, *, task_id: str | None = None
    ) -> dict[str, Any]:
        """Move one candidate to a non-active lifecycle state."""
        item = await self.operator_candidate_item(candidate_id, task_id)
        if item is None:
            return {"status": "error", "error": "candidate not found or not visible"}
        kind = str(item.get("type") or "")
        if kind == "memory":
            ok = await self._svc._memory.discard_pending_candidate(candidate_id)
        elif kind == "skill":
            lifecycle = getattr(self._svc, "_skill_lifecycle", None)
            discard = getattr(lifecycle, "discard_candidate", None)
            ok = bool(await discard(candidate_id)) if callable(discard) else False
        elif kind == "workflow":
            workflow = await self._svc._workflow_store.get(
                candidate_id,
                task_id=task_id,
                project_id=getattr(self._svc._default_workspace, "id", None),
                user_id=self._svc.config.cache_namespace,
            )
            if workflow is None:
                ok = False
            else:
                from dataclasses import replace

                await self._svc._workflow_store.save(
                    replace(workflow, enabled=False, lifecycle_state="DEPRECATED")
                )
                ok = True
        elif kind == "generated":
            result = await self.operator_deprecate_generated_capability(candidate_id, task_id)
            return {"status": "deprecated", "candidate_id": candidate_id, **result}
        else:
            return {"status": "error", "error": f"unsupported candidate type: {kind}"}
        return {
            "status": "deprecated" if ok else "error",
            "candidate_id": candidate_id,
            **({} if ok else {"error": "candidate lifecycle refused deprecation"}),
        }

    async def operator_memory_candidate(self, memory_id: str) -> dict | None:
        """Inspect one memory candidate without changing its lifecycle."""
        if self._svc._memory is None:
            return None
        record = await self._svc._memory.get(memory_id)
        if record is None or (record.metadata or {}).get("pending_promotion") is not True:
            return None
        return _memory_candidate_view(record)

    async def operator_promote_memory_candidate(
        self, memory_id: str, scope: str, scope_id: str | None = None
    ) -> dict:
        """Promote only through the durable memory candidate gate."""
        if self._svc._memory is None:
            return {"status": "error", "error": "memory store unavailable"}
        try:
            target_scope = MemoryScope(scope.strip().lower())
        except ValueError:
            return {"status": "error", "error": f"unknown memory scope: {scope}"}
        if target_scope is MemoryScope.TASK:
            return {"status": "error", "error": "task scope requires a task-local operator flow"}
        record = await self._svc._memory.promote_pending_candidate(
            memory_id,
            scope=target_scope,
            scope_id=scope_id,
        )
        if record is None:
            return {"status": "error", "error": "memory candidate not found or already reviewed"}
        return {"status": "promoted", "candidate": _memory_candidate_view(record)}

    async def operator_discard_memory_candidate(self, memory_id: str) -> dict:
        """Discard one pending memory lesson through the durable store."""
        if self._svc._memory is None:
            return {"status": "error", "error": "memory store unavailable"}
        discarded = await self._svc._memory.discard_pending_candidate(memory_id)
        return {
            "status": "discarded" if discarded else "error",
            **({} if discarded else {"error": "memory candidate not found or already reviewed"}),
        }

    async def operator_generated_capability(
        self, capability_id: str, task_id: str | None = None
    ) -> dict:
        """Inspect one generated capability through the canonical synthesis API."""
        result = await self._invoke_synthesis(
            {"operation": "inspect", "capability_id": capability_id}, task_id=task_id
        )
        return result["value"]

    async def operator_promote_generated_capability(
        self, capability_id: str, scope: str, task_id: str | None = None
    ) -> dict:
        """Promote a generated capability through policy and synthesis."""
        return await self._invoke_synthesis(
            {"operation": "promote", "capability_id": capability_id, "scope": scope},
            task_id=task_id,
        )

    async def operator_deprecate_generated_capability(
        self, capability_id: str, task_id: str | None = None
    ) -> dict:
        """Retire a generated capability through policy and synthesis."""
        return await self._invoke_synthesis(
            {"operation": "deprecate", "capability_id": capability_id}, task_id=task_id
        )

    async def _invoke_synthesis(self, arguments: dict, *, task_id: str | None) -> dict:
        from athena.protocol.capabilities import (
            CapabilityRequest,
            CapabilityRequestOrigin,
            CapabilityResult,
            CapabilityResultStatus,
        )

        if self._svc._dispatcher is None:
            raise RuntimeError("AthenaService not started")
        result = await self._svc._dispatcher.dispatch(
            CapabilityRequest(
                capability_id="synthesis",
                arguments=arguments,
                task_id=task_id,
                call_id=new_id("operator-synthesis"),
                origin=CapabilityRequestOrigin.USER_DIRECT,
            ),
            workspace=self._svc._default_workspace,
            profile=self._svc.config.autonomy_level,
        )
        if not isinstance(result, CapabilityResult):
            raise RuntimeError("generated capability operation requires approval")
        if result.status is not CapabilityResultStatus.OK:
            raise ValueError(result.error or "generated capability operation failed")
        try:
            value = json.loads(result.output or "null")
        except (TypeError, ValueError) as exc:
            raise ValueError("generated capability operation returned invalid output") from exc
        return {"value": value, "metadata": dict(result.metadata or {})}


def _memory_candidate_view(record: Any) -> dict[str, Any]:
    """Bounded, operator-readable projection for one pending memory lesson."""
    metadata = dict(getattr(record, "metadata", {}) or {})
    source = getattr(record, "source", None)
    return {
        "id": record.id,
        "type": "memory",
        "content": str(record.content or "")[:4000],
        "summary": getattr(record, "summary", None),
        "source_task": metadata.get("task_id")
        or (getattr(source, "source_id", None) if getattr(source, "source_type", None) else None),
        "scope_id": metadata.get("scope_id")
        or metadata.get("project_id")
        or metadata.get("user_id"),
        "evidence": list(getattr(record, "source_refs", ()) or ()),
        "observation_count": metadata.get("observation_count", 1),
        "last_observed_at": metadata.get("last_observed_at")
        or getattr(record, "updated_at", None)
        or getattr(record, "created_at", None),
        "proposed_scope": getattr(getattr(record, "scope", None), "value", record.scope),
        "trust": getattr(getattr(record, "trust", None), "value", record.trust),
        "conflicts": list(getattr(record, "contradicted_by", ()) or ()),
        "required_action": "operator_review",
        "lifecycle_state": "pending_promotion",
    }
