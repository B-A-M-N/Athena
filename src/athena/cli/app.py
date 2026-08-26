"""Athena CLI entrypoint.

This is the top of the interface stack (BUILDSPEC §8). It MUST remain
system-neutral (INV-001/007): it never runs an agent loop. It builds an
``AthenaService``, dispatches a subcommand, and renders whatever the service
returns. Task execution happens inside the kernel.

Each subcommand maps onto a documented ``AthenaService`` method:

    chat      -> interactive REPL (chat.py)
    run       -> submit + stream events + print result + exit
    inspect   -> inspect.py renders task/events/results
    sessions  -> list_sessions (interactive '' session listing)
    resume    -> resume(session_id), then interactive chat
    approve   -> approve(approval_id, grant)
    cancel    -> cancel(task_id)

Argument parsing prefers ``click`` (optional extra ``cli``) and falls back to
``argparse`` so the CLI works without it. Click is imported lazily.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any

from athena.protocol.tasks import AgentRequest, AutonomyLevel, WorkspaceSpec


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _autonomy(value: str | None) -> AutonomyLevel:
    if not value:
        return AutonomyLevel.SUPERVISED
    try:
        return AutonomyLevel(value.strip().lower())
    except ValueError:
        valid = ", ".join(a.value for a in AutonomyLevel)
        raise ValueError(
            f"unknown autonomy {value!r}; choose one of: {valid}"
        ) from None


def _criteria_metadata(o: "Options") -> dict:
    """Parse ``o.criteria`` (semicolon-separated) into request metadata.

    Entries prefixed ``command:`` become executable acceptance probes;
    everything else is model-judged. Semicolons avoid clashing with the
    shell pipes/quotes people already use inside probe commands.
    """
    raw = getattr(o, "criteria", None)
    if not raw:
        return {}
    items = [part.strip() for part in str(raw).split(";") if part.strip()]
    return {"acceptance_criteria": items} if items else {}


def build_config(o: "Options"):
    """Build the real ``athena.service.config.AthenaConfig`` from flags/env.

    ``athena.service.config.athena.AthenaConfig`` is the composition-root config
    the service understands. The CLI only forwards the knobs a user set on the
    command line (INV-007); it never interprets execution.
    """
    from athena.service.config import AthenaConfig

    root = o.workspace or _env("ATHENA_WORKSPACE", "ATHENA_WORKSPACE_PATH")
    pcs = getattr(o, "_providers", None)
    return AthenaConfig(
        db_path=o.db_path or _env("ATHENA_DB", "ATHENA_DB_PATH"),
        workspace_root=os.path.abspath(os.path.expanduser(root)) if root else os.getcwd(),
        autonomy=_autonomy(o.autonomy),
        artifact_root=o.artifact_root,
        providers=pcs or (),
    )


def build_service(config) -> Any:
    """Construct (but do NOT start) an ``AthenaService``.

    ``athena.service.service`` is imported lazily so that this module imports
    and help renders cleanly even before the service package lands.
    """
    try:
        from athena.service.service import AthenaService
    except ImportError as exc:  # pragma: no cover
        raise ServiceUnavailable(
            "AthenaService is not available yet; cannot run tasks. "
            "(reason: %s)" % exc
        ) from exc
    return AthenaService(config=config)


class ServiceUnavailable(Exception):
    pass


def workspace_spec(root: str | None) -> WorkspaceSpec | None:
    if not root:
        return None
    return WorkspaceSpec(id="cli", root=os.path.abspath(os.path.expanduser(root)))


def _model_policy(name: str | None):
    if not name:
        return None
    from athena.protocol.tasks import ModelPolicy

    return ModelPolicy(allowed=(name,))


# ---------------------------------------------------------------------------
# CLI options + dispatch
# ---------------------------------------------------------------------------


@dataclass
class Options:
    command: str = "chat"
    args: list[str] = field(default_factory=list)
    config_path: str | None = None
    db_path: str | None = None
    workspace: str | None = None
    autonomy: str | None = None
    model: str | None = None
    verbose: bool = False
    details: bool = False
    criteria: str | None = None
    deny: bool = False
    artifact_root: str | None = None
    _providers: tuple[Any, ...] = ()


def dispatch(o: Options) -> int:
    """Run a parsed command synchronously (asyncio.run at the top)."""
    config = build_config(o)
    try:
        service = build_service(config)
    except ServiceUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1

    async def _runner(o: Options, service: Any) -> int:
        try:
            start = getattr(service, "start", None)
            if start is not None:
                await start()
            return await _run(o, service)
        finally:
            stop = getattr(service, "stop", None)
            if stop is not None:
                try:
                    await stop()
                except Exception:  # pragma: no cover
                    pass

    code = 1
    try:
        code = asyncio.run(_runner(o, service))
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:  # pragma: no cover
        print(f"error: {exc}", file=sys.stderr)
        if o.verbose:
            traceback.print_exc()
        code = 1
    return code


async def _run(o: Options, service: Any) -> int:
    cmd = o.command
    if cmd == "chat":
        from athena.cli.chat import ChatREPL

        repl = ChatREPL(service=service, options=o)
        return await repl.run_forever()
    if cmd == "run":
        return await _cmd_run(o, service)
    if cmd == "inspect":
        from athena.cli.inspect import run_inspect

        return await run_inspect(service, o.args[0], verbose=o.verbose)
    if cmd == "sessions":
        return await _cmd_sessions(service)
    if cmd == "resume":
        from athena.cli.chat import ChatREPL

        sess_id = o.args[0]
        resumed = await _resume(service, sess_id)
        repl = ChatREPL(service=service, options=o)
        repl.session_id = (getattr(resumed, "session_id", None) or sess_id)
        return await repl.run_forever()
    if cmd == "approve":
        return await _cmd_approve(o, service)
    if cmd == "cancel":
        return await _cmd_cancel(o, service)
    return 2


async def _cmd_run(o: Options, service: Any) -> int:
    objective = o.args[0] if o.args else None
    if not objective:
        print("athena run: missing objective", file=sys.stderr)
        return 2
    request = AgentRequest(
        prompt=objective,
        autonomy=_autonomy(o.autonomy),
        workspace=workspace_spec(o.workspace),
        model_policy=_model_policy(o.model),
        metadata=_criteria_metadata(o),
    )
    # Start streaming before waiting so an interactive approval can wake the
    # parked task.  Waiting first deadlocks supervised execution at the
    # service boundary and hides the OI-style operator surface.
    task = await service.submit(request, wait=False)
    task_id = getattr(task, "id", task)
    from athena.cli.chat import stream_task
    from athena.cli.surface import OperatorSurface

    result = await stream_task(
        service,
        task_id,
        autonomy=_autonomy(o.autonomy),
        surface=OperatorSurface(details=o.details),
    )
    if result is not None:
        from athena.cli.chat import render_summary

        summary = getattr(result, "summary", "") or ""
        if summary:
            print()
            print("--- Result ---")
            print(summary)
        extra = render_summary(result)
        if extra:
            print()
            print(extra)
        status = getattr(result, "status", None)
        status_str = (
            status.value if status is not None and hasattr(status, "value") else str(status)
        )
        print(f"\n[task {task_id} -> {status_str}]")
        return 0
    # No result available: expose status anyway.
    print(f"\n[task {task_id} has no result yet]", file=sys.stderr)
    return 0


async def _cmd_sessions(service: Any) -> int:
    try:
        sessions = await service.list_sessions()
    except (NotImplementedError, AttributeError):
        print("(session listing unavailable)", file=sys.stderr)
        return 1
    sessions = list(sessions or [])
    if not sessions:
        print("(no sessions)")
        return 0
    for s in sessions:
        sid = s.get("id") if isinstance(s, dict) else getattr(s, "id", s)
        title = (
            s.get("objective")
            if isinstance(s, dict)
            else (getattr(s, "title", None) or getattr(s, "objective", "") or "")
        )
        print(f"{sid}\t{title or ''}")
    return 0


async def _resume(service: Any, sess_id: str) -> Any:
    fn = getattr(service, "resume", None)
    if fn is None:
        raise NotImplementedError("service does not expose resume()")
    return await fn(sess_id)


async def _cmd_approve(o: Options, service: Any) -> int:
    approval_id = o.args[0]
    approve = getattr(service, "approve", None)
    if approve is None:
        print("service does not expose approve()", file=sys.stderr)
        return 1
    await approve(approval_id, granted=not o.deny)
    print(f"approval {approval_id}: {'granted' if not o.deny else 'denied'}")
    return 0


async def _cmd_cancel(o: Options, service: Any) -> int:
    task_id = o.args[0]
    cancel = getattr(service, "cancel", None)
    if cancel is None:
        print("service does not expose cancel()", file=sys.stderr)
        return 1
    await cancel(task_id)
    print(f"cancel requested for {task_id}")
    return 0


# ---------------------------------------------------------------------------
# click front-end (preferred)
# ---------------------------------------------------------------------------


def _click_cli(click: Any):
    levels = [a.value for a in AutonomyLevel]

    @click.group(invoke_without_command=True)
    @click.version_option("0.1.0")
    @click.option("--config", "config_path", default=None, help="Path to config file.")
    @click.option("--db", "db_path", default=None, help="Path to persistence DB.")
    @click.option("--workspace", default=None, help="Workspace root directory.")
    @click.option("--autonomy", default=None, type=click.Choice(levels), help="Autonomy profile.")
    @click.option("--model", default=None, help="Model policy.")
    @click.option("--verbose", is_flag=True, help="Verbose output.")
    @click.option("--details", is_flag=True, help="Show detailed model and task activity.")
    @click.pass_context
    def cli(ctx: click.Context, config_path, db_path, workspace, autonomy, model, verbose, details) -> None:
        obj = ctx.ensure_object(dict)
        obj.update(
            config_path=config_path,
            db_path=db_path,
            workspace=workspace,
            autonomy=autonomy,
            model=model,
            verbose=verbose,
            details=details,
        )
        if ctx.invoked_subcommand is None:
            # Bare `athena`/`athena chat` (no goal) lands in the interactive
            # REPL (BUILDSPEC §97: `athena` is a valid command).
            sys.exit(dispatch(base_options(ctx=ctx, command="chat")))

    def base_options(ctx: Any, command: str, args: list[str] | None = None) -> Options:
        b = ctx.ensure_object(dict)
        return Options(
            command=command,
            args=list(args or []),
            config_path=b.get("config_path"),
            db_path=b.get("db_path"),
            workspace=b.get("workspace"),
            autonomy=b.get("autonomy"),
            model=b.get("model"),
            verbose=bool(b.get("verbose")),
            details=bool(b.get("details")),
        )

    @cli.command()
    @click.argument("objective", required=False)
    @click.option("--details", "c_details", is_flag=True, help="Show detailed model and task activity.")
    @click.pass_context
    def chat(ctx, objective, c_details):
        if c_details:
            ctx.ensure_object(dict)["details"] = True
        if objective:
            sys.exit(dispatch(base_options(ctx, "run", [objective])))
        sys.exit(dispatch(base_options(ctx, "chat")))

    @cli.command()
    @click.argument("objective")
    @click.option("--autonomy", "a_autonomy", type=click.Choice([a.value for a in AutonomyLevel]))
    @click.option("--workspace", "a_workspace", default=None)
    @click.option("--model", "a_model", default=None)
    @click.option("--details", "r_details", is_flag=True, help="Show detailed model and task activity.")
    @click.option("--criteria", "r_criteria", default=None, help="Acceptance criteria separated by ';'. Prefix 'command:' for an executable probe.")
    @click.pass_context
    def run(ctx, objective, **kw):
        o = base_options(ctx, "run", [objective])
        o.autonomy = kw.get("a_autonomy") or o.autonomy
        o.workspace = kw.get("a_workspace") or o.workspace
        o.model = kw.get("a_model") or o.model
        o.details = bool(kw.get("r_details")) or o.details
        o.criteria = kw.get("r_criteria") or o.criteria
        sys.exit(dispatch(o))

    @cli.command()
    @click.argument("task_id")
    @click.pass_context
    def inspect(ctx, task_id):
        sys.exit(dispatch(base_options(ctx, "inspect", [task_id])))

    @cli.command("sessions")
    @click.pass_context
    def sessions(ctx):
        sys.exit(dispatch(base_options(ctx, "sessions")))

    @cli.command()
    @click.argument("session_id")
    @click.pass_context
    def resume(ctx, session_id):
        sys.exit(dispatch(base_options(ctx, "resume", [session_id])))

    @cli.command()
    @click.argument("approval_id")
    @click.option("--deny", is_flag=True, help="Deny the approval.")
    @click.pass_context
    def approve(ctx, approval_id, deny):
        o = base_options(ctx, "approve", [approval_id])
        o.deny = deny
        sys.exit(dispatch(o))

    @cli.command()
    @click.argument("task_id")
    @click.pass_context
    def cancel(ctx, task_id):
        sys.exit(dispatch(base_options(ctx, "cancel", [task_id])))

    return cli


# ---------------------------------------------------------------------------
# argparse front-end (fallback)
# ---------------------------------------------------------------------------


def _arg_parse(argv: list[str]) -> Options:
    import argparse

    p = argparse.ArgumentParser(prog="athena", description="Project Athena CLI")
    sub = p.add_subparsers(dest="command", metavar="SUBCOMMAND")

    def globals_(sp):
        sp.add_argument("--config", dest="config_path", default=None)
        sp.add_argument("--db", dest="db_path", default=None)
        sp.add_argument("--workspace", default=None)
        sp.add_argument("--autonomy", default=None)
        sp.add_argument("--model", default=None)
        sp.add_argument("--verbose", action="store_true")
        sp.add_argument("--details", action="store_true", help="Show detailed model and task activity.")
        sp.add_argument("--criteria", default=None, help="Acceptance criteria separated by ';'. Prefix 'command:' for an executable probe.")

    for name, help_, pos in (
        ("run", "Submit a one-shot objective.", "objective"),
        ("inspect", "Inspect a task.", "task_id"),
        ("resume", "Resume a session.", "session_id"),
        ("approve", "Approve/deny a pending approval.", "approval_id"),
        ("cancel", "Cancel a running task.", "task_id"),
    ):
        sp = sub.add_parser(name, help=help_)
        globals_(sp)
        sp.add_argument(pos)
        if name == "approve":
            sp.add_argument("--deny", action="store_true", help="Deny the approval.")

    sp = sub.add_parser("chat", help="Start the interactive REPL.")
    globals_(sp)
    sp.add_argument("objective", nargs="?", default=None)
    sp = sub.add_parser("sessions", help="List sessions.")
    globals_(sp)

    ns = p.parse_args(argv)
    command = ns.command or "chat"
    o = Options(
        command=command,
        config_path=getattr(ns, "config_path", None),
        db_path=getattr(ns, "db_path", None),
        workspace=getattr(ns, "workspace", None),
        autonomy=getattr(ns, "autonomy", None),
        model=getattr(ns, "model", None),
        verbose=getattr(ns, "verbose", False),
        details=getattr(ns, "details", False),
        criteria=getattr(ns, "criteria", None),
        deny=getattr(ns, "deny", False) if command == "approve" else False,
    )
    if command == "run":
        o.args = [ns.objective]
    elif command in ("inspect", "resume", "cancel", "approve"):
        o.args = [getattr(ns, "task_id" if command in ("inspect", "cancel") else ("session_id" if command == "resume" else "approval_id"), "")]
    elif command == "chat" and getattr(ns, "objective", None):
        o.command = "run"
        o.args = [ns.objective]
    if (o.command in ("run", "inspect", "resume", "approve", "cancel")) and (not o.args or not o.args[0]):
        print(f"athena {o.command}: missing required argument", file=sys.stderr)
        p.print_usage(file=sys.stderr)
        return o  # argparse will have printed usage; we return nonzero
    return o


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Synchronous ``[project.scripts] athena`` entrypoint."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        import click
    except ImportError:  # pragma: no cover
        click = None  # type: ignore[assignment]  # optional dependency absent

    if click is not None:
        _cli = _click_cli(click)
        try:
            _cli.main(args=argv, prog_name="athena", standalone_mode=True)
        except SystemExit:
            raise
        return

    o = _arg_parse(argv)
    code = dispatch(o)
    raise SystemExit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
