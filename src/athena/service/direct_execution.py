"""Direct OI-style execution through the canonical capability dispatcher."""

from __future__ import annotations

import inspect
from typing import Any

from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.continuations import SuspendedCall
from athena.protocol.ids import new_id
from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
    Message,
    Provenance,
    Role,
    SourceType,
    utcnow,
)

__all__ = ["DirectExecutionPorts", "DirectExecutionService"]


class DirectExecutionPorts:
    """Allowlisted resources and one application approval operation."""

    _RESOURCE_NAMES = {
        "dispatcher": "_dispatcher",
        "default_workspace": "_default_workspace",
        "config": "config",
        "sessions": "_sessions",
        "store_messages": "_store_messages",
    }
    _APPLICATION_OPERATIONS = {"approve": "approve"}

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        resource_name = self._RESOURCE_NAMES.get(name)
        if resource_name is None:
            resource_name = self._APPLICATION_OPERATIONS.get(name)
        if resource_name is None:
            raise AttributeError(f"direct execution port is not allowed: {name}")
        return getattr(self._owner, resource_name, None)


class DirectExecutionService:
    """Execute user-directed code without bypassing policy or audit state."""

    def __init__(self, *, ports: DirectExecutionPorts) -> None:
        self._ports = ports

    async def execute_direct(
        self,
        source: str,
        *,
        language: str = "shell",
        cwd: str | None = None,
        session_id: str | None = None,
        inject_into_context: bool = True,
        on_approval=None,
    ) -> dict[str, Any]:
        """Dispatch direct code and persist its session-scoped audit record."""
        dispatcher = self._ports.dispatcher
        if dispatcher is None:
            raise RuntimeError("AthenaService not started")
        request = CapabilityRequest(
            capability_id="execute",
            arguments={
                "language": language,
                "code": source,
                **({"cwd": cwd} if cwd is not None else {}),
            },
            task_id=None,
            session_id=session_id,
            origin=CapabilityRequestOrigin.USER_DIRECT,
        )

        async def dispatch():
            return await dispatcher.dispatch(
                request,
                workspace=self._ports.default_workspace,
                profile=self._ports.config.autonomy_level,
            )

        result = await dispatch()
        if isinstance(result, SuspendedCall):
            approval_id = result.approval_id
            scopes = [scope.value for scope in result.decision.approval_scope_options]
            if not approval_id:
                return self._failed("approval required but no approval id was issued")
            if on_approval is None:
                return {
                    "approval_id": approval_id,
                    "scopes": scopes,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "approval required",
                    "status": "approval_required",
                }
            decision = on_approval(approval_id, scopes)
            if inspect.isawaitable(decision):
                decision = await decision
            granted = bool(getattr(decision, "granted", decision))
            scope = getattr(decision, "scope", None)
            await self._ports.approve(approval_id, granted=granted, scope=scope)
            if not granted:
                result = CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="denied: approval not granted",
                    metadata={"decision": "deny"},
                )
            else:
                result = await dispatch()

        if not isinstance(result, CapabilityResult):
            return self._failed("direct execute returned an invalid capability result")

        ok = result.status is CapabilityResultStatus.OK
        metadata = dict(result.metadata or {})
        output = result.output or ""
        error = result.error or ""
        await self._record_result(
            session_id=session_id,
            source=source,
            language=language,
            result=result,
            inject_into_context=inject_into_context,
        )
        return {
            "exit_code": metadata.get("exit_code", 0 if ok else 1),
            "stdout": output,
            "stderr": "" if ok else error,
            "status": "completed" if ok else "failed",
            "duration_ms": metadata.get("duration_ms"),
            "artifact_uri": result.ref_uri,
            "session_id": session_id,
        }

    @staticmethod
    def _failed(error: str) -> dict[str, Any]:
        return {"exit_code": 1, "stdout": "", "stderr": error, "status": "failed"}

    async def _record_result(
        self,
        *,
        session_id: str | None,
        source: str,
        language: str,
        result: CapabilityResult,
        inject_into_context: bool,
    ) -> None:
        """Persist a direct execution as a capability transcript message."""
        sessions = self._ports.sessions
        messages = self._ports.store_messages
        if not session_id or messages is None:
            return
        if sessions is not None and await sessions.get(session_id) is None:
            create_session = sessions.create
            kwargs: dict[str, Any] = {}
            try:
                if "principal_id" in inspect.signature(create_session).parameters:
                    kwargs["principal_id"] = self._ports.config.cache_namespace
            except (TypeError, ValueError):
                pass
            await create_session(session_id, **kwargs)
        call_id = result.call_id or new_id("call")
        await messages.append_to_session(
            session_id,
            Message(
                id=new_id("msg"),
                role=Role.CAPABILITY,
                blocks=(
                    CapabilityCallBlock(
                        call_id=call_id,
                        capability_id="execute",
                        arguments={"language": language, "code": source},
                    ),
                    CapabilityResultBlock(
                        call_id=call_id,
                        capability_id="execute",
                        ok=result.status is CapabilityResultStatus.OK,
                        output=result.output or "",
                        error=result.error,
                        metadata=dict(result.metadata or {}),
                        ref_uri=result.ref_uri,
                    ),
                ),
                created_at=utcnow(),
                provenance=Provenance(source_type=SourceType.CAPABILITY),
                metadata={
                    "session_id": session_id,
                    "direct_execution": True,
                    "inject_into_context": inject_into_context,
                },
            ),
        )
