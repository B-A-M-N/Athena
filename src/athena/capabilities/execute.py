"""``execute`` capability.

The OI-derived universal computation primitive (BUILDSPEC 46, BHV-056). It
bridges the model's ``execute(language, code)`` request to the ExecutionManager
and its persistent runtimes (shell, python). Execution remains behind
``ExecutionManager`` (INV-005) and policy (INV-004).
"""

from __future__ import annotations

import os
import hashlib
import shutil
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from athena.execution.diagnostics import normalize_diagnostics
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
    ExecutionEventType,
    ExecutionExitStatus,
    ExecutionRequest,
)
from athena.protocol.tasks import NetworkPolicy

_LANGUAGES = ("python", "shell", "bash", "sh", "zsh", "node", "powershell", "py")

_INPUT_SCHEMA = {
    "type": "object",
    "required": ["language", "code"],
    "additionalProperties": False,
    "properties": {
        "language": {"type": "string", "enum": list(_LANGUAGES)},
        "code": {"type": "string", "maxLength": 10_000_000},
        "session": {"type": "string", "minLength": 1, "maxLength": 128},
        "cwd": {"type": "string", "maxLength": 4096},
        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
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

    def __init__(
        self,
        execution_manager,
        workspace=None,
        event_sink=None,
        artifact_store=None,
        failure_memory=None,
    ) -> None:
        self.execution_manager = execution_manager
        self.workspace = workspace
        self._event_sink = event_sink
        self._artifact_store = artifact_store
        self._failure_memory = failure_memory

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        output_accumulator=None,
        context: "InvocationContext | None" = None,
    ) -> CapabilityResult:
        args = request.arguments or {}
        language = (args.get("language") or "sh").lower()
        if language not in _LANGUAGES:
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=f"unsupported language: {language}",
            )
        code = args.get("code", "")
        runtime_name = _map_runtime(language)
        if runtime_name not in self.execution_manager.available_runtimes():
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=f"runtime unavailable: {language}",
            )

        ws = context.workspace if context else self.workspace
        if ws is None:
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error="no workspace bound",
            )
        execution_task_id = request.task_id or f"direct:{request.call_id}"
        try:
            cwd = self._resolve_cwd(ws, args.get("cwd"))
        except ValueError as exc:
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=str(exc),
            )

        # Validate + honor timeout (safety/resource-control promise)
        timeout = None
        if args.get("timeout") is not None:
            try:
                timeout_sec = float(args["timeout"])
            except (TypeError, ValueError):
                return CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="timeout must be a number (seconds)",
                )
            if timeout_sec <= 0:
                return CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="timeout must be positive (seconds)",
                )
            timeout = timedelta(seconds=timeout_sec)

        # A capability call cannot outlive its task.  The kernel supplies the
        # cumulative remaining runtime; direct callers still get an absolute
        # deadline/resource ceiling from the invocation context.
        hard_remaining = context.runtime_remaining_s if context else None
        if hard_remaining is None and context is not None and context.deadline is not None:
            now = datetime.now(context.deadline.tzinfo or timezone.utc)
            hard_remaining = (context.deadline - now).total_seconds()
        if hard_remaining is None and context is not None:
            max_wall = getattr(context.resource_budget, "max_wall_time", None)
            if max_wall is not None:
                hard_remaining = max_wall.total_seconds()
        if hard_remaining is not None:
            if hard_remaining <= 0:
                return CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="task deadline or wall-time budget exceeded",
                )
            hard_timeout = timedelta(seconds=hard_remaining)
            if timeout is None or hard_timeout < timeout:
                timeout = hard_timeout

        # session (runtime_session_id) must be validated for task ownership
        runtime_session_id = args.get("session")
        if runtime_session_id is not None:
            if not self.execution_manager.is_session_owned_by_task(
                runtime_session_id, request.task_id
            ):
                return CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=f"session {runtime_session_id} is not owned by task {request.task_id}",
                )

        task_policy = getattr(context, "capability_policy", None) if context else None
        network_authorized = bool(
            task_policy
            and {
                EffectClass.NETWORK_READ.value,
                EffectClass.NETWORK_WRITE.value,
                EffectClass.NETWORK_READ,
                EffectClass.NETWORK_WRITE,
            }.intersection(set(task_policy.effects or ()))
        )
        requested_network = getattr(ws.network_policy, "value", ws.network_policy)
        effective_network = (
            ws.network_policy
            if network_authorized and requested_network != NetworkPolicy.DENY.value
            else NetworkPolicy.DENY
        )
        writable_rules = tuple(getattr(ws, "writable", ()) or ())
        workspace_root = os.path.realpath(os.path.abspath(ws.root))
        exec_req = ExecutionRequest(
            runtime=runtime_name,
            source=code,
            task_id=execution_task_id,
            workspace_id=ws.id,
            backend=ws.execution_backend or "local",
            cwd=cwd,
            runtime_session_id=runtime_session_id,
            timeout=timeout,
            network_policy=effective_network,
            workspace_root=workspace_root,
            # Candidate verification must import the candidate tree, while
            # never inheriting a host PYTHONPATH that could leak unrelated
            # code into the proof environment. A self-host task additionally
            # supplies its trusted, host-resolved frozen environment.
            env=(
                verification_environment.for_workspace(workspace_root)
                if (verification_environment := getattr(context, "verification_environment", None))
                is not None
                else _candidate_python_environment(workspace_root)
            ),
            writable_paths=(
                None
                if not writable_rules
                else _canonical_workspace_rules(ws, workspace_root, allow=True)
            ),
            read_only_paths=_canonical_workspace_rules(ws, workspace_root, allow=False),
            toolchain_paths=(
                verification_environment.readonly_mounts
                if verification_environment is not None
                else _trusted_toolchain_paths(ws)
            ),
            writable_toolchain_paths=(
                verification_environment.writable_mounts
                if verification_environment is not None
                else ()
            ),
        )
        execution_id = _new_id()

        # Use streaming to capture full output for artifactization
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_status: ExecutionExitStatus | None = None
        exit_code: int | None = None
        timed_out = False
        interrupted = False
        execution_metadata: dict[str, object] = {}

        async for event in self.execution_manager.stream(exec_req, execution_id):
            if event.type == ExecutionEventType.STARTED:
                execution_metadata.update(dict(event.metadata or {}))
            elif event.type == ExecutionEventType.STDOUT:
                data = event.data or ""
                stdout_parts.append(data)
                if self._event_sink:
                    await self._event_sink("stdout", data)
                if output_accumulator is not None:
                    await output_accumulator.chunk(data, stream="stdout")
            elif event.type == ExecutionEventType.STDERR:
                data = event.data or ""
                stderr_parts.append(data)
                if self._event_sink:
                    await self._event_sink("stderr", data)
                if output_accumulator is not None:
                    await output_accumulator.chunk(data, stream="stderr")
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
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                output=stdout,
                error="execution timed out",
                metadata=_execution_metadata(execution_metadata),
            )
        if interrupted:
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                output=stdout,
                error="execution interrupted",
                metadata=_execution_metadata(execution_metadata),
            )

        combined = stdout + ("\n" + stderr if stderr else "")
        diagnostics = normalize_diagnostics(combined, tool=language, cwd=cwd)

        # Artifactize large output: bounded preview inline, full in artifact
        max_inline = 8 * 1024  # 8KB inline preview
        ref_uri = None
        result_metadata: dict[str, object] = {
            "diagnostics": [diagnostic.to_dict() for diagnostic in diagnostics],
            "diagnostic_count": len(diagnostics),
            "failure_environment_fingerprint": _failure_environment(
                runtime_name,
                ws,
                backend_metadata=execution_metadata,
            ),
        }
        result_metadata.update(_execution_metadata(execution_metadata))
        if len(combined) > max_inline and self._artifact_store is not None:
            try:
                artifact = await self._artifact_store.save(
                    task_id=request.task_id,
                    content=combined.encode("utf-8"),
                    mime_type="text/plain",
                    producer="execute",
                )
                ref_uri = artifact.uri
                combined = (
                    combined[:max_inline] + f"\n…[truncated, full output in artifact {ref_uri}]"
                )
            except Exception as exc:
                # Preserve the complete output and make the degraded
                # observability state explicit.  Silently pretending that a
                # durable follow-up artifact exists is worse than returning a
                # large result because it makes later inspection impossible
                # to diagnose.
                result_metadata["artifactization_error"] = str(exc)
                return CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    output=combined[:max_inline],
                    error=f"required output artifact could not be saved: {exc}",
                    metadata=result_metadata,
                )
        elif len(combined) > max_inline:
            result_metadata["artifactization"] = "unavailable"

        if exit_code not in (0, None):
            await self._remember_failures(
                diagnostics,
                request=request,
                workspace=ws,
                runtime=runtime_name,
                error=stderr or f"exit code {exit_code}",
                backend_metadata=execution_metadata,
            )
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                output=combined,
                error=stderr or f"exit code {exit_code}",
                ref_uri=ref_uri,
                metadata={"exit_code": exit_code, **result_metadata},
            )
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output=combined,
            ref_uri=ref_uri,
            metadata=result_metadata,
        )

    async def _remember_failures(
        self,
        diagnostics,
        *,
        request,
        workspace,
        runtime: str,
        error: str,
        backend_metadata: Mapping[str, object] | None = None,
    ) -> None:
        if self._failure_memory is None:
            return
        environment = _failure_environment(
            runtime,
            workspace,
            backend_metadata=backend_metadata,
        )
        for diagnostic in diagnostics:
            try:
                await self._failure_memory.record(
                    signature_fingerprint=diagnostic.signature_fingerprint,
                    capability_id="execute",
                    environment_fingerprint=environment,
                    project_scope=workspace.id,
                    strategy={
                        "kind": "execution-diagnostic",
                        "tool": diagnostic.tool,
                        "code": diagnostic.code,
                    },
                    remediation={"error": error, "status": "advisory"},
                    success=False,
                )
            except Exception:
                # Failure memory is observability. It must never turn the
                # underlying execution result into a different failure.
                continue

    @staticmethod
    def _resolve_cwd(ws, cwd: str | None) -> str | None:
        """Resolve the execution cwd against the task workspace (INV-008).

        A relative cwd is joined to the workspace root; an absolute cwd is
        required to stay inside the workspace root. Invalid paths fail closed
        rather than falling back to a runtime-dependent host cwd.
        """
        if not cwd:
            return None
        root = os.path.realpath(os.path.abspath(ws.root))
        if os.path.isabs(cwd):
            candidate = os.path.realpath(os.path.abspath(cwd))
        else:
            candidate = os.path.realpath(os.path.abspath(os.path.join(ws.root, cwd)))
        if candidate != root and not candidate.startswith(root + os.sep):
            raise ValueError(f"cwd outside workspace: {cwd}")
        return candidate


def _failure_environment(
    runtime: str,
    workspace,
    *,
    backend_metadata: Mapping[str, object] | None = None,
) -> str:
    identity = (
        f"{runtime}\x1f{workspace.execution_backend or 'local'}\x1f{workspace.network_policy.value}"
    )
    image_digest = str((backend_metadata or {}).get("image_digest") or "")
    if image_digest:
        identity += f"\x1f{image_digest}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _execution_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    """Expose backend identity in the execution proof without raw internals."""
    selected = {
        key: metadata[key]
        for key in ("backend", "image", "image_digest", "container_id")
        if key in metadata
    }
    return {"execution_environment": selected} if selected else {}


def _map_runtime(language: str) -> str:
    aliases = {
        "python": "python",
        "py": "python",
        "python3": "python",
        "shell": "shell",
        "bash": "shell",
        "sh": "shell",
        "zsh": "shell",
        "powershell": "powershell",
        "pwsh": "powershell",
        "ps1": "powershell",
        "cmd": "powershell",
        "node": "node",
        "nodejs": "node",
        "js": "node",
    }
    base = aliases.get(language, language)
    return base if base in ("python", "shell", "node", "powershell") else "shell"


def _language_name(language: str) -> str:
    return _map_runtime(language)


def _canonical_workspace_rules(ws, root: str, *, allow: bool) -> tuple[str, ...]:
    """Convert workspace-relative rules to contained host mount paths."""
    paths: list[str] = []
    for rule in tuple(getattr(ws, "writable", ()) or ()):
        if bool(rule.allow) is not allow:
            continue
        raw = os.fspath(rule.path)
        candidate = os.path.realpath(
            os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw))
        )
        if candidate == root or candidate.startswith(root + os.sep):
            if candidate not in paths:
                paths.append(candidate)
    return tuple(paths)


def _candidate_python_environment(workspace_root: str) -> dict[str, str]:
    """Return the minimal project import environment for a routed workspace."""
    source_root = os.path.join(workspace_root, "src")
    env = {"PYTHONDONTWRITEBYTECODE": "1"}
    if os.path.isdir(source_root):
        env["PYTHONPATH"] = source_root
    return env


def _trusted_toolchain_paths(workspace) -> tuple[str, ...]:
    """Select exact read-only verification tools, never an arbitrary HOME."""
    backend = getattr(workspace, "execution_backend", None) or "local"
    if backend not in {"shadow", "verification", "sandbox"}:
        return ()
    paths: list[str] = []
    uv = shutil.which("uv")
    if uv:
        paths.append(os.path.realpath(uv))
    return tuple(dict.fromkeys(paths))


def _new_id() -> str:
    from athena.protocol.ids import new_id

    return new_id("exec")


__all__ = ["ExecuteCapability"]
