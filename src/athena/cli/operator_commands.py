"""Shared operator command routing for hosted and native surfaces.

The service remains the authority for every command with task, approval,
mutation, or generated-capability effects.  Surfaces provide only output and
small presentation-state callbacks, so adding a command cannot silently make
the native session use different service semantics from hosted chat.
"""

from __future__ import annotations

from collections.abc import Callable
from inspect import isawaitable
from typing import Any


class OperatorCommandRouter:
    """Dispatch service-backed operator commands through one vocabulary."""

    _SHARED = frozenset(
        {
            "approve",
            "cancel",
            "candidate",
            "candidates",
            "compact",
            "context",
            "criteria",
            "deprecate",
            "deny",
            "diff",
            "interrupted",
            "model",
            "new",
            "permissions",
            "promote",
            "resume",
            "sessions",
            "undo",
            "autonomy",
        }
    )

    def __init__(
        self,
        service: Any | Callable[[], Any],
        *,
        emit: Callable[[str], Any],
        get_task_id: Callable[[], str | None],
        get_session_id: Callable[[], str | None] | None = None,
        set_session_id: Callable[[str | None], None] | None = None,
        set_model: Callable[[str | None], None] | None = None,
        set_autonomy: Callable[[str], str] | None = None,
        set_criteria: Callable[[list[str]], None] | None = None,
        new_handler: Callable[[], Any] | None = None,
        resume_handler: Callable[[str], Any] | None = None,
    ) -> None:
        self._service_source = service
        self._emit = emit
        self._get_task_id = get_task_id
        self._get_session_id = get_session_id
        self._set_session_id = set_session_id
        self._set_model = set_model
        self._set_autonomy = set_autonomy
        self._set_criteria = set_criteria
        self._new_handler = new_handler
        self._resume_handler = resume_handler

    def _service(self) -> Any:
        service = self._service_source() if callable(self._service_source) else self._service_source
        if service is None:
            raise RuntimeError("operator service is not started")
        return service

    async def dispatch(self, line: str) -> bool:
        """Handle a shared command; return ``False`` for surface-local input."""
        name, _, argument = line[1:].partition(" ")
        name = name.strip().casefold()
        argument = argument.strip()
        if name not in self._SHARED:
            return False
        if name == "resume":
            if self._resume_handler is None:
                self._emit("resume is unavailable on this surface")
            else:
                await self._resume_handler(argument)
            return True
        service = self._service()

        if name == "new":
            if self._new_handler is not None:
                result = self._new_handler()
                if isawaitable(result):
                    await result
            elif self._set_session_id is not None:
                self._set_session_id(None)
            self._emit("fresh session")
            return True
        if name == "sessions":
            rows = await service.list_sessions()
            if not rows:
                self._emit("(no sessions)")
            for row in rows or ():
                if isinstance(row, dict):
                    self._emit(f"{row.get('id')}\t{row.get('objective') or ''}")
                else:
                    self._emit(str(getattr(row, "id", row)))
            return True
        if name == "permissions":
            view = await service.operator_permissions()
            grants = view.get("active_grants") or ()
            pending = view.get("pending") or ()
            if not grants:
                self._emit("no active grants")
            for grant in grants:
                self._emit(
                    f"grant {grant.get('approval_id')} · {grant.get('capability') or '*'}"
                    f" · {grant.get('scope') or '?'}"
                )
            if not pending:
                self._emit("no pending approvals")
            for item in pending:
                self._emit(f"pending {item.get('approval_id')} · {item.get('capability_id')}")
            return True
        if name == "diff":
            limit = int(argument) if argument.isdigit() else 25
            rows = await service.operator_diff(limit=limit)
            if not rows:
                self._emit("(no recorded mutations)")
            for row in rows or ():
                self._emit(
                    f"{row.get('id')} · {row.get('status')} · "
                    f"{row.get('operation')} {row.get('resource')}"
                )
            return True
        if name == "undo":
            if not argument:
                self._emit("usage: /undo MUTATION_ID")
                return True
            outcome = await service.undo_mutation(argument)
            self._emit(
                f"undo {argument}: {outcome.get('status')} {outcome.get('error') or ''}".strip()
            )
            return True
        if name in {"context", "compact"}:
            session_id = self._get_session_id() if self._get_session_id is not None else None
            summary = await service.operator_context_summary(session_id)
            window = summary.get("window")
            reserve = summary.get("reserve_output")
            recent = summary.get("recent_verbatim_turns")
            count = summary.get("message_count")
            if name == "compact":
                self._emit(f"context window: {window or '?'} tokens")
                self._emit(f"output reserve: {reserve if reserve is not None else '?'} tokens")
                self._emit(f"recent verbatim turns: {recent if recent is not None else '?'}")
                self._emit("older transcript: compressed with provenance retained")
            else:
                self._emit(f"session: {session_id or '(none yet)'}")
                self._emit(f"durable messages: {count if count is not None else '?'}")
                self._emit("next turn includes: objective, policy boundaries, recent turns,")
                self._emit("capability calls/results, relevant memories and skills.")
            return True
        if name == "criteria":
            criteria = [item.strip() for item in argument.split(";") if item.strip()]
            if self._set_criteria is not None:
                self._set_criteria(criteria)
            self._emit("acceptance criteria: " + ("; ".join(criteria) or "cleared"))
            return True
        if name == "interrupted":
            rows = await service.list_interrupted()
            if not rows:
                self._emit("(no interrupted tasks)")
            for row in rows or ():
                self._emit(f"{row.get('id')}\t{str(row.get('objective') or '')[:80]}")
            return True
        if name == "model":
            if self._set_model is not None:
                self._set_model(argument or None)
            self._emit(f"model: {argument or 'default'}")
            return True
        if name == "autonomy":
            value = self._set_autonomy(argument) if self._set_autonomy is not None else argument
            self._emit(f"autonomy: {value or 'supervised'}")
            return True
        if name == "approve":
            approval_id, _, scope = argument.partition(" ")
            if not approval_id:
                self._emit("approval id required")
                return True
            await service.approve(approval_id, granted=True, scope=scope or None)
            self._emit(f"approval {approval_id}: granted" + (f" ({scope})" if scope else ""))
            return True
        if name == "deny":
            if not argument:
                self._emit("approval id required")
                return True
            await service.approve(argument, granted=False)
            self._emit(f"approval {argument}: denied")
            return True
        if name == "cancel":
            task_id = argument or self._get_task_id()
            if not task_id:
                self._emit("(no active task)")
                return True
            await service.cancel(task_id)
            self._emit(f"cancel requested for {task_id}")
            return True
        task_id = self._get_task_id()
        if name == "candidates":
            if task_id is None:
                self._emit("(no task context for generated candidates)")
                return True
            rows = await service.operator_generated_capabilities(task_id)
            if not rows:
                self._emit("(no generated candidates)")
            for row in rows or ():
                usage = dict((row.get("proof") or {}).get("usage") or {})
                self._emit(
                    f"{row.get('capability_id')} · {row.get('lifecycle_state')} · "
                    f"{usage.get('successes', 0)}/{usage.get('uses', 0)} successful"
                )
                self._emit(f"  {row.get('description') or ''}")
            return True
        if name == "candidate":
            if not argument:
                self._emit("usage: /candidate CAPABILITY_ID")
                return True
            try:
                row = await service.operator_generated_capability(argument, task_id)
            except (RuntimeError, ValueError) as exc:
                self._emit(f"candidate unavailable: {exc}")
                return True
            proof = dict(row.get("proof_record") or {})
            usage = dict(proof.get("usage") or {})
            self._emit(f"{row.get('id')} · {row.get('scope')} · {row.get('lifecycle_state')}")
            self._emit(f"  {row.get('description') or ''}")
            self._emit(f"  proof: {usage.get('successes', 0)}/{usage.get('uses', 0)} successful")
            if proof.get("quality_score") is not None:
                self._emit(f"  quality: {proof['quality_score']}")
            self._emit(f"  code hash: {row.get('code_hash')}")
            self._emit(f"  schema hash: {row.get('schema_hash')}")
            return True
        if name == "promote":
            parts = argument.split()
            if len(parts) != 2 or parts[1] not in {"project", "user"}:
                self._emit("usage: /promote CAPABILITY_ID project|user")
                return True
            try:
                outcome = await service.operator_promote_generated_capability(
                    parts[0], parts[1], task_id
                )
            except (RuntimeError, ValueError) as exc:
                self._emit(f"promotion failed: {exc}")
                return True
            promoted_value = dict(outcome.get("value") or {})
            owner = promoted_value.get("project_id") or promoted_value.get("user_id") or "?"
            self._emit(f"promoted {parts[0]} to {parts[1]} {owner}")
            return True
        if name == "deprecate":
            if not argument:
                self._emit("usage: /deprecate CAPABILITY_ID")
                return True
            try:
                await service.operator_deprecate_generated_capability(argument, task_id)
            except (RuntimeError, ValueError) as exc:
                self._emit(f"deprecation failed: {exc}")
                return True
            self._emit(f"deprecated {argument}")
            return True
        return False


__all__ = ["OperatorCommandRouter"]
