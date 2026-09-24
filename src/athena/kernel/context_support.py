"""Context compilation and evidence projection for the reasoning kernel."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Awaitable, Callable

from athena.kernel.adaptive_gate import project_adaptive_decision
from athena.protocol.errors import ContextIntegrityError
from athena.protocol.messages import Message

__all__ = ["ContextSupport"]


class ContextSupport:
    """Load canonical transcript, compile context, and emit context evidence."""

    def __init__(
        self,
        *,
        compiler: Any,
        messages: Any,
        runs: dict[str, Any],
        emit: Callable[..., Awaitable[None]],
        recovery_hint: Callable[[Any], dict[str, Any]],
        generated_recovery_hint: Callable[[Any], dict[str, Any]],
        textable_messages: Callable[[list[Message]], list[Message]],
    ) -> None:
        self._compiler = compiler
        self._messages = messages
        self._runs = runs
        self._emit = emit
        self._recovery_hint = recovery_hint
        self._generated_recovery_hint = generated_recovery_hint
        self._textable_messages = textable_messages

    async def compile(self, task: Any, *, context_window: int | None = None) -> Any:
        context_task = self._with_recovery_context(task)
        recent = await self._load_recent(task)
        compiled = await self._compiler.compile(
            context_task,
            recent_messages=self._textable_messages(recent),
            workspace=context_task.workspace.root if context_task.workspace else None,
            context_window=context_window,
        )
        await self._emit_context_evidence(task, compiled)
        return compiled

    def _with_recovery_context(self, task: Any) -> Any:
        state = self._runs.get(task.id)
        context_task = task
        if state is not None and state.speculative_recovery_pending:
            metadata = dict(task.metadata or {})
            metadata["_runtime_recovery_hint"] = self._recovery_hint(state)
            context_task = replace(task, metadata=metadata)
        if state is not None and state.generated_recovery_pending:
            metadata = dict(task.metadata or {})
            metadata["_runtime_recovery_hint"] = self._generated_recovery_hint(state)
            context_task = replace(task, metadata=metadata)
        if state is not None:
            adaptive = project_adaptive_decision(state)
            if adaptive is not None and adaptive.get("kind") != "ordinary_reasoning":
                metadata = dict(context_task.metadata or {})
                metadata["_adaptive_recovery"] = adaptive
                context_task = replace(task, metadata=metadata)
        return context_task

    async def _load_recent(self, task: Any) -> list[Message]:
        if not task.session_id:
            return []
        try:
            loader = getattr(self._messages, "list_causal_messages", None)
            if loader is not None:
                return await loader(task.session_id, task.id)
            loader = getattr(self._messages, "list_task_messages", None)
            if loader is not None:
                return await loader(task.session_id, task.id)
            loader = getattr(self._messages, "list_recent_session_messages", None)
            if loader is None:
                loader = self._messages.list_session_messages
            return await loader(task.session_id)
        except Exception as exc:
            raise ContextIntegrityError(
                f"canonical transcript unavailable for session {task.session_id}",
                cause=exc,
                session_id=task.session_id,
            ) from exc

    async def _emit_context_evidence(self, task: Any, compiled: Any) -> None:
        strategy = compiled.strategy
        await self._emit("StrategySelected", strategy.to_dict(), task)
        if compiled.degradations:
            await self._emit(
                "DiagnosticsProduced",
                {
                    "kind": "context_degradation",
                    "degradations": [
                        {"source": d.source, "scope": d.scope, "detail": d.detail}
                        for d in compiled.degradations
                    ],
                    "count": len(compiled.degradations),
                },
                task,
            )
        if compiled.selected_skill_versions:
            await self._emit(
                "SkillContextSelected",
                {
                    "skills": [
                        {"skill_id": skill_id, "version": version}
                        for skill_id, version in compiled.selected_skill_versions
                    ],
                    "source": "context_injection",
                },
                task,
            )
        if strategy.missing_affordance:
            await self._emit(
                "AffordanceGapDetected",
                {
                    "missing_affordance": strategy.missing_affordance,
                    "route": strategy.route,
                },
                task,
            )
