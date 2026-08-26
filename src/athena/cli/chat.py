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
from typing import Any, AsyncIterator

from athena.protocol.events import Event, make_event
from athena.protocol.ids import new_id
from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskResult
from athena.cli.surface import ApprovalChoice, OperatorSurface

_META_HELP = """\
/help            show this help
/exit  /quit     leave the REPL
/cancel          cancel the running task
/sessions        list sessions
/new             start a fresh session
/autonomy LEVEL  set autonomy (supervised|coding|autonomous|offline)
/model POLICY    set model policy
/details         toggle detailed event activity
/permissions     show active policy grants and pending approvals
/diff            show recent file mutations (mutation ledger)
/undo MUTATION   roll back a completed mutation by id
/compact         show context-window / compression settings
/context         show what the next model turn will see
/criteria LIST   set acceptance criteria (';'-separated; 'command:' prefix = probe); bare to clear
/interrupted     list tasks parked by shutdown or crash
/resume [TASK]   re-queue an interrupted task (or the most recent one)
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


async def _cmd_permissions(service: Any) -> None:
    """Project active grants and pending approvals from canonical stores."""
    view = await service.operator_permissions()
    grants = view.get("active_grants") or []
    pending = view.get("pending") or []
    print("┌─ permissions ─────────────────────────")
    if not grants:
        print("│ no active grants")
    for g in grants:
        scope = g.get("scope") or "?"
        cap = g.get("capability") or "*"
        pattern = g.get("resource_pattern") or ""
        expires = g.get("expires_at") or "no expiry"
        line = f"│ grant {g.get('approval_id')} · {cap} · {scope}"
        if pattern:
            line += f" · {pattern}"
        print(line)
        print(f"│   expires: {expires}")
    if not pending:
        print("│ no pending approvals")
    for p in pending:
        print(f"│ pending {p.get('approval_id')} · {p.get('capability_id')}")
    print("└──────────────────────────────────────")


async def _cmd_diff(service: Any, limit: int) -> None:
    """Project the mutation ledger (grouped file-change evidence)."""
    rows = await service.operator_diff(limit=limit)
    print("┌─ mutations ───────────────────────────")
    if not rows:
        print("│ (no recorded mutations)")
    for r in rows:
        status_icon = {
            "COMPLETED": "✓",
            "FAILED": "✗",
            "ROLLED_BACK": "↩",
        }.get(r.get("status"), "·")
        reversible = "reversible" if r.get("reversible") else "one-way"
        print(
            f"│ {status_icon} {r.get('operation', '?'):10} "
            f"{r.get('resource', '?')}  [{reversible}]"
        )
        print(f"│   id: {r.get('id')}  task: {r.get('task_id') or '—'}")
    print("└──────────────────────────────────────")


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
        self.surface = OperatorSurface(details=bool(getattr(options, "details", False)))
        self.criteria: list[str] = list(
            (getattr(options, "criteria") or "").split(";")
        ) if getattr(options, "criteria", None) else []

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
        if name == "details":
            self.surface.details = not self.surface.details
            print(f"details: {'on' if self.surface.details else 'off'}")
            return True
        if name == "permissions":
            await _cmd_permissions(self.service)
            return True
        if name == "diff":
            limit = int(arg) if arg.isdigit() else 25
            await _cmd_diff(self.service, limit)
            return True
        if name == "undo":
            if not arg:
                print("usage: /undo <mutation_id>")
                return True
            outcome = await self.service.undo_mutation(arg)
            status = outcome.get("status")
            if status == "ok":
                print(f"rolled back {arg} (rollback {outcome.get('rollback_id')})")
            else:
                print(f"undo failed: {outcome.get('error', status)}")
            return True
        if name == "criteria":
            if not arg:
                self.criteria = []
                print("acceptance criteria cleared")
            else:
                self.criteria = [c.strip() for c in arg.split(";") if c.strip()]
                for i, c in enumerate(self.criteria, 1):
                    kind = "command probe" if c.lower().startswith("command:") else "model-judged"
                    print(f"  ac_{i} [{kind}] {c}")
            return True
        if name == "interrupted":
            rows = await self.service.list_interrupted()
            if not rows:
                print("(no interrupted tasks)")
            for r in rows:
                objective = (r.get("objective") or "")[:60]
                print(f"{r.get('id')}  {objective}")
            return True
        if name == "resume":
            if not self.session_id:
                self.session_id = new_id("session")
            async def _approval(approval_id: str, scopes: list[str]) -> ApprovalChoice:
                event = make_event(
                    "ApprovalRequested",
                    {"approval_id": approval_id, "capability_id": "execute", "scopes": scopes},
                    session_id=self.session_id,
                )
                await self.surface.render_event(event)
                return await self.surface.choose_approval(event)

            task_id = arg or None
            if task_id is None:
                rows = await self.service.list_interrupted()
                if not rows:
                    print("(no interrupted tasks to resume)")
                    return True
                task_id = rows[0]["id"]
                print(f"resuming most recent: {task_id}")
            from athena.cli.chat import stream_task as _stream
            spec = await self.service.resume_task(task_id)
            self._active_task_id = spec.id
            result = await _stream(self.service, spec.id, autonomy=self.autonomy, surface=self.surface)
            if result is not None:
                status = getattr(result, "status", None)
                s = status.value if hasattr(status, "value") else str(status)
                summary = getattr(result, "summary", "") or ""
                if summary:
                    print(summary)
                print(f"[task {spec.id} -> {s}]")
            self._active_task_id = None
            return True
        if name in ("compact", "context"):
            summary = await self.service.operator_context_summary(self.session_id)
            window = summary.get("window")
            reserve = summary.get("reserve_output")
            recent = summary.get("recent_verbatim_turns")
            count = summary.get("message_count")
            if name == "compact":
                print(f"context window: {window or '?'} tokens")
                print(f"output reserve: {reserve if reserve is not None else '?'} tokens")
                print(f"recent verbatim turns: {recent if recent is not None else '?'}")
                older = "compressed with provenance retained"
                print(f"older transcript: {older}")
            else:
                print(f"session: {self.session_id or '(none yet)'}")
                print(f"durable messages: {count if count is not None else '?'}")
                print("next turn includes: objective, policy boundaries, recent turns,")
                print("capability calls/results, relevant memories and skills.")
            return True
        return False

    async def _submit_task(self, line: str) -> None:
        request = AgentRequest(
            prompt=line,
            session_id=self.session_id,
            autonomy=self.autonomy,
            workspace=self.workspace,
            model_policy=_model_policy(self.model_policy),
            metadata=(
                {"acceptance_criteria": list(self.criteria)} if self.criteria else {}
            ),
        )
        spec = await self.service.submit(request, wait=False)
        self._active_task_id = spec.id
        result = await stream_task(
            self.service,
            spec.id,
            autonomy=self.autonomy,
            surface=self.surface,
        )
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

        ``inject=True`` (the ``!`` form): result is shown, recorded, and made
        available to the next model turn. ``inject=False`` (the ``!!`` form):
        result is shown and recorded for audit, but excluded from model context.
        """
        if not source.strip():
            print("empty command")
            return

        # A direct escape still belongs to the current durable session.  If
        # this is the first interaction, establish the session before the
        # service records the capability call/result.
        if self.session_id is None:
            self.session_id = new_id("session")

        async def _approval(approval_id: str, scopes: list[str]) -> ApprovalChoice:
            event = make_event(
                "ApprovalRequested",
                {
                    "approval_id": approval_id,
                    "capability_id": "execute",
                    "scopes": scopes,
                },
                session_id=self.session_id,
            )
            await self.surface.render_event(event)
            return await self.surface.choose_approval(event)

        result = await self.service.execute_direct(
            source,
            language="shell",
            session_id=self.session_id,
            inject_into_context=inject,
            on_approval=_approval,
        )
        self.surface.render_direct_execution(
            source,
            result,
            inject_into_context=inject,
        )


# ---------------------------------------------------------------------------
# Shared streaming + rendering used by chat and the `athena run` command.
# ---------------------------------------------------------------------------


async def stream_task(
    service: Any,
    task_id: Any,
    *,
    autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED,
    on_approval: Any = None,
    surface: OperatorSurface | None = None,
) -> TaskResult | None:
    """Stream a task through the operator surface until it reaches a terminal status.

    When the task enters ``WAITING_APPROVAL`` (or emits an ``ApprovalRequested``
    event) the corresponding pending approval is offered to the ``on_approval``
    callback (``(approval_id) -> bool``). Without a callback, the surface's
    scope selector is used. A ``True`` reply resolves the approval via
    ``service.approve(approval_id, granted=True)``; a ``False`` denies it.
    """
    assert task_id is not None
    surface = surface or OperatorSurface()
    events_iter: AsyncIterator[Event] = service.stream_events(task_id, after_sequence=0)
    try:
        async for event in events_iter:
            await surface.render_event(event)
            if getattr(event, "type", "") != "ApprovalRequested":
                continue
            payload = getattr(event, "payload", {}) or {}
            approval_id = payload.get("approval_id")
            if not approval_id:
                continue
            approval_key = str(approval_id)
            if surface.approval_was_handled(approval_key):
                continue
            if on_approval is not None:
                decision = await on_approval(approval_id)
                if isinstance(decision, ApprovalChoice):
                    choice = decision
                else:
                    choice = ApprovalChoice(bool(decision))
            else:
                choice = await surface.choose_approval(event)
            surface.mark_approval_handled(approval_key)
            await service.approve(
                approval_id,
                granted=choice.granted,
                scope=choice.scope,
            )
    finally:
        surface.finish()
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
