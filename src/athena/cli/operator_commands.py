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
            "memory",
            "new",
            "permissions",
            "promote",
            "resume",
            "sessions",
            "undo",
            "autonomy",
            "jobs",
            "workflows",
            "packs",
            "skills",
            "health",
        }
    )
    _LOCAL = frozenset({"exit", "quit", "help", "details", "scroll", "mascot"})

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
            if name not in self._LOCAL:
                self._emit(f"unknown command: /{name}; try /help")
                return True
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
        if name == "health":
            report = service.runtime_health()
            for subsystem, state in report.items():
                if isinstance(state, dict):
                    value = state.get("health") or state.get("state") or "unknown"
                    detail = state.get("error") or state.get("last_error") or ""
                    self._emit(f"{subsystem}: {value}" + (f" ({detail})" if detail else ""))
                else:
                    self._emit(f"{subsystem}: {state}")
            return True
        if name == "jobs":
            action, _, value = argument.partition(" ")
            action = action.casefold() or "list"
            if action in {"list", "show"}:
                rows = await service.list_jobs(enabled_only=False)
                if action == "show" and value:
                    rows = [row for row in rows if str(row.get("id")) == value]
                if not rows:
                    self._emit("(no scheduled jobs)")
                for row in rows:
                    last = row.get("last_run_receipt") or {}
                    self._emit(
                        f"{row.get('id')} · {'enabled' if row.get('enabled') else 'disabled'} · "
                        f"next={row.get('next_run') or '-'} · last={last.get('status') or '-'}"
                    )
                return True
            if action in {"enable", "disable"} and value:
                changed = await service.job_set_enabled(value, action == "enable")
                self._emit(f"job {value}: {action if changed else 'not found'}")
                return True
            if action in {"run", "run-now"} and value:
                task_id = await service.job_run_now(value)
                self._emit(f"job {value}: " + (f"started task {task_id}" if task_id else "not run"))
                return True
            self._emit("usage: /jobs list|show ID|enable ID|disable ID|run-now ID")
            return True
        if name == "workflows":
            action, _, value = argument.partition(" ")
            action = action.casefold() or "list"
            if action == "list":
                rows = await service.list_workflows(task_id=value or None)
                if not rows:
                    self._emit("(no visible workflows)")
                for row in rows:
                    self._emit(
                        f"{row.get('id')} · {row.get('scope')} · "
                        f"{row.get('lifecycle_state')} · "
                        f"{'enabled' if row.get('enabled', True) else 'disabled'} · "
                        f"{row.get('name')}"
                    )
                return True
            if action in {"show", "inspect", "describe"} and value:
                parts = value.split()
                row = await service.inspect_workflow(
                    parts[0], task_id=parts[1] if len(parts) > 1 else None
                )
                if row is None:
                    self._emit("workflow unavailable")
                    return True
                self._emit(
                    f"{row.get('id')} · {row.get('scope')} · "
                    f"{row.get('lifecycle_state')} · "
                    f"{'enabled' if row.get('enabled', True) else 'disabled'} · "
                    f"{row.get('name')}"
                )
                self._emit(f"  steps: {len(row.get('steps') or ())}")
                if row.get("description"):
                    self._emit(f"  {row['description']}")
                return True
            self._emit("usage: /workflows list [TASK_ID]|show WORKFLOW_ID [TASK_ID]")
            return True
        if name == "skills":
            action, _, value = argument.partition(" ")
            action = action.casefold() or "list"
            lifecycle = getattr(service, "_skill_lifecycle", None)
            if lifecycle is None:
                self._emit("skills: unavailable")
                return True
            if action in {"list", "search"}:
                rows = await lifecycle.list(active_only=False)
                if action == "search" and value:
                    rows = [
                        row
                        for row in rows
                        if value.casefold()
                        in f"{row.name} {row.description} {' '.join(row.triggers)}".casefold()
                    ]
                if not rows:
                    self._emit("(no installed skills)")
                for row in rows:
                    self._emit(
                        f"{row.id} · {'enabled' if row.enabled else 'disabled'} · "
                        f"trust={row.trust.value} · {row.name}"
                    )
                return True
            if action in {"enable", "disable"} and value:
                changed = await getattr(lifecycle, action)(value)
                self._emit(f"skill {value}: {action if changed else 'not found'}")
                return True
            self._emit("usage: /skills list|search TEXT|enable ID|disable ID")
            return True
        if name == "packs":
            action, _, value = argument.partition(" ")
            action = action.casefold() or "list"
            if action in {"list", "search"}:
                rows = await service.list_packs(value if action == "search" else None)
                if not rows:
                    self._emit("(no installed packs)")
                for row in rows:
                    health = (row.get("health_detail") or {}).get("status", row.get("health", "?"))
                    self._emit(
                        f"{row.get('id')}@{row.get('version')} · "
                        f"{'enabled' if row.get('enabled') else 'disabled'} · {health}"
                    )
                return True
            if action in {"enable", "disable", "remove"} and value:
                if action == "remove":
                    ok = await service.remove_pack(value)
                    self._emit(f"pack {value}: {'removed' if ok else 'not found'}")
                else:
                    method = getattr(service, f"{action}_pack")
                    row = await method(value)
                    self._emit(f"pack {value}: {'enabled' if row.get('enabled') else 'disabled'}")
                return True
            self._emit("usage: /packs list|search TEXT|enable ID|disable ID|remove ID")
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
        if name == "memory":
            action, _, value = argument.partition(" ")
            action = action.casefold()
            value = value.strip()
            if action in {"candidates", "list", ""}:
                rows = await service.operator_memory_candidates()
                if not rows:
                    self._emit("(no pending memory candidates)")
                for row in rows or ():
                    self._emit(
                        f"{row.get('id')} · {row.get('lifecycle_state')} · "
                        f"{row.get('proposed_scope')} · {row.get('trust')}"
                    )
                    self._emit(f"  {row.get('content') or row.get('summary') or ''}")
                return True
            if action == "inspect":
                row = await service.operator_memory_candidate(value)
                if row is None:
                    self._emit("memory candidate unavailable")
                else:
                    self._emit(f"{row.get('id')} · {row.get('lifecycle_state')}")
                    self._emit(f"  proposed: {row.get('proposed_scope')} / {row.get('trust')}")
                    self._emit(f"  source task: {row.get('source_task') or '-'}")
                    self._emit(f"  evidence: {', '.join(row.get('evidence') or ()) or '-'}")
                    self._emit(f"  {row.get('content') or row.get('summary') or ''}")
                return True
            parts = value.split()
            if action == "promote" and len(parts) in {2, 3}:
                outcome = await service.operator_promote_memory_candidate(
                    parts[0], parts[1], parts[2] if len(parts) == 3 else None
                )
                self._emit(
                    f"memory {parts[0]}: {outcome.get('status')} "
                    f"{outcome.get('error') or ''}".strip()
                )
                return True
            if action == "discard" and len(parts) == 1:
                outcome = await service.operator_discard_memory_candidate(parts[0])
                self._emit(
                    f"memory {parts[0]}: {outcome.get('status')} "
                    f"{outcome.get('error') or ''}".strip()
                )
                return True
            self._emit(
                "usage: /memory candidates | inspect ID | "
                "promote ID project|global [SCOPE_ID] | discard ID"
            )
            return True
        if name == "model":
            if not argument:
                self._emit("usage: /model PROVIDER/MODEL (or /model default)")
                return True
            if argument.casefold() != "default":
                registry = getattr(service, "_model_registry", None)
                if registry is not None:
                    models = await registry.list_models()
                    valid = {str(info.id) for info in models} | {
                        f"{info.provider}/{info.id}" for info in models
                    }
                    if argument not in valid:
                        choices = ", ".join(sorted(valid)) or "(none configured)"
                        self._emit(f"invalid model {argument!r}; choose from: {choices}")
                        return True
            if self._set_model is not None:
                self._set_model(None if argument.casefold() == "default" else argument)
            self._emit(f"model: {argument}")
            return True
        if name == "autonomy":
            if not argument:
                self._emit("usage: /autonomy supervised|assisted|coding|autonomous")
                return True
            try:
                value = self._set_autonomy(argument) if self._set_autonomy is not None else argument
            except (TypeError, ValueError) as exc:
                self._emit(f"invalid autonomy: {exc}")
                return True
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
            shared = getattr(service, "operator_candidates", None)
            if callable(shared):
                rows = await shared(task_id)
                if not rows:
                    self._emit("(no candidates)")
                for row in rows:
                    identifier = row.get("id") or row.get("capability_id") or "?"
                    label = row.get("name") or row.get("description") or ""
                    evidence = row.get("observation_count")
                    suffix = f" · observations={evidence}" if evidence is not None else ""
                    self._emit(
                        f"{identifier} · {row.get('type', 'candidate')} · "
                        f"{row.get('lifecycle_state', 'unknown')}{suffix}"
                    )
                    if label:
                        self._emit(f"  {label}")
                return True
            memory_candidates = getattr(service, "operator_memory_candidates", None)
            memory_rows = await memory_candidates() if callable(memory_candidates) else []
            if memory_rows:
                self._emit("memory candidates:")
                for row in memory_rows:
                    self._emit(
                        f"{row.get('id')} · pending memory · {row.get('proposed_scope')} · "
                        f"{row.get('trust')}"
                    )
            if task_id is None:
                if not memory_rows:
                    self._emit("(no candidates)")
                return True
            rows = await service.operator_generated_capabilities(task_id)
            if not rows and not memory_rows:
                self._emit("(no candidates)")
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
            shared = getattr(service, "operator_candidate_item", None)
            if callable(shared):
                row = await shared(argument, task_id)
                if row is None:
                    self._emit("candidate unavailable")
                    return True
                self._emit(
                    f"{row.get('id') or row.get('capability_id')} · "
                    f"{row.get('type', 'candidate')} · {row.get('lifecycle_state', 'unknown')}"
                )
                for key in (
                    "name",
                    "description",
                    "proposed_scope",
                    "trust",
                    "source_task",
                    "rationale",
                ):
                    value = row.get(key)
                    if value:
                        self._emit(f"  {key}: {value}")
                self._emit(
                    f"  evidence: {len(row.get('evidence') or ())}; "
                    f"observations: {row.get('observation_count', 0)}"
                )
                return True
            memory_inspect = getattr(service, "operator_memory_candidate", None)
            memory_row = await memory_inspect(argument) if callable(memory_inspect) else None
            if memory_row is not None:
                self._emit(f"{memory_row.get('id')} · pending memory")
                self._emit(
                    f"  proposed: {memory_row.get('proposed_scope')} / {memory_row.get('trust')}"
                )
                self._emit(f"  source task: {memory_row.get('source_task') or '-'}")
                self._emit(f"  evidence: {', '.join(memory_row.get('evidence') or ()) or '-'}")
                self._emit(f"  {memory_row.get('content') or memory_row.get('summary') or ''}")
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
            if len(parts) != 2 or parts[1] not in {"project", "user", "global"}:
                self._emit("usage: /promote CANDIDATE_ID project|user|global")
                return True
            shared = getattr(service, "operator_promote_candidate", None)
            if callable(shared):
                outcome = await shared(parts[0], target_scope=parts[1], task_id=task_id)
                if outcome.get("status") != "promoted":
                    self._emit(f"promotion failed: {outcome.get('error') or 'proof gate refused'}")
                else:
                    self._emit(f"promoted {parts[0]} to {parts[1]}")
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
            shared = getattr(service, "operator_deprecate_candidate", None)
            if callable(shared):
                outcome = await shared(argument, task_id=task_id)
                if outcome.get("status") != "deprecated":
                    self._emit(f"deprecation failed: {outcome.get('error') or 'lifecycle refused'}")
                else:
                    self._emit(f"deprecated {argument}")
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
