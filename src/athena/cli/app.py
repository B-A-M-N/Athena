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
    native    -> native Athena terminal frontend

Argument parsing prefers ``click`` (optional extra ``cli``) and falls back to
``argparse`` so the CLI works without it. Click is imported lazily.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena.protocol.tasks import AgentRequest, AutonomyLevel, WorkspaceSpec

OPENROUTER_DEFAULT_MODEL = "poolside/laguna-s-2.1:free"


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
        raise ValueError(f"unknown autonomy {value!r}; choose one of: {valid}") from None


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


def _openrouter_free_model(value: str | None) -> str:
    """Validate the env-selected OpenRouter model stays on free capacity.

    The automatic OpenRouter wiring is intentionally a free-inference path.
    An explicit model override is supported, but a paid model must not enter
    that path by accident or through a copied shell profile. OpenRouter's
    documented free model variants use the ``:free`` suffix; ``openrouter/free``
    is also accepted as the provider's free router.
    """
    model = (value or OPENROUTER_DEFAULT_MODEL).strip()
    if not model:
        model = OPENROUTER_DEFAULT_MODEL
    if model.lower() != "openrouter/free" and not model.lower().endswith(":free"):
        raise ValueError(
            "OPENROUTER_MODEL must identify a free route (use a model ending "
            "in ':free' or 'openrouter/free')"
        )
    return model


def build_config(o: "Options"):
    """Build the real ``athena.service.config.AthenaConfig`` from flags/env.

    ``athena.service.config.athena.AthenaConfig`` is the composition-root config
    the service understands. The CLI only forwards the knobs a user set on the
    command line (INV-007); it never interprets execution.
    """
    from athena.service.config import ProviderConfig, load_config

    root = o.workspace or _env("ATHENA_WORKSPACE", "ATHENA_WORKSPACE_PATH")
    pcs = getattr(o, "_providers", None)
    config = load_config(
        explicit_path=o.config_path,
        cli_overrides={
            "db_path": o.db_path,
            "workspace_root": (os.path.abspath(os.path.expanduser(root)) if root else None),
            "autonomy": _autonomy(o.autonomy).value if o.autonomy else None,
            "artifact_root": o.artifact_root,
            "mascot": o.mascot,
            "display": getattr(o, "display", None),
            "animations": getattr(o, "animations", None),
            "reduced_motion": True if getattr(o, "reduced_motion", False) else None,
        },
    )
    if config.workspace_root is None:
        config.workspace_root = os.getcwd()
    if pcs:
        config.providers = tuple(pcs)
    elif not config.providers and os.environ.get("OPENROUTER_API_KEY"):
        # OpenRouter speaks the OpenAI-compatible protocol. Keep the key in
        # the environment/SecretManager; never copy it into config or task
        # state. The free router selects an eligible free model at request time.
        config.providers = (
            ProviderConfig(
                kind="openai-compat",
                name="openrouter",
                model=_openrouter_free_model(os.environ.get("OPENROUTER_MODEL")),
                credential_id="OPENROUTER_API_KEY",
                base_url="https://openrouter.ai/api/v1",
                extra={"headers": {"X-Title": "Athena"}},
            ),
        )
    return config


def build_service(config) -> Any:
    """Construct (but do NOT start) an ``AthenaService``.

    ``athena.service.service`` is imported lazily so that this module imports
    and help renders cleanly even before the service package lands.
    """
    try:
        from athena.service.service import AthenaService
    except ImportError as exc:  # pragma: no cover
        raise ServiceUnavailable(
            "AthenaService is not available yet; cannot run tasks. (reason: %s)" % exc
        ) from exc
    return AthenaService(config=config)


def _doctor_display(o: "Options", config: Any) -> int:
    """Report the selected terminal projection without starting Athena."""
    from athena.cli.framebuffer import pillow_available
    from athena.cli.layout import compute_layout
    from athena.cli.render.kitty import KittyCapabilityProbe, select_renderer

    requested = str(o.display or getattr(config, "display", None) or "auto")
    columns, rows = shutil.get_terminal_size((120, 30))
    env_override = os.environ.get("ATHENA_KITTY_CONFIRMED", "").lower() in {"1", "true", "yes"}
    tty = bool(
        getattr(sys.stdout, "isatty", lambda: False)()
        and getattr(sys.stdin, "isatty", lambda: False)()
    )
    confirmed = env_override or (
        KittyCapabilityProbe.probe(sys.stdout, sys.stdin) if tty else False
    )
    selected = select_renderer(
        requested,
        capability_confirmed=confirmed and pillow_available(),
    )
    layout = compute_layout(columns, rows, requested)
    print(f"terminal: {columns}x{rows}  tty={'yes' if tty else 'no'}")
    print(f"requested: {requested}")
    print(f"kitty graphics: {'confirmed' if confirmed else 'not confirmed'}")
    print(f"Pillow framebuffer: {'available' if pillow_available() else 'missing'}")
    print(f"selected: {selected}  layout: {layout.mode.value}")
    if requested in {"auto", "glass"} and selected != "glass":
        print("note: Glass needs a confirmed Kitty graphics transport; ANSI is safe.")
    return 0


def _doctor_startup(o: "Options", config: Any) -> int:
    """Start the service briefly and report readiness-owned checks."""
    try:
        service = build_service(config)
    except ServiceUnavailable as exc:
        print(f"startup health: failed ({exc})")
        return 1

    async def probe() -> dict[str, Any]:
        try:
            await service.start()
            return service.startup_health()
        finally:
            await service.stop()

    try:
        health = asyncio.run(probe())
    except Exception as exc:  # pragma: no cover - environment-specific startup failure
        print(f"startup health: failed ({exc})")
        return 1
    print(f"startup health: {health.get('status', 'unknown')}")
    for name, check in (health.get("checks") or {}).items():
        status = check.get("status", "unknown") if isinstance(check, dict) else "unknown"
        print(f"  {name}: {status}")
    return 0 if health.get("status") == "ok" else 1


class ServiceUnavailable(Exception):
    pass


def _register_config_mascots(config) -> None:
    """Register user-defined mascot characters from config (UI-only concern)."""
    definitions = getattr(config, "mascots", None)
    if not definitions:
        return
    try:
        from athena.cli.dual_pane import configure_mascots
    except Exception:  # pragma: no cover
        return
    configure_mascots(definitions)


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
    mascot: str | None = None
    display: str | None = None
    animations: bool | None = None
    reduced_motion: bool = False
    criteria: str | None = None
    deny: bool = False
    artifact_root: str | None = None
    _providers: tuple[Any, ...] = ()


def dispatch(o: Options) -> int:
    """Run a parsed command synchronously (asyncio.run at the top)."""
    try:
        config = build_config(o)
    except ValueError as exc:
        print(f"athena configuration error: {exc}", file=sys.stderr)
        return 2
    if not o.mascot:
        o.mascot = getattr(config, "mascot", None)
    if not o.display:
        o.display = getattr(config, "display", None)
    if o.animations is None:
        o.animations = getattr(config, "animations", True)
    if not o.reduced_motion:
        o.reduced_motion = bool(getattr(config, "reduced_motion", False))
    _register_config_mascots(config)
    if o.command == "doctor":
        target = o.args[0] if o.args else "startup"
        if target == "startup":
            return _doctor_startup(o, config)
        if target != "display":
            print(
                "athena doctor: supported targets are 'startup' and 'display'",
                file=sys.stderr,
            )
            return 2
        return _doctor_display(o, config)
    if o.command == "native":
        from athena.cli.native import launch

        return launch(o)
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

        repl = ChatREPL(service=service, config=getattr(service, "config", None), options=o)
        return await repl.run_forever()
    if cmd == "run":
        return await _cmd_run(o, service)
    if cmd == "self":
        return await _cmd_self(o, service)
    if cmd == "inspect":
        from athena.cli.inspect import run_inspect

        return await run_inspect(service, o.args[0], verbose=o.verbose)
    if cmd == "sessions":
        return await _cmd_sessions(service)
    if cmd == "resume":
        from athena.cli.chat import ChatREPL

        sess_id = o.args[0]
        resumed = await _resume(service, sess_id)
        repl = ChatREPL(service=service, config=getattr(service, "config", None), options=o)
        repl.session_id = getattr(resumed, "session_id", None) or sess_id
        return await repl.run_forever()
    if cmd == "approve":
        return await _cmd_approve(o, service)
    if cmd == "cancel":
        return await _cmd_cancel(o, service)
    if cmd == "oi-stream":
        from athena.cli.oi_stream import run_viewer

        task_id = o.args[0] if o.args else None
        return await run_viewer(service, task_id, mascot=o.mascot)
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
    surface = None
    from athena.cli.chat import _make_surface, _model_label

    surface = _make_surface(
        details=o.details,
        mascot=o.mascot,
        display=o.display,
        model_label=_model_label(getattr(service, "config", None)),
        animations=True if o.animations is None else o.animations,
        reduced_motion=o.reduced_motion,
    )
    opener = getattr(surface, "open", None)
    closer = getattr(surface, "close", None)
    if callable(opener):
        opener()
    try:
        surface.render_user_message(objective)
        # Start streaming before waiting so an interactive approval can wake
        # the parked task. Waiting first deadlocks supervised execution at the
        # service boundary and hides the OI-style operator surface.
        task = await service.submit(request, wait=False)
        task_id = getattr(task, "id", task)
        from athena.cli.chat import stream_task

        result = await stream_task(
            service,
            task_id,
            autonomy=_autonomy(o.autonomy),
            surface=surface,
        )
        if result is not None:
            from athena.cli.chat import render_summary

            summary = getattr(result, "summary", "") or ""
            extra = render_summary(result)
            if extra:
                surface.render_notice(extra)
            status = getattr(result, "status", None)
            status_str = (
                status.value if status is not None and hasattr(status, "value") else str(status)
            )
            surface.render_result(summary, status=status_str)
            return 0
        # No result available: expose status anyway.
        surface.render_notice(f"[task {task_id} has no result yet]", status="PENDING")
        return 0
    finally:
        async_closer = getattr(surface, "aclose", None)
        if callable(async_closer):
            await async_closer()
        elif callable(closer):
            closer()


def _athena_checkout_root() -> str:
    """Resolve and validate the checkout targeted by ``athena self``."""
    root = None
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / ".git").exists():
            root = candidate.resolve()
            break
    if root is None:
        raise ValueError("athena self must run inside the Athena Git checkout")
    if not (root / "src" / "athena" / "__init__.py").is_file():
        raise ValueError("current Git checkout is not the Athena source tree")
    try:
        import tomllib

        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError("Athena pyproject.toml could not be validated") from exc
    if project.get("project", {}).get("name") != "athena-agent":
        raise ValueError("current checkout does not identify as athena-agent")
    return str(root)


async def _cmd_self(o: Options, service: Any) -> int:
    objective = o.args[0] if o.args else None
    if not objective:
        print("athena self: missing objective", file=sys.stderr)
        return 2
    try:
        root = _athena_checkout_root()
    except ValueError as exc:
        print(f"athena self: {exc}", file=sys.stderr)
        return 2

    criteria = [
        "command:uv run --frozen ruff format --check src tests",
        "command:uv run --frozen ruff check src tests",
        "command:uv run --frozen mypy src",
        "command:uv run --frozen pytest -q",
        "command:uv run --frozen python scripts/architecture-lint",
    ]
    request = AgentRequest(
        prompt=objective,
        autonomy=AutonomyLevel.CODING,
        workspace=WorkspaceSpec(id="athena-self", root=root),
        metadata={
            "self_host": True,
            "review_before_commit": True,
            "acceptance_criteria": criteria,
        },
    )
    from athena.cli.chat import _make_surface, _model_label, render_summary, stream_task

    surface = _make_surface(
        details=o.details,
        mascot=o.mascot,
        display=o.display,
        model_label=_model_label(getattr(service, "config", None)),
        animations=True if o.animations is None else o.animations,
        reduced_motion=o.reduced_motion,
    )
    opener = getattr(surface, "open", None)
    closer = getattr(surface, "close", None)
    if callable(opener):
        opener()
    try:
        surface.render_user_message(objective)
        task = await service.submit(request, wait=False)
        task_id = getattr(task, "id", task)
        result = await stream_task(service, task_id, autonomy=AutonomyLevel.CODING, surface=surface)
        if result is not None:
            extra = render_summary(result)
            if extra:
                surface.render_notice(extra)
            status = getattr(result, "status", None)
            status_str = getattr(status, "value", str(status))
            surface.render_result(getattr(result, "summary", "") or "", status=status_str)

        candidate = await service.operator_candidate(task_id)
        if candidate is None:
            surface.render_notice("no retained candidate is available", status="NOT_READY")
            return 0
        print(
            "\nCandidate ready for review: "
            f"{len(candidate['changed_resources'])} changed resource(s), "
            f"certificate {candidate.get('certificate_hash', 'missing')}"
        )
        print("[a] Apply  [d] Discard  [l] Later")
        try:
            choice = input("choice> ").strip().lower()[:1]
        except EOFError:
            choice = "l"
        if choice == "a":
            outcome = await service.apply_candidate(task_id)
            print(f"candidate: {outcome.get('status', 'unknown')}")
        elif choice == "d":
            outcome = await service.discard_candidate(task_id)
            print(f"candidate: {outcome.get('status', 'unknown')}")
        else:
            print("candidate retained for later review")
        return 0
    finally:
        async_closer = getattr(surface, "aclose", None)
        if callable(async_closer):
            await async_closer()
        elif callable(closer):
            closer()


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
    @click.option(
        "--mascot", default=None, help="Mascot/buddy character (e.g. owl, cat, bot, off)."
    )
    @click.option(
        "--display",
        type=click.Choice(["auto", "glass", "ansi", "plain"]),
        default=None,
        help="Display frontend: auto, glass, ansi, or plain.",
    )
    @click.option(
        "--no-animations", "no_animations", is_flag=True, help="Disable presentation animation."
    )
    @click.option("--reduced-motion", is_flag=True, help="Use reduced-motion presentation.")
    @click.option("--verbose", is_flag=True, help="Verbose output.")
    @click.option("--details", is_flag=True, help="Show detailed model and task activity.")
    @click.pass_context
    def cli(
        ctx: click.Context,
        config_path,
        db_path,
        workspace,
        autonomy,
        model,
        mascot,
        display,
        no_animations,
        reduced_motion,
        verbose,
        details,
    ) -> None:
        obj = ctx.ensure_object(dict)
        obj.update(
            config_path=config_path,
            db_path=db_path,
            workspace=workspace,
            autonomy=autonomy,
            model=model,
            mascot=mascot,
            display=display,
            animations=False if no_animations else None,
            reduced_motion=reduced_motion,
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
            mascot=b.get("mascot"),
            display=b.get("display"),
            animations=b.get("animations"),
            reduced_motion=bool(b.get("reduced_motion")),
            verbose=bool(b.get("verbose")),
            details=bool(b.get("details")),
        )

    @cli.group(invoke_without_command=True)
    @click.pass_context
    def doctor(ctx):
        """Diagnose local runtime and display capabilities."""

        if ctx.invoked_subcommand is None:
            sys.exit(dispatch(base_options(ctx, "doctor", ["startup"])))

    @doctor.command("display")
    @click.pass_context
    def doctor_display(ctx):
        """Check terminal geometry, Pillow, and Kitty transport support."""
        sys.exit(dispatch(base_options(ctx, "doctor", ["display"])))

    @doctor.command("startup")
    @click.pass_context
    def doctor_startup(ctx):
        """Check service startup health and readiness dependencies."""
        sys.exit(dispatch(base_options(ctx, "doctor", ["startup"])))

    @cli.command()
    @click.argument("objective", required=False)
    @click.option(
        "--details", "c_details", is_flag=True, help="Show detailed model and task activity."
    )
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
    @click.option(
        "--details", "r_details", is_flag=True, help="Show detailed model and task activity."
    )
    @click.option(
        "--criteria",
        "r_criteria",
        default=None,
        help="Acceptance criteria separated by ';'. Prefix 'command:' for an executable probe.",
    )
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
    @click.argument("objective")
    @click.pass_context
    def self(ctx, objective):
        """Improve this Athena checkout in a verified candidate workspace."""
        sys.exit(dispatch(base_options(ctx, "self", [objective])))

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

    @cli.command("oi-stream")
    @click.option(
        "--task", "task_id", default=None, help="Stream a specific task instead of the global tail."
    )
    @click.option(
        "--config", "oi_config_path", default=None, help="Path to the Athena config file."
    )
    @click.option("--db", "oi_db_path", default=None, help="Path to the Athena persistence DB.")
    @click.option("--workspace", "oi_workspace", default=None, help="Workspace root directory.")
    @click.option("--mascot", "oi_mascot", default=None, help="Mascot/buddy character.")
    @click.pass_context
    def oi_stream(ctx, task_id, oi_config_path, oi_db_path, oi_workspace, oi_mascot):
        """Live OI window: unbuffered model/runtime stream + activity mascot."""
        o = base_options(ctx, "oi-stream", [task_id] if task_id else [])
        o.config_path = oi_config_path or o.config_path
        o.db_path = oi_db_path or o.db_path
        o.workspace = oi_workspace or o.workspace
        o.mascot = oi_mascot or o.mascot
        sys.exit(dispatch(o))

    @cli.command("native")
    @click.pass_context
    def native(ctx):
        """Launch the development native Athena terminal frontend."""
        sys.exit(dispatch(base_options(ctx, "native")))

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
        sp.add_argument(
            "--mascot", default=None, help="Mascot/buddy character (e.g. owl, cat, bot, off)."
        )
        sp.add_argument("--display", choices=["auto", "glass", "ansi", "plain"], default=None)
        sp.add_argument("--no-animations", dest="no_animations", action="store_true")
        sp.add_argument("--reduced-motion", action="store_true")
        sp.add_argument("--verbose", action="store_true")
        sp.add_argument(
            "--details", action="store_true", help="Show detailed model and task activity."
        )
        sp.add_argument(
            "--criteria",
            default=None,
            help="Acceptance criteria separated by ';'. Prefix 'command:' for an executable probe.",
        )

    for name, help_, pos in (
        ("run", "Submit a one-shot objective.", "objective"),
        ("self", "Self-host a verified improvement to Athena.", "objective"),
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
    sp = sub.add_parser("doctor", help="Diagnose service startup and display support.")
    globals_(sp)
    sp.add_argument("target", nargs="?", choices=["startup", "display"], default="startup")
    sp = sub.add_parser("oi-stream", help="Stream the live OI projection.")
    globals_(sp)
    sp.add_argument("--task", dest="task_id", default=None)
    sp = sub.add_parser("native", help="Launch the native Athena terminal frontend.")
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
        mascot=getattr(ns, "mascot", None),
        display=getattr(ns, "display", None),
        animations=False if getattr(ns, "no_animations", False) else None,
        reduced_motion=getattr(ns, "reduced_motion", False),
        verbose=getattr(ns, "verbose", False),
        details=getattr(ns, "details", False),
        criteria=getattr(ns, "criteria", None),
        deny=getattr(ns, "deny", False) if command == "approve" else False,
    )
    if command == "doctor":
        o.args = [ns.target]
    if command == "oi-stream":
        o.args = [ns.task_id] if ns.task_id else []
    elif command in ("run", "self"):
        o.args = [ns.objective]
    elif command in ("inspect", "resume", "cancel", "approve"):
        o.args = [
            getattr(
                ns,
                "task_id"
                if command in ("inspect", "cancel")
                else ("session_id" if command == "resume" else "approval_id"),
                "",
            )
        ]
    elif command == "chat" and getattr(ns, "objective", None):
        o.command = "run"
        o.args = [ns.objective]
    if (o.command in ("run", "self", "inspect", "resume", "approve", "cancel")) and (
        not o.args or not o.args[0]
    ):
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
