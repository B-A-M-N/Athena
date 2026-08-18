"""Interactive REPL for Project Athena.

``ChatREPL`` is a pure interface (INV-001/007): it never runs an agent loop. It
builds ``AgentRequest`` and hands it to ``AthenaService.submit``, streams the
events the service emits, renders them, then shows the final result. Meta/delegation
commands are handled here; task work always flows through the service.

Direct shell escapes (BUILDSPEC §52 / BHV-004) ``!cmd``/``!!cmd`` are executed
via ``ExecutionManager`` without routing them through the model loop.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, AsyncIterator

from athena.protocol.events import Event
from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskResult

_META_HELP = """\
/help            show this help
/exit  /quit     leave the REPL
/cancel          cancel the running task
/sessions        list sessions
/new             start a fresh session
/autonomy LEVEL  set autonomy (supervised|coding|autonomous|offline)
/model POLICY    set model policy
!cmd             execute a shell command directly
!!cmd            execute a shell command (output not injected into model context)
"""


def _autonomy(value: str | None) -> AutonomyLevel:
    if not value:
        return AutonomyLevel.SUPERVISED
    try:
        return AutonomyLevel(value.strip().lower())
    except ValueError:
        return AutonomyLevel.SUPERVISED


def _workspace_spec(root: str | None):
    if not root:
        return None
    from athena.protocol.tasks import WorkspaceSpec

    return WorkspaceSpec(id="cli", root=os.path.abspath(os.path.expanduser(root)))


def _model_policy(name: str | None):
    if not name:
        return None
    from athena.protocol.tasks import ModelPolicy

    return ModelPolicy(allowed=(name,))


class ChatREPL:
    """Interactive loop binding a user to an ``AthenaService``."""

    def __init__(self, service: Any, config: Any = None, options: Any = None) -> None:
        self.service = service
        self.config = config
        self.options = options
        self.session_id: str | None = None
        cfg_autonomy = getattr(config, "autonomy", None)
        opt_autonomy = getattr(options, "autonomy", None)
        self.autonomy = _autonomy(opt_autonomy or cfg_autonomy)
        self.model_policy: str | None = getattr(options, "model", None) or getattr(config, "model", None)
        self.workspace = _workspace_spec(getattr(options, "workspace", None))
        self._active_task_id: str | None = None

    # -- input ------------------------------------------------------------

    async def _read_line(self, prompt: str = "athena> ") -> str:
        loop = asyncio.get_running_loop()

        def _get() -> str:
            try:
                return (input(prompt) or "").strip()
            except EOFError:
                return ""

        return await loop.run_in_executor(None, _get)

    # -- main loop --------------------------------------------------------

    async def run_forever(self) -> int:
        print("Athena console (type /help for commands; Ctrl-D to exit)")
        while True:
            line = await self._read_line()
            if line == "":
                print()
                return 0
            try:
                if line.startswith("/"):
                    if await self._dispatch_meta(line):
                        continue
                elif line.startswith("!"):
                    skip = line.startswith("!!")
                    source = line[2:] if skip else line[1:]
                    await self._shell_escape(source, inject=not skip)
                else:
                    await self._submit_task(line)
            except (EOFError, KeyboardInterrupt):
                print()
                continue
        return 0

    # -- meta-commands -------------------------------------------------------

    async def _dispatch_meta(self, line: str) -> bool:
        cmd = line[1:]
        name, _, arg = cmd.partition(" ")
        name = name.lower().strip()
        arg = arg.strip()

        if name in ("exit", "quit"):
            raise SystemExit(0)
        if name == "help":
            print(_META_HELP)
            return True
        if name == "cancel":
            if self._active_task_id:
                await self.service.cancel(self._active_task_id)
                print(f"cancel requested for {self._active_task_id}")
            else:
                print("(no active task)")
            return True
        if name == "sessions":
            sessions = await self.service.list_sessions()
            if not sessions:
                print("(no sessions)")
            for s in sessions:
                sid = s.get("id") if isinstance(s, dict) else getattr(s, "id", s)
                title = (
                    s.get("objective")
                    if isinstance(s, dict)
                    else (getattr(s, "title", None) or getattr(s, "objective", "") or "")
                )
                print(f"{sid}\t{title or ''}")
            return True
        if name == "new":
            self.session_id = None
            print("(fresh session)")
            return True
        if name == "autonomy":
            self.autonomy = _autonomy(arg)
            print(f"autonomy: {self.autonomy.value}")
            return True
        if name == "model":
            self.model_policy = arg or None
            print(f"model: {self.model_policy}")
            return True
        return False

    async def _submit_task(self, line: str) -> None:
        request = AgentRequest(
            prompt=line,
            session_id=self.session_id,
            autonomy=self.autonomy,
            workspace=self.workspace,
            model_policy=_model_policy(self.model_policy),
        )
        spec = await self.service.submit(request, wait=False)
        self._active_task_id = spec.id
        result = await stream_task(self.service, spec.id, autonomy=self.autonomy)
        if result is not None:
            self.session_id = getattr(spec, "session_id", self.session_id)
            summary = getattr(result, "summary", "") or ""
            if summary:
                print()
                print(summary)
            status = getattr(result, "status", None)
            status_str = (
                status.value if status is not None and hasattr(status, "value") else str(status)
            )
            print(f"\n[task {spec.id} -> {status_str}]")
        self._active_task_id = None

    async def _shell_escape(self, source: str, inject: bool) -> None:
        """Execute a shell command directly without model inference.

        ``inject=True`` (the ``!`` form): result is shown AND recorded.
        ``inject=False`` (the ``!!`` form): result is shown only.
        """
        if not source.strip():
            print("empty command")
            return
        print(f"$ {source}")
        result = await self.service.execute_direct(
            source,
            language="shell",
            inject_into_context=inject,
        )
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        if stdout:
            print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)
        exit_code = result.get("exit_code")
        if exit_code not in (0, None):
            print(f"[exit {exit_code}]")


# ---------------------------------------------------------------------------
# Shared streaming + rendering used by chat and the `athena run` command.
# ---------------------------------------------------------------------------


async def stream_task(
    service: Any,
    task_id: Any,
    *,
    autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED,
    on_approval: Any = None,
) -> TaskResult | None:
    """Stream a task to terminal until it reaches a terminal status.

    When the task enters ``WAITING_APPROVAL`` (or emits an ``ApprovalRequested``
    event) the corresponding pending approval is offered to the ``on_approval``
    callback (``(approval_id) -> bool``). A ``True`` reply resolves the approval
    via ``service.approve(approval_id, granted=True)``; a ``False`` denies it.
    """
    assert task_id is not None
    events_iter: AsyncIterator[Event] = service.stream_events(task_id, after_sequence=0)
    async for event in events_iter:
        # Surface approval requests for the interactive user.
        if getattr(event, "type", "") == "ApprovalRequested":
            payload = getattr(event, "payload", {}) or {}
            approval_id = payload.get("approval_id")
            if approval_id and on_approval is not None:
                granted = bool(await on_approval(approval_id))
                await service.approve(approval_id, granted=granted)
        # Render common event types; skip low-noise diagnostics.
        etype = getattr(event, "type", "")
        if etype == "TaskCreated":
            pass
        elif etype == "TaskStarted":
            print(f"[task {task_id} started]")
        elif etype == "TaskInterrupted":
            print(f"[task {task_id} interrupted]")
        elif etype == "TaskCompleted":
            print(f"[task {task_id} complete]")
        elif etype == "TaskFailed":
            print(f"[task {task_id} failed]")
        elif etype == "TaskCancelled":
            print(f"[task {task_id} cancelled]")
        elif etype == "ApprovalRequested":
            payload = getattr(event, "payload", {}) or {}
            print(f"[approval requested: {payload.get('approval_id')}]")
    # Reached terminal status; return the final TaskResult.
    return await service.get_result(task_id)


def render_summary(result: Any) -> str:
    """Render a short usage/cost summary for a TaskResult."""
    if result is None:
        return ""
    usage = getattr(result, "usage", None)
    if usage is None:
        return ""
    parts = []
    if getattr(usage, "input_tokens", 0):
        parts.append(f"in={usage.input_tokens}")
    if getattr(usage, "output_tokens", 0):
        parts.append(f"out={usage.output_tokens}")
    if getattr(usage, "model_calls", 0):
        parts.append(f"calls={usage.model_calls}")
    if getattr(usage, "cost_usd", None) and usage.cost_usd > 0:
        parts.append(f"cost=${usage.cost_usd:.4f}")
    if getattr(usage, "duration_ms", 0):
        parts.append(f"{usage.duration_ms}ms")
    if getattr(usage, "executions", 0):
        parts.append(f"execs={usage.executions}")
    if getattr(usage, "mutations", 0):
        parts.append(f"muts={usage.mutations}")
    return "  ".join(parts)
