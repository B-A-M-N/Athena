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
/scroll PANE DIRECTION  inspect conversation/OI history (up|down|bottom)
/permissions     show active policy grants and pending approvals
/diff            show recent file mutations (mutation ledger)
/undo MUTATION   roll back a completed mutation by id
/compact         show context-window / compression settings
/context         show what the next model turn will see
/criteria LIST   set acceptance criteria (';'-separated; 'command:' prefix = probe); bare to clear
/interrupted     list tasks parked by shutdown or crash
/resume [TASK]   re-queue an interrupted task (or the most recent one)
/mascot [NAME]   list or switch the mascot/buddy ('off' hides it)
!cmd             execute a shell command directly
!!cmd            execute a shell command (output not injected into model context)
"""


def bold(text: str) -> str:
    """ANSI style helper used by the inspect projection."""
    return f"\033[1m{text}\033[0m"


def dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


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


def _model_label(config: Any = None) -> str:
    """Give the instrument rail the configured provider/model identity."""
    providers = tuple(getattr(config, "providers", ()) or ())
    if providers:
        provider = providers[0]
        return (f"{getattr(provider, 'name', 'model')} / {getattr(provider, 'model', '')}").rstrip(
            " /"
        )
    return os.environ.get("OPENROUTER_MODEL", "local / fake-1")


async def _cmd_permissions(service: Any, surface: OperatorSurface) -> None:
    """Project active grants and pending approvals from canonical stores."""
    view = await service.operator_permissions()
    grants = view.get("active_grants") or []
    pending = view.get("pending") or []
    surface.render_notice("┌─ permissions ─────────────────────────")
    if not grants:
        surface.render_notice("│ no active grants")
    for g in grants:
        scope = g.get("scope") or "?"
        cap = g.get("capability") or "*"
        pattern = g.get("resource_pattern") or ""
        expires = g.get("expires_at") or "no expiry"
        line = f"│ grant {g.get('approval_id')} · {cap} · {scope}"
        if pattern:
            line += f" · {pattern}"
        surface.render_notice(line)
        surface.render_notice(f"│   expires: {expires}")
    if not pending:
        surface.render_notice("│ no pending approvals")
    for p in pending:
        surface.render_notice(f"│ pending {p.get('approval_id')} · {p.get('capability_id')}")
    surface.render_notice("└──────────────────────────────────────")


async def _cmd_diff(service: Any, limit: int, surface: OperatorSurface) -> None:
    """Project the mutation ledger (grouped file-change evidence)."""
    rows = await service.operator_diff(limit=limit)
    surface.render_notice("┌─ mutations ───────────────────────────")
    if not rows:
        surface.render_notice("│ (no recorded mutations)")
    for r in rows:
        status_icon = {
            "COMPLETED": "✓",
            "FAILED": "✗",
            "ROLLED_BACK": "↩",
        }.get(r.get("status"), "·")
        reversible = "reversible" if r.get("reversible") else "one-way"
        surface.render_notice(
            f"│ {status_icon} {r.get('operation', '?'):10} {r.get('resource', '?')}  [{reversible}]"
        )
        surface.render_notice(f"│   id: {r.get('id')}  task: {r.get('task_id') or '—'}")
    surface.render_notice("└──────────────────────────────────────")


def _surface_class():
    """Prefer the dual-pane surface; degrade gracefully if unavailable."""
    try:
        from athena.cli.dual_pane import DualPaneSurface

        return DualPaneSurface
    except Exception:
        from athena.cli.surface import OperatorSurface

        return OperatorSurface


def _make_surface(
    *,
    details: bool = False,
    mascot: str | None = None,
    display: str | None = None,
    model_label: str | None = None,
    animations: bool = True,
    reduced_motion: bool = False,
):
    """Build the preferred surface, forwarding the mascot choice when the
    surface supports one (the plain OperatorSurface has no mascot column)."""
    surface_cls = _surface_class()
    if surface_cls.__name__ == "DualPaneSurface":
        return surface_cls(
            details=details,
            mascot=mascot,
            display=display,
            model_label=model_label,
            animations=animations,
            reduced_motion=reduced_motion,
        )
    return surface_cls(details=details)


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
        self.model_policy: str | None = getattr(options, "model", None) or getattr(
            config, "model", None
        )
        self.workspace = _workspace_spec(getattr(options, "workspace", None))
        self._active_task_id: str | None = None
        option_animations = getattr(options, "animations", None)
        config_animations = getattr(config, "animations", True)
        self.surface = _make_surface(
            details=bool(getattr(options, "details", False)),
            mascot=getattr(options, "mascot", None) or getattr(config, "mascot", None),
            display=getattr(options, "display", None) or getattr(config, "display", None),
            model_label=_model_label(config),
            animations=bool(config_animations if option_animations is None else option_animations),
            reduced_motion=bool(
                getattr(options, "reduced_motion", False)
                or getattr(config, "reduced_motion", False)
            ),
        )
        self.criteria: list[str] = (
            list((getattr(options, "criteria") or "").split(";"))
            if getattr(options, "criteria", None)
            else []
        )

    # -- input ------------------------------------------------------------

    async def _read_line(self, prompt: str = "athena> ") -> str:
        loop = asyncio.get_running_loop()

        def _get() -> str:
            try:
                reader = getattr(self.surface, "read_prompt", None)
                if callable(reader):
                    return (reader(prompt) or "").strip()
                return (self.surface._input_fn(prompt) or "").strip()
            except EOFError:
                return ""

        return await loop.run_in_executor(None, _get)

    # -- main loop --------------------------------------------------------

    async def run_forever(self) -> int:
        opener = getattr(self.surface, "open", None)
        closer = getattr(self.surface, "close", None)
        if callable(opener):
            opener()
        try:
            return await self._run_loop()
        finally:
            async_closer = getattr(self.surface, "aclose", None)
            if callable(async_closer):
                await async_closer()
            elif callable(closer):
                closer()

    async def _run_loop(self) -> int:
        self.surface.render_idle()
        while True:
            line = await self._read_line()
            if line == "":
                self.surface.render_notice("")
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
            except EOFError:
                self.surface.render_notice("")
                continue
            except KeyboardInterrupt:
                task_id = self._active_task_id
                if task_id:
                    try:
                        await self.service.cancel(task_id)
                    except Exception as exc:
                        self.surface.render_notice(f"Cancellation could not be requested: {exc}")
                    else:
                        self.surface.render_notice(
                            f"Cancellation requested for {task_id}.",
                            status="INTERRUPTED",
                        )
                    self._active_task_id = None
                else:
                    self.surface.render_notice("")
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
            self.surface.render_notice(_META_HELP)
            return True
        if name == "cancel":
            if self._active_task_id:
                await self.service.cancel(self._active_task_id)
                self.surface.render_notice(f"cancel requested for {self._active_task_id}")
            else:
                self.surface.render_notice("(no active task)")
            return True
        if name == "sessions":
            sessions = await self.service.list_sessions()
            if not sessions:
                self.surface.render_notice("(no sessions)")
            for s in sessions:
                sid = s.get("id") if isinstance(s, dict) else getattr(s, "id", s)
                title = (
                    s.get("objective")
                    if isinstance(s, dict)
                    else (getattr(s, "title", None) or getattr(s, "objective", "") or "")
                )
                self.surface.render_notice(f"{sid}\t{title or ''}")
            return True
        if name == "new":
            self.session_id = None
            self.surface.render_notice("(fresh session)")
            return True
        if name == "autonomy":
            self.autonomy = _autonomy(arg)
            self.surface.render_notice(f"autonomy: {self.autonomy.value}")
            return True
        if name == "model":
            self.model_policy = arg or None
            self.surface.render_notice(f"model: {self.model_policy}")
            return True
        if name == "details":
            self.surface.details = not self.surface.details
            self.surface.render_notice(
                f"details: {'expanded' if self.surface.details else 'collapsed'}"
            )
            repaint = getattr(self.surface, "repaint_oi", None)
            if callable(repaint):
                repaint(force=True)
            return True
        if name == "scroll":
            self._cmd_scroll(arg)
            return True
        if name == "mascot":
            self._cmd_mascot(arg)
            return True
        if name == "permissions":
            await _cmd_permissions(self.service, self.surface)
            return True
        if name == "diff":
            limit = int(arg) if arg.isdigit() else 25
            await _cmd_diff(self.service, limit, self.surface)
            return True
        if name == "undo":
            if not arg:
                self.surface.render_notice("usage: /undo <mutation_id>")
                return True
            outcome = await self.service.undo_mutation(arg)
            status = outcome.get("status")
            if status == "ok":
                self.surface.render_notice(
                    f"rolled back {arg} (rollback {outcome.get('rollback_id')})"
                )
            else:
                self.surface.render_notice(f"undo failed: {outcome.get('error', status)}")
            return True
        if name == "criteria":
            if not arg:
                self.criteria = []
                self.surface.render_notice("acceptance criteria cleared")
            else:
                self.criteria = [c.strip() for c in arg.split(";") if c.strip()]
                for i, c in enumerate(self.criteria, 1):
                    kind = "command probe" if c.lower().startswith("command:") else "model-judged"
                    self.surface.render_notice(f"  ac_{i} [{kind}] {c}")
            return True
        if name == "interrupted":
            rows = await self.service.list_interrupted()
            if not rows:
                self.surface.render_notice("(no interrupted tasks)")
            for r in rows:
                objective = (r.get("objective") or "")[:60]
                self.surface.render_notice(f"{r.get('id')}  {objective}")
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
                    self.surface.render_notice("(no interrupted tasks to resume)")
                    return True
                task_id = rows[0]["id"]
                self.surface.render_notice(f"resuming most recent: {task_id}")
            from athena.cli.chat import stream_task as _stream

            spec = await self.service.resume_task(task_id)
            self._active_task_id = spec.id
            result = await _stream(
                self.service, spec.id, autonomy=self.autonomy, surface=self.surface
            )
            if result is not None:
                status = getattr(result, "status", None)
                value = getattr(status, "value", None)
                s = str(value if value is not None else status)
                summary = getattr(result, "summary", "") or ""
                self.surface.render_result(summary, status=s)
            self._active_task_id = None
            return True
        if name in ("compact", "context"):
            summary = await self.service.operator_context_summary(self.session_id)
            window = summary.get("window")
            reserve = summary.get("reserve_output")
            recent = summary.get("recent_verbatim_turns")
            count = summary.get("message_count")
            if name == "compact":
                self.surface.render_notice(f"context window: {window or '?'} tokens")
                self.surface.render_notice(
                    f"output reserve: {reserve if reserve is not None else '?'} tokens"
                )
                self.surface.render_notice(
                    f"recent verbatim turns: {recent if recent is not None else '?'}"
                )
                older = "compressed with provenance retained"
                self.surface.render_notice(f"older transcript: {older}")
            else:
                self.surface.render_notice(f"session: {self.session_id or '(none yet)'}")
                self.surface.render_notice(
                    f"durable messages: {count if count is not None else '?'}"
                )
                self.surface.render_notice(
                    "next turn includes: objective, policy boundaries, recent turns,"
                )
                self.surface.render_notice(
                    "capability calls/results, relevant memories and skills."
                )
            return True
        return False

    def _cmd_mascot(self, arg: str) -> None:
        """List or switch the visible mascot/buddy (only one shows at a time)."""
        try:
            from athena.cli.dual_pane import Mascot
        except Exception:
            self.surface.render_notice("(mascot unavailable)")
            return
        setter = getattr(self.surface, "set_mascot", None)
        mascot = getattr(self.surface, "mascot", None)
        enabled = bool(getattr(self.surface, "mascot_enabled", False))
        current = getattr(mascot, "character", None)
        if not arg:
            shown = current if enabled else "off"
            self.surface.render_notice(f"mascot: {shown or '?'}")
            for cid, label in sorted(Mascot.available().items()):
                marker = "*" if enabled and cid == current else " "
                self.surface.render_notice(f"  {marker} {cid:10} {label}")
            self.surface.render_notice("usage: /mascot NAME | off")
            return
        if setter is None:
            self.surface.render_notice("(current surface has no mascot)")
            return
        choice = arg.strip().lower()
        if setter(choice):
            state = "off" if not getattr(self.surface, "mascot_enabled", True) else choice
            self.surface.render_notice(f"mascot: {state}")
            repaint = getattr(self.surface, "repaint_oi", None)
            if callable(repaint):
                repaint(force=True)
        else:
            valid = ", ".join(sorted(Mascot.available()))
            self.surface.render_notice(f"unknown mascot {arg!r}; choose one of: {valid}, off")

    def _cmd_scroll(self, arg: str) -> None:
        """Move a retained pane viewport without interrupting the task."""
        parts = arg.split()
        if len(parts) < 2:
            self.surface.render_notice("usage: /scroll left|right up|down|bottom [lines]")
            return
        pane, direction = parts[0], parts[1].lower()
        if direction == "bottom":
            fn = getattr(self.surface, "scroll_to_bottom", None)
            if fn is None or not fn(pane):
                self.surface.render_notice("scrolling is unavailable on the current surface")
            return
        try:
            amount = int(parts[2]) if len(parts) > 2 else 5
        except ValueError:
            self.surface.render_notice("scroll amount must be a number")
            return
        if direction == "up":
            amount = abs(amount)
        elif direction == "down":
            amount = -abs(amount)
        else:
            self.surface.render_notice("direction must be up, down, or bottom")
            return
        fn = getattr(self.surface, "scroll", None)
        if fn is None or not fn(pane, amount):
            self.surface.render_notice("scrolling is unavailable on the current surface")

    async def _submit_task(self, line: str) -> None:
        self.surface.render_user_message(line)
        request = AgentRequest(
            prompt=line,
            session_id=self.session_id,
            autonomy=self.autonomy,
            workspace=self.workspace,
            model_policy=_model_policy(self.model_policy),
            metadata=({"acceptance_criteria": list(self.criteria)} if self.criteria else {}),
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
            status = getattr(result, "status", None)
            status_str = (
                status.value if status is not None and hasattr(status, "value") else str(status)
            )
            self.surface.render_result(summary, status=status_str)
        self._active_task_id = None

    async def _shell_escape(self, source: str, inject: bool) -> None:
        """Execute a shell command directly without model inference.

        ``inject=True`` (the ``!`` form): result is shown, recorded, and made
        available to the next model turn. ``inject=False`` (the ``!!`` form):
        result is shown and recorded for audit, but excluded from model context.
        """
        if not source.strip():
            self.surface.render_notice("empty command")
            return

        self.surface.render_user_message(f"! {source}")

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
        flush = getattr(surface, "flush_task", None) or surface.finish
        flush()
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
