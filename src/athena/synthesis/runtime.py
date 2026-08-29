"""Parent-side host bridge for generated capability composition.

Generated Python runs in a restricted child process.  It never receives a
dispatcher, filesystem handle, or subprocess handle.  ``GeneratedToolHost``
is the narrow parent-side object that turns a framed child request into the
same canonical capability dispatch used by model calls.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from athena.capabilities.dispatcher import SuspendedCall
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResultStatus,
    DispatchDirectives,
    EffectClass,
)
from athena.protocol.ids import new_id
from athena.execution.process_tree import kill_tree_async
from athena.protocol.tasks import CapabilityPolicy, ResourceBudget, WorkspaceSpec

__all__ = [
    "GeneratedHostError",
    "GeneratedToolHost",
    "PersistentGeneratedSession",
]


class GeneratedHostError(RuntimeError):
    """A governed host call could not be completed for generated code."""


class PersistentGeneratedSession:
    """One serialized, sandboxed generated-process session.

    The session owns no Athena authority. It only keeps the generated module
    globals alive between calls; each host request still goes through the
    parent-side :class:`GeneratedToolHost` and canonical dispatcher.
    """

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self._argv = tuple(argv)
        self._env = dict(env)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr: list[bytes] = []
        self._lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._process is None

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(  # architecture-lint: allow subprocess-outside-approved-backends reason=generated runtime worker
            *self._argv,
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._read_stderr(self._process.stderr))

    async def invoke(
        self,
        payload: str,
        host: "GeneratedToolHost | None",
        *,
        timeout: float,
    ) -> tuple[str, str, int]:
        async with self._lock:
            await self.start()
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                return "", "persistent generated runtime is unavailable", 1
            stderr_start = len(self._stderr)
            try:
                process.stdin.write((payload + "\n").encode())
                await process.stdin.drain()
                output: list[bytes] = []
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    remaining = max(0.01, deadline - asyncio.get_running_loop().time())
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=remaining,
                    )
                    if not line:
                        stderr = self._stderr_text(stderr_start)
                        await self._terminate_unlocked()
                        return (
                            b"".join(output).decode("utf-8", errors="replace"),
                            (stderr or "persistent generated runtime exited"),
                            1,
                        )
                    if line.startswith(b"__HOST__"):
                        response = await self._host_response(line, host)
                        process.stdin.write((json.dumps(response) + "\n").encode())
                        await process.stdin.drain()
                        continue
                    if line.startswith(b"__RESULT__"):
                        output.append(line)
                        return (
                            b"".join(output).decode("utf-8", errors="replace"),
                            self._stderr_text(stderr_start),
                            0,
                        )
                    if line.startswith(b"__ERROR__"):
                        error = line[len(b"__ERROR__") :].decode("utf-8", errors="replace").strip()
                        try:
                            error = str(json.loads(error).get("error") or error)
                        except (TypeError, ValueError):
                            pass
                        return (
                            b"".join(output).decode("utf-8", errors="replace"),
                            error or self._stderr_text(stderr_start) or "generated run failed",
                            1,
                        )
                    output.append(line)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionError) as exc:
                await self._terminate_unlocked()
                return "", f"persistent generated runtime failed: {exc}", 1
            except asyncio.CancelledError:
                await self._terminate_unlocked()
                raise

    async def close(self) -> None:
        async with self._lock:
            await self._terminate_unlocked()

    async def _terminate_unlocked(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            await kill_tree_async(process, timeout=1.0)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def _host_response(
        self,
        line: bytes,
        host: "GeneratedToolHost | None",
    ) -> dict[str, Any]:
        try:
            request = json.loads(line[len(b"__HOST__") :])
            if host is None:
                raise GeneratedHostError("generated host is unavailable in this context")
            value = await host.call(request["capability_id"], request["arguments"])
            return {"ok": True, "value": value}
        except Exception as exc:  # noqa: BLE001 - child receives a failed call
            return {"ok": False, "error": str(exc)}

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                self._stderr.append(chunk)
        except asyncio.CancelledError:
            return

    def _stderr_text(self, start: int) -> str:
        return b"".join(self._stderr[start:]).decode("utf-8", errors="replace")


@dataclass
class GeneratedToolHost:
    """Mediated host API exposed to one generated-tool invocation.

    The object lives in the parent process.  The child sees only the framed
    request/response protocol implemented by the synthesis runtime.  Every
    call therefore gets a fresh request id and traverses the dispatcher,
    including schema validation, task policy, approvals, RealityGate,
    resource accounting, mutation recording, and events.
    """

    dispatcher: Any
    workspace: WorkspaceSpec
    task_id: str | None
    session_id: str | None = None
    profile: str | None = None
    task_policy: CapabilityPolicy | None = None
    task_budget: ResourceBudget | None = None
    max_calls: int = 32
    call_depth: int = 0
    call_chain: tuple[str, ...] = ()
    max_depth: int = 4
    # ``None`` is the validation/inference mode. A live generated executor
    # receives the immutable capability set observed during validation.
    allowed_capabilities: frozenset[str] | None = None
    inherited_effects: frozenset[EffectClass] = frozenset()
    inherited_capability_id: str | None = None
    _calls: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, capability_id: str, arguments: Any) -> Any:
        """Dispatch one native Athena capability and return its JSON value."""
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise GeneratedHostError("host capability_id must be a non-empty string")
        if not isinstance(arguments, dict):
            raise GeneratedHostError("host capability arguments must be an object")
        if self.allowed_capabilities is not None and capability_id not in self.allowed_capabilities:
            raise GeneratedHostError(
                f"host capability {capability_id!r} was not declared for this tool"
            )
        if self.call_depth >= self.max_depth:
            raise GeneratedHostError(f"generated host call depth exceeded ({self.max_depth})")
        if capability_id in self.call_chain:
            raise GeneratedHostError(f"generated host call cycle detected for {capability_id!r}")
        if self._calls >= self.max_calls:
            raise GeneratedHostError(f"generated host call budget exceeded ({self.max_calls})")
        self._calls += 1
        request = CapabilityRequest(
            capability_id=capability_id,
            arguments=arguments,
            task_id=self.task_id,
            session_id=self.session_id,
            call_id=new_id("generated-host"),
            # Generated code is explicitly untrusted protocol input. It is
            # not model-repaired, and native capabilities may use this origin
            # to reject authority reserved for a user/system call.
            origin=CapabilityRequestOrigin.GENERATED,
        )
        try:
            result = await self.dispatcher.dispatch(
                request,
                workspace=self.workspace,
                profile=self.profile,
                task_policy=self.task_policy,
                task_budget=self.task_budget,
                _generated_call_depth=self.call_depth + 1,
                _generated_call_chain=(*self.call_chain, capability_id),
                _directives=DispatchDirectives(
                    inherited_effects=self.inherited_effects,
                    inherited_capability_id=self.inherited_capability_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - child boundary returns a failed call
            self.calls.append(
                {
                    "capability_id": capability_id,
                    "arguments": dict(arguments),
                    "status": CapabilityResultStatus.FAILED.value,
                    "error": str(exc),
                }
            )
            raise GeneratedHostError(f"host call {capability_id!r} raised: {exc}") from exc
        if isinstance(result, SuspendedCall):
            self.calls.append(
                {
                    "capability_id": capability_id,
                    "arguments": dict(arguments),
                    "status": "suspended",
                }
            )
            raise GeneratedHostError(
                "host call requires approval and cannot suspend generated execution"
            )
        if result.status is not CapabilityResultStatus.OK:
            self.calls.append(
                {
                    "capability_id": capability_id,
                    "arguments": dict(arguments),
                    "status": result.status.value,
                    "error": result.error or "unknown error",
                }
            )
            raise GeneratedHostError(
                f"host call {capability_id!r} failed: {result.error or 'unknown error'}"
            )
        self.calls.append(
            {
                "capability_id": capability_id,
                "arguments": dict(arguments),
                "status": result.status.value,
                "metadata": dict(result.metadata or {}),
            }
        )
        if not result.output:
            return None
        try:
            return json.loads(result.output)
        except json.JSONDecodeError:
            # Native capabilities may intentionally return plain text. Keep
            # that value JSON-compatible for the child protocol.
            return result.output
