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
