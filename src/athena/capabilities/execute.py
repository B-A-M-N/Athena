"""``execute`` capability.

The OI-derived universal computation primitive (BUILDSPEC 46, BHV-056). It
bridges the model's ``execute(language, code)`` request to the ExecutionManager
and its persistent runtimes (shell, python). Execution remains behind
``ExecutionManager`` (INV-005) and policy (INV-004).
"""
from __future__ import annotations

import os
from datetime import timedelta

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    InvocationContext,
)
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
    ExecutionRequest,
)

_LANGUAGES = ("python", "shell", "bash", "sh", "zsh", "node", "powershell", "py")

_INPUT_SCHEMA = {
    "type": "object",
    "required": ["language", "code"],
    "properties": {
        "language": {"type": "string", "enum": list(_LANGUAGES)},
        "code": {"type": "string"},
        "session": {"type": "string"},
        "cwd": {"type": "string"},
        "timeout": {"type": "number"},
    },
}


class ExecuteCapability:
    descriptor = CapabilityDescriptor(
        id="execute",
        description=(
            "Universal computation: run code in a language on the local system. "
            "Persistent sessions preserve state across executions (e.g. python "
            "or shell). Operations: execute."
        ),
        input_schema=_INPUT_SCHEMA,
        effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, execution_manager, workspace=None, event_sink=None, artifact_store=None) -> None:
        self.execution_manager = execution_manager
        self.workspace = workspace
        self._event_sink = event_sink
        self._artifact_store = artifact_store

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        output_accumulator=None,
        context: "InvocationContext | None" = None,
    ) -> CapabilityResult:
        args = request.arguments or {}
        language = (args.get("language") or "sh").lower()
        code = args.get("code", "")
        runtime_name = _map_runtime(language)
        if runtime_name not in self.execution_manager.available_runtimes():
            return CapabilityResult(
                request.call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error=f"runtime unavailable: {language}",
            )

        ws = context.workspace if context else self.workspace
        if ws is None:
            return CapabilityResult(
                request.call_id, request.capability_id,
                CapabilityResultStatus.FAILED,
                error="no workspace bound",
            )
        cwd = self._resolve_cwd(ws, args.get("cwd"))

        # Validate + honor timeout (safety/resource-control promise)
        timeout = None
        if args.get("timeout") is not None:
            try:
                timeout_sec = float(args["timeout"])
            except (TypeError, ValueError):
                return CapabilityResult(
                    request.call_id, request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="timeout must be a number (seconds)",
                )
            if timeout_sec <= 0:
                return CapabilityResult(
                    request.call_id, request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="timeout must be positive (seconds)",
                )
            timeout = timedelta(seconds=timeout_sec)

        # session (runtime_session_id) must be validated for task ownership
        runtime_session_id = args.get("session")
        if runtime_session_id is not None:
            if not self.execution_manager.is_session_owned_by_task(
                runtime_session_id, request.task_id
            ):
                return CapabilityResult(
                    request.call_id, request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=f"session {runtime_session_id} is not owned by task {request.task_id}",
                )

        exec_req = ExecutionRequest(
            runtime=runtime_name,
            source=code,
            task_id=request.task_id,
            workspace_id=ws.id,
            cwd=cwd,
            runtime_session_id=runtime_session_id,
            timeout=timeout,
        )
        execution_id = _new_id()

        # Use streaming to capture full output for artifactization
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_status: ExecutionExitStatus | None = None
        exit_code: int | None = None
        timed_out = False
        interrupted = False

        async for event in self.execution_manager.stream(exec_req, execution_id):
            if event.type == ExecutionEventType.STDOUT:
                data = event.data or ""
                stdout_parts.append(data)
                if self._event_sink:
                    await self._event_sink("stdout", data)
            elif event.type == ExecutionEventType.STDERR:
                data = event.data or ""
                stderr_parts.append(data)
                if self._event_sink:
                    await self._event_sink("stderr", data)
            elif event.type == ExecutionEventType.EXITED:
                exit_status = event.exit_status
                exit_code = event.exit_code
                if exit_status == ExecutionExitStatus.TIMED_OUT:
                    timed_out = True
                elif exit_status == ExecutionExitStatus.INTERRUPTED:
                    interrupted = True

        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)

        if timed_out:
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.FAILED,
                output=stdout, error="execution timed out",
            )
        if interrupted:
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.FAILED,
                output=stdout, error="execution interrupted",
            )

        combined = stdout + ("\n" + stderr if stderr else "")

        # Artifactize large output: bounded preview inline, full in artifact
        max_inline = 8 * 1024  # 8KB inline preview
        ref_uri = None
        if len(combined) > max_inline and self._artifact_store is not None:
            try:
                artifact = await self._artifact_store.save(
                    task_id=request.task_id,
                    content=combined.encode("utf-8"),
                    mime_type="text/plain",
                    producer="execute",
                )
                ref_uri = artifact.uri
                combined = combined[:max_inline] + f"\n…[truncated, full output in artifact {ref_uri}]"
            except Exception:
                pass

        if exit_code not in (0, None):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.FAILED,
                output=combined, error=stderr or f"exit code {exit_code}",
                ref_uri=ref_uri,
                metadata={"exit_code": exit_code},
            )
        return CapabilityResult(
            request.call_id, request.capability_id, CapabilityResultStatus.OK,
            output=combined,
            ref_uri=ref_uri,
        )

    @staticmethod
    def _resolve_cwd(ws, cwd: str | None) -> str | None:
        """Resolve the execution cwd against the task workspace (INV-008).

        A relative cwd is joined to the workspace root; an absolute cwd is
        required to stay inside the workspace root, otherwise ``None`` (the
        runtime's default) is used so execution never escapes the workspace.
        """
        if not cwd:
            return None
        root = os.path.realpath(os.path.abspath(ws.root))
        if os.path.isabs(cwd):
            candidate = os.path.realpath(os.path.abspath(cwd))
        else:
            candidate = os.path.realpath(os.path.abspath(os.path.join(ws.root, cwd)))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate


def _map_runtime(language: str) -> str:
    aliases = {
        "python": "python", "py": "python", "python3": "python",
        "shell": "shell", "bash": "shell", "sh": "shell", "zsh": "shell",
        "powershell": "powershell", "pwsh": "powershell", "ps1": "powershell",
        "cmd": "powershell",
        "node": "node", "nodejs": "node", "js": "node",
    }
    base = aliases.get(language, language)
    return base if base in ("python", "shell", "node", "powershell") else "shell"


def _language_name(language: str) -> str:
    return _map_runtime(language)


def _new_id() -> str:
    from athena.protocol.ids import new_id
    return new_id("exec")


__all__ = ["ExecuteCapability"]
