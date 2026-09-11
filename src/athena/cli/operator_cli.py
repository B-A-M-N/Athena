"""Discoverable Click command families for Athena's operator surfaces.

The service remains the only execution authority.  This module only declares
typed Click arguments and translates them into the existing ``Options``
dispatcher contract, so the legacy argparse fallback and the hosted/native
operator router can continue to share service behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_operator_commands(
    cli: Any,
    click: Any,
    *,
    base_options: Callable[..., Any],
    dispatch: Callable[[Any], int],
) -> None:
    """Register typed, nested operator command families on ``cli``."""

    def invoke(ctx: Any, command: str, args: tuple[str, ...] = (), **values: Any) -> None:
        options = base_options(ctx, command, list(args))
        for key, value in values.items():
            setattr(options, key, value)
        ctx.exit(dispatch(options))

    @cli.group("sessions", invoke_without_command=True)
    @click.pass_context
    def sessions(ctx: Any) -> None:
        """List, inspect, and close durable sessions.

        Commands: list, show, close.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "sessions", ("list",))

    @sessions.command("list")
    @click.pass_context
    def sessions_list(ctx: Any) -> None:
        """List durable sessions."""
        invoke(ctx, "sessions", ("list",))

    @sessions.command("show")
    @click.argument("session_id")
    @click.pass_context
    def sessions_show(ctx: Any, session_id: str) -> None:
        """Show one durable session."""
        invoke(ctx, "sessions", ("show", session_id))

    @sessions.command("close")
    @click.argument("session_id")
    @click.pass_context
    def sessions_close(ctx: Any, session_id: str) -> None:
        """Close one durable session and its session-scoped resources."""
        invoke(ctx, "sessions", ("close", session_id))

    @cli.group("tasks", invoke_without_command=True)
    @click.pass_context
    def tasks(ctx: Any) -> None:
        """Inspect and control durable tasks.

        Commands: list, show, result, cancel, interrupt, resume, steer, input.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "tasks", ("list",))

    @tasks.command("list")
    @click.option("--status", type=click.Choice(_task_status_values()), default=None)
    @click.pass_context
    def tasks_list(ctx: Any, status: str | None) -> None:
        """List durable tasks, optionally filtered by lifecycle status."""
        invoke(ctx, "tasks", ("list",), task_status=status)

    @tasks.command("show")
    @click.argument("task_id")
    @click.pass_context
    def tasks_show(ctx: Any, task_id: str) -> None:
        """Show the forensic task view."""
        invoke(ctx, "tasks", ("show", task_id))

    tasks.add_command(tasks_show, "inspect")

    @tasks.command("result")
    @click.argument("task_id")
    @click.pass_context
    def tasks_result(ctx: Any, task_id: str) -> None:
        """Print the durable task result."""
        invoke(ctx, "tasks", ("result", task_id))

    @tasks.command("cancel")
    @click.argument("task_id")
    @click.pass_context
    def tasks_cancel(ctx: Any, task_id: str) -> None:
        """Cancel a task."""
        invoke(ctx, "tasks", ("cancel", task_id))

    @tasks.command("interrupt")
    @click.argument("task_id")
    @click.pass_context
    def tasks_interrupt(ctx: Any, task_id: str) -> None:
        """Interrupt a task at the next safe boundary."""
        invoke(ctx, "tasks", ("interrupt", task_id))

    @tasks.command("resume")
    @click.argument("task_id")
    @click.pass_context
    def tasks_resume(ctx: Any, task_id: str) -> None:
        """Resume a task in an explicitly resumable state."""
        invoke(ctx, "tasks", ("resume", task_id))

    @tasks.command("steer")
    @click.argument("task_id")
    @click.argument("text", nargs=-1, required=True)
    @click.option("--source-task-id", default=None, help="Authorized ancestor task id.")
    @click.pass_context
    def tasks_steer(
        ctx: Any, task_id: str, text: tuple[str, ...], source_task_id: str | None
    ) -> None:
        """Queue operator steering text for a task."""
        invoke(
            ctx,
            "tasks",
            ("steer", task_id, " ".join(text)),
            steer_source_task_id=source_task_id,
        )

    @tasks.command("input")
    @click.argument("task_id")
    @click.argument("answer", nargs=-1, required=True)
    @click.pass_context
    def tasks_input(ctx: Any, task_id: str, answer: tuple[str, ...]) -> None:
        """Provide an answer to a task's pending input request."""
        invoke(ctx, "tasks", ("input", task_id, " ".join(answer)))

    @cli.group("jobs", invoke_without_command=True)
    @click.pass_context
    def jobs(ctx: Any) -> None:
        """Inspect and control scheduled jobs.

        Commands: list, show, enable, disable, run-now, grant, revoke.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "jobs", ("list",))

    @jobs.command("list")
    @click.option("--enabled-only", is_flag=True, help="Hide disabled jobs.")
    @click.pass_context
    def jobs_list(ctx: Any, enabled_only: bool) -> None:
        """List scheduled jobs and their latest durable run receipt."""
        invoke(ctx, "jobs", ("list",), jobs_enabled_only=enabled_only)

    @jobs.command("show")
    @click.argument("job_id")
    @click.pass_context
    def jobs_show(ctx: Any, job_id: str) -> None:
        """Show one scheduled job."""
        invoke(ctx, "jobs", ("show", job_id))

    @jobs.command("enable")
    @click.argument("job_id")
    @click.pass_context
    def jobs_enable(ctx: Any, job_id: str) -> None:
        """Enable a scheduled job."""
        invoke(ctx, "jobs", ("enable", job_id))

    @jobs.command("disable")
    @click.argument("job_id")
    @click.pass_context
    def jobs_disable(ctx: Any, job_id: str) -> None:
        """Disable a scheduled job."""
        invoke(ctx, "jobs", ("disable", job_id))

    @jobs.command("run-now")
    @click.argument("job_id")
    @click.pass_context
    def jobs_run_now(ctx: Any, job_id: str) -> None:
        """Claim and run one occurrence immediately."""
        invoke(ctx, "jobs", ("run-now", job_id))

    jobs.add_command(jobs_run_now, "run")

    @jobs.command("grant")
    @click.argument("job_id")
    @click.argument("task_id")
    @click.option("--principal-id", default=None)
    @click.option("--project-id", default=None)
    @click.option(
        "--operation",
        "operations",
        multiple=True,
        type=click.Choice(["inspect", "update", "enable", "disable", "run"]),
        help="Restrict the lease; repeat for multiple operations.",
    )
    @click.option("--expires-at", default=None, help="RFC3339 lease expiry.")
    @click.pass_context
    def jobs_grant(
        ctx: Any,
        job_id: str,
        task_id: str,
        principal_id: str | None,
        project_id: str | None,
        operations: tuple[str, ...],
        expires_at: str | None,
    ) -> None:
        """Grant a task-bound schedule-control lease."""
        invoke(
            ctx,
            "jobs",
            ("grant", job_id, task_id),
            job_principal_id=principal_id,
            job_project_id=project_id,
            job_operations=operations,
            job_expires_at=expires_at,
        )

    @jobs.command("revoke")
    @click.argument("job_id")
    @click.pass_context
    def jobs_revoke(ctx: Any, job_id: str) -> None:
        """Revoke a task-bound schedule-control lease."""
        invoke(ctx, "jobs", ("revoke", job_id))

    @cli.group("workflows", invoke_without_command=True)
    @click.pass_context
    def workflows(ctx: Any) -> None:
        """Inspect durable workflow definitions without executing them.

        Commands: list, show, describe.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "workflows", ("list",))

    @workflows.command("list")
    @click.option("--task-id", default=None, help="Include task-owned workflows for this task.")
    @click.pass_context
    def workflows_list(ctx: Any, task_id: str | None) -> None:
        """List visible workflows."""
        invoke(ctx, "workflows", ("list",) + ((task_id,) if task_id else ()))

    @workflows.command("show")
    @click.argument("workflow_id")
    @click.argument("legacy_task_id", required=False)
    @click.option("--task-id", default=None, help="Task scope for task-owned workflows.")
    @click.pass_context
    def workflows_show(
        ctx: Any,
        workflow_id: str,
        legacy_task_id: str | None,
        task_id: str | None,
    ) -> None:
        """Show one visible workflow definition."""
        task_id = task_id or legacy_task_id
        args = ("show", workflow_id) + ((task_id,) if task_id else ())
        invoke(ctx, "workflows", args)

    workflows.add_command(workflows_show, "inspect")
    workflows.add_command(workflows_show, "describe")

    @cli.group("packs", invoke_without_command=True)
    @click.pass_context
    def packs(ctx: Any) -> None:
        """Inspect and control declarative capability packs.

        Commands: list, search, inspect, install, enable, disable, remove.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "packs", ("list",))

    @packs.command("list")
    @click.pass_context
    def packs_list(ctx: Any) -> None:
        """List installed packs and live health."""
        invoke(ctx, "packs", ("list",))

    @packs.command("search")
    @click.argument("text")
    @click.pass_context
    def packs_search(ctx: Any, text: str) -> None:
        """Search installed packs by id or publisher."""
        invoke(ctx, "packs", ("search", text))

    @packs.command("inspect")
    @click.argument("pack_id")
    @click.pass_context
    def packs_inspect(ctx: Any, pack_id: str) -> None:
        """Inspect one installed pack."""
        invoke(ctx, "packs", ("inspect", pack_id))

    @packs.command("install")
    @click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=str))
    @click.option("--disabled", "install_disabled", is_flag=True, help="Install without enabling.")
    @click.pass_context
    def packs_install(ctx: Any, source: str, install_disabled: bool) -> None:
        """Install a pack from a local source file."""
        invoke(ctx, "packs", ("install", source), pack_enable=not install_disabled)

    @packs.command("enable")
    @click.argument("pack_id")
    @click.pass_context
    def packs_enable(ctx: Any, pack_id: str) -> None:
        """Enable one installed pack."""
        invoke(ctx, "packs", ("enable", pack_id))

    @packs.command("disable")
    @click.argument("pack_id")
    @click.pass_context
    def packs_disable(ctx: Any, pack_id: str) -> None:
        """Disable one installed pack."""
        invoke(ctx, "packs", ("disable", pack_id))

    @packs.command("remove")
    @click.argument("pack_id")
    @click.pass_context
    def packs_remove(ctx: Any, pack_id: str) -> None:
        """Remove one installed pack."""
        invoke(ctx, "packs", ("remove", pack_id))

    packs.add_command(packs_remove, "uninstall")

    @cli.group("memory", invoke_without_command=True)
    @click.pass_context
    def memory(ctx: Any) -> None:
        """Review durable memory candidates.

        Commands: candidates, inspect, promote, discard.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "memory", ("candidates",))

    @memory.command("candidates")
    @click.pass_context
    def memory_candidates(ctx: Any) -> None:
        """List pending memory candidates."""
        invoke(ctx, "memory", ("candidates",))

    memory.add_command(memory_candidates, "list")

    @memory.command("inspect")
    @click.argument("memory_id")
    @click.pass_context
    def memory_inspect(ctx: Any, memory_id: str) -> None:
        """Inspect one pending memory candidate."""
        invoke(ctx, "memory", ("inspect", memory_id))

    @memory.command("promote")
    @click.argument("memory_id")
    @click.argument(
        "scope", type=click.Choice(["job", "session", "project", "user", "global", "task"])
    )
    @click.argument("scope_id", required=False)
    @click.pass_context
    def memory_promote(ctx: Any, memory_id: str, scope: str, scope_id: str | None) -> None:
        """Promote a memory candidate into an explicit durable scope."""
        args = ("promote", memory_id, scope) + ((scope_id,) if scope_id else ())
        invoke(ctx, "memory", args)

    @memory.command("discard")
    @click.argument("memory_id")
    @click.pass_context
    def memory_discard(ctx: Any, memory_id: str) -> None:
        """Discard one pending memory candidate."""
        invoke(ctx, "memory", ("discard", memory_id))

    @cli.group("mcp", invoke_without_command=True)
    @click.pass_context
    def mcp(ctx: Any) -> None:
        """Inspect and reconnect configured MCP servers.

        Commands: list, tools, resources, prompts, doctor, reconnect.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "mcp", ("list",))

    @mcp.command("list")
    @click.pass_context
    def mcp_list(ctx: Any) -> None:
        """List configured MCP server connection state."""
        invoke(ctx, "mcp", ("list",))

    mcp.add_command(mcp_list, "status")

    @mcp.command("tools")
    @click.argument("server")
    @click.pass_context
    def mcp_tools(ctx: Any, server: str) -> None:
        """List tools discovered from one connected MCP server."""
        invoke(ctx, "mcp", ("tools", server))

    @mcp.command("resources")
    @click.pass_context
    def mcp_resources(ctx: Any) -> None:
        """List resources exposed by configured MCP adapters."""
        invoke(ctx, "mcp", ("resources",))

    @mcp.command("prompts")
    @click.pass_context
    def mcp_prompts(ctx: Any) -> None:
        """List prompts exposed by configured MCP adapters."""
        invoke(ctx, "mcp", ("prompts",))

    @mcp.command("doctor")
    @click.argument("server")
    @click.pass_context
    def mcp_doctor(ctx: Any, server: str) -> None:
        """Probe one MCP server and report tool discovery health."""
        invoke(ctx, "mcp", ("doctor", server))

    @mcp.command("reconnect")
    @click.argument("server")
    @click.pass_context
    def mcp_reconnect(ctx: Any, server: str) -> None:
        """Reconnect one configured MCP server."""
        invoke(ctx, "mcp", ("reconnect", server))

    @cli.group("skills", invoke_without_command=True)
    @click.pass_context
    def skills(ctx: Any) -> None:
        """Inspect and control installed skills.

        Commands: list, search, inspect, enable, disable.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "skills", ("list",))

    @skills.command("list")
    @click.pass_context
    def skills_list(ctx: Any) -> None:
        """List installed skills and lifecycle state."""
        invoke(ctx, "skills", ("list",))

    @skills.command("search")
    @click.argument("text")
    @click.pass_context
    def skills_search(ctx: Any, text: str) -> None:
        """Search installed skills by name, description, or trigger."""
        invoke(ctx, "skills", ("search", text))

    @skills.command("inspect")
    @click.argument("skill_id")
    @click.pass_context
    def skills_inspect(ctx: Any, skill_id: str) -> None:
        """Inspect one installed skill."""
        invoke(ctx, "skills", ("inspect", skill_id))

    @skills.command("enable")
    @click.argument("skill_id")
    @click.pass_context
    def skills_enable(ctx: Any, skill_id: str) -> None:
        """Enable one installed skill."""
        invoke(ctx, "skills", ("enable", skill_id))

    @skills.command("disable")
    @click.argument("skill_id")
    @click.pass_context
    def skills_disable(ctx: Any, skill_id: str) -> None:
        """Disable one installed skill."""
        invoke(ctx, "skills", ("disable", skill_id))

    @cli.group("inference-recoveries", invoke_without_command=True)
    @click.pass_context
    def inference_recoveries(ctx: Any) -> None:
        """Inspect and resolve uncertain provider attempts.

        Commands: list, show, resolve, close-liability.
        """

        if ctx.invoked_subcommand is None:
            invoke(ctx, "inference-recoveries", ("list",))

    @inference_recoveries.command("list")
    @click.pass_context
    def inference_list(ctx: Any) -> None:
        """List unresolved provider attempts."""
        invoke(ctx, "inference-recoveries", ("list",))

    inference_recoveries.add_command(inference_list, "status")

    @inference_recoveries.command("show")
    @click.argument("attempt_id")
    @click.pass_context
    def inference_show(ctx: Any, attempt_id: str) -> None:
        """Show one provider-attempt recovery record."""
        invoke(ctx, "inference-recoveries", ("show", attempt_id))

    inference_recoveries.add_command(inference_show, "inspect")

    @inference_recoveries.command("resolve")
    @click.argument("attempt_id")
    @click.option(
        "--resolution",
        required=True,
        type=click.Choice(["failed", "succeeded", "retry", "abandon"]),
        help="Disposition to record for this provider attempt.",
    )
    @click.option("--note", required=True, help="Operator evidence and reconciliation note.")
    @click.option("--provider-response-id", default=None)
    @click.option("--actual-cost", default=None, help="Finite non-negative provider cost in USD.")
    @click.pass_context
    def inference_resolve(
        ctx: Any,
        attempt_id: str,
        resolution: str,
        note: str,
        provider_response_id: str | None,
        actual_cost: str | None,
    ) -> None:
        """Record a provider disposition and apply its documented effects."""
        invoke(
            ctx,
            "inference-recoveries",
            ("resolve", attempt_id),
            recovery_resolution=resolution,
            recovery_note=note,
            recovery_provider_response_id=provider_response_id,
            recovery_actual_cost=actual_cost,
        )

    @inference_recoveries.command("close-liability")
    @click.argument("attempt_id")
    @click.option("--note", required=True, help="Reason for closing the retained liability.")
    @click.pass_context
    def inference_close(ctx: Any, attempt_id: str, note: str) -> None:
        """Close an abandoned provider liability with an operator note."""
        invoke(ctx, "inference-recoveries", ("close-liability", attempt_id), recovery_note=note)

    inference_recoveries.add_command(inference_close, "close")

    @cli.group("artifacts", invoke_without_command=True)
    @click.pass_context
    def artifacts(ctx: Any) -> None:
        """Inspect operator-visible durable artifacts."""

        if ctx.invoked_subcommand is None:
            invoke(ctx, "artifacts", ("list",))

    @artifacts.command("list")
    @click.option("--limit", type=click.IntRange(min=1, max=1000), default=50, show_default=True)
    @click.pass_context
    def artifacts_list(ctx: Any, limit: int) -> None:
        """List the most recent operator-visible artifacts."""
        invoke(ctx, "artifacts", ("list",), operator_limit=limit)

    @cli.group("candidates", invoke_without_command=True)
    @click.pass_context
    def candidates(ctx: Any) -> None:
        """Review learned candidates across memory, skills, workflows, and synthesis."""

        if ctx.invoked_subcommand is None:
            invoke(ctx, "candidates", ("list",))

    @candidates.command("list")
    @click.option("--task-id", default=None)
    @click.pass_context
    def candidates_list(ctx: Any, task_id: str | None) -> None:
        """List the shared operator candidate queue."""
        invoke(ctx, "candidates", ("list",), operator_task_id=task_id)

    @candidates.command("inspect")
    @click.argument("candidate_id")
    @click.option("--task-id", default=None)
    @click.pass_context
    def candidates_inspect(ctx: Any, candidate_id: str, task_id: str | None) -> None:
        """Inspect one candidate from the shared queue."""
        invoke(ctx, "candidates", ("inspect", candidate_id), operator_task_id=task_id)

    @candidates.command("promote")
    @click.argument("candidate_id")
    @click.option("--scope", "target_scope", required=True, type=click.Choice(["project", "user"]))
    @click.option("--task-id", default=None)
    @click.pass_context
    def candidates_promote(
        ctx: Any, candidate_id: str, target_scope: str, task_id: str | None
    ) -> None:
        """Promote one candidate through its owning proof gate."""
        invoke(
            ctx,
            "candidates",
            ("promote", candidate_id),
            operator_scope=target_scope,
            operator_task_id=task_id,
        )

    @candidates.command("deprecate")
    @click.argument("candidate_id")
    @click.option("--task-id", default=None)
    @click.pass_context
    def candidates_deprecate(ctx: Any, candidate_id: str, task_id: str | None) -> None:
        """Deprecate one candidate."""
        invoke(ctx, "candidates", ("deprecate", candidate_id), operator_task_id=task_id)

    @cli.group("mutations", invoke_without_command=True)
    @click.pass_context
    def mutations(ctx: Any) -> None:
        """Inspect and undo durable mutation receipts."""

        if ctx.invoked_subcommand is None:
            invoke(ctx, "mutations", ("list",))

    @mutations.command("list")
    @click.option("--limit", type=click.IntRange(min=1, max=1000), default=25, show_default=True)
    @click.pass_context
    def mutations_list(ctx: Any, limit: int) -> None:
        """List recent mutation receipts."""
        invoke(ctx, "mutations", ("list",), operator_limit=limit)

    @mutations.command("undo")
    @click.argument("mutation_id")
    @click.pass_context
    def mutations_undo(ctx: Any, mutation_id: str) -> None:
        """Request an idempotent undo for one mutation receipt."""
        invoke(ctx, "mutations", ("undo", mutation_id))

    @cli.command("permissions")
    @click.pass_context
    def permissions(ctx: Any) -> None:
        """Show active grants and pending operator approvals."""
        invoke(ctx, "permissions")

    @cli.command("capabilities")
    @click.pass_context
    def capabilities(ctx: Any) -> None:
        """List currently available capability descriptors."""
        invoke(ctx, "capabilities")

    @cli.group("context", invoke_without_command=True)
    @click.pass_context
    def context(ctx: Any) -> None:
        """Inspect the compiled context summary for a session."""

        if ctx.invoked_subcommand is None:
            invoke(ctx, "context", ("show",))

    @context.command("show")
    @click.option("--session-id", default=None)
    @click.pass_context
    def context_show(ctx: Any, session_id: str | None) -> None:
        """Show context-window and causal-message counts."""
        invoke(ctx, "context", ("show",), operator_session_id=session_id)

    @cli.group("generated-capabilities", invoke_without_command=True)
    @click.pass_context
    def generated_capabilities(ctx: Any) -> None:
        """Inspect and govern generated capability candidates."""

        if ctx.invoked_subcommand is None:
            invoke(ctx, "generated-capabilities", ("list",))

    @generated_capabilities.command("list")
    @click.option("--task-id", default=None)
    @click.pass_context
    def generated_list(ctx: Any, task_id: str | None) -> None:
        """List generated capabilities visible to the operator."""
        invoke(ctx, "generated-capabilities", ("list",), operator_task_id=task_id)

    @generated_capabilities.command("show")
    @click.argument("capability_id")
    @click.option("--task-id", default=None)
    @click.pass_context
    def generated_show(ctx: Any, capability_id: str, task_id: str | None) -> None:
        """Inspect one generated capability."""
        invoke(
            ctx,
            "generated-capabilities",
            ("show", capability_id),
            operator_task_id=task_id,
        )

    @generated_capabilities.command("promote")
    @click.argument("capability_id")
    @click.argument("scope", type=click.Choice(["project", "user"]))
    @click.option("--task-id", default=None)
    @click.pass_context
    def generated_promote(ctx: Any, capability_id: str, scope: str, task_id: str | None) -> None:
        """Promote a generated capability through synthesis policy."""
        invoke(
            ctx,
            "generated-capabilities",
            ("promote", capability_id, scope),
            operator_task_id=task_id,
        )

    @generated_capabilities.command("deprecate")
    @click.argument("capability_id")
    @click.option("--task-id", default=None)
    @click.pass_context
    def generated_deprecate(ctx: Any, capability_id: str, task_id: str | None) -> None:
        """Deprecate a generated capability."""
        invoke(
            ctx,
            "generated-capabilities",
            ("deprecate", capability_id),
            operator_task_id=task_id,
        )

    @cli.command("completion")
    @click.argument(
        "shell",
        type=click.Choice(["bash", "zsh", "fish", "powershell"], case_sensitive=False),
    )
    def completion(shell: str) -> None:
        """Print a shell-completion script for the installed ``athena`` command."""
        click.echo(completion_source(cli, shell, click=click))


def _task_status_values() -> list[str]:
    from athena.protocol.tasks import TaskStatus

    return [status.value for status in TaskStatus]


def completion_source(cli: Any, shell: str, *, click: Any) -> str:
    """Return Click's generated completion script for ``athena``."""
    from click.shell_completion import get_completion_class

    shell_name = shell.casefold()
    completion_class = get_completion_class(shell_name)
    if completion_class is None:  # pragma: no cover - Click controls available shells
        raise click.ClickException(f"shell completion is unavailable for {shell_name}")
    completion_context = completion_class(cli, {}, "athena", "_ATHENA_COMPLETE")
    return completion_context.source()


OPERATOR_COMMANDS = frozenset(
    {
        "sessions",
        "tasks",
        "memory",
        "mcp",
        "jobs",
        "workflows",
        "packs",
        "skills",
        "inference-recoveries",
        "artifacts",
        "candidates",
        "mutations",
        "context",
        "generated-capabilities",
    }
)


def register_argparse_commands(sub: Any, globals_: Callable[[Any], None], argparse: Any) -> None:
    """Register the same nested operator families for Click-less installs."""

    def action_group(name: str, help_text: str) -> Any:
        group = sub.add_parser(name, help=help_text)
        globals_(group)
        return group.add_subparsers(dest=f"{name.replace('-', '_')}_action", metavar="COMMAND")

    sessions_sub = action_group("sessions", "List and manage durable sessions.")
    sessions_sub.add_parser("list", help="List durable sessions.")
    session_show = sessions_sub.add_parser("show", help="Show one durable session.")
    session_show.add_argument("session_id")
    session_close = sessions_sub.add_parser("close", help="Close one durable session.")
    session_close.add_argument("session_id")

    tasks_sub = action_group("tasks", "Inspect and control durable tasks.")
    task_list = tasks_sub.add_parser("list", help="List durable tasks.")
    task_list.add_argument("--status", choices=_task_status_values())
    task_show = tasks_sub.add_parser("show", aliases=["inspect"], help="Show a task forensic view.")
    task_show.add_argument("task_id")
    task_result = tasks_sub.add_parser("result", help="Print a durable task result.")
    task_result.add_argument("task_id")
    for action, help_text in (
        ("cancel", "Cancel a task."),
        ("interrupt", "Interrupt a task at the next safe boundary."),
        ("resume", "Resume an explicitly resumable task."),
    ):
        parser = tasks_sub.add_parser(action, help=help_text)
        parser.add_argument("task_id")
    task_steer = tasks_sub.add_parser("steer", help="Queue operator steering text.")
    task_steer.add_argument("task_id")
    task_steer.add_argument("text", nargs="+")
    task_steer.add_argument("--source-task-id", default=None)
    task_input = tasks_sub.add_parser("input", help="Answer a pending task input request.")
    task_input.add_argument("task_id")
    task_input.add_argument("answer", nargs="+")

    jobs_sub = action_group("jobs", "Inspect and control scheduled jobs.")
    jobs_list = jobs_sub.add_parser("list", help="List scheduled jobs.")
    jobs_list.add_argument("--enabled-only", action="store_true")
    for action, help_text in (
        ("show", "Show one scheduled job."),
        ("enable", "Enable a scheduled job."),
        ("disable", "Disable a scheduled job."),
    ):
        parser = jobs_sub.add_parser(action, help=help_text)
        parser.add_argument("job_id")
    jobs_run = jobs_sub.add_parser(
        "run-now", aliases=["run"], help="Run one occurrence immediately."
    )
    jobs_run.add_argument("job_id")
    jobs_grant = jobs_sub.add_parser("grant", help="Grant a task-bound schedule-control lease.")
    jobs_grant.add_argument("job_id")
    jobs_grant.add_argument("task_id")
    jobs_grant.add_argument("--principal-id", default=None)
    jobs_grant.add_argument("--project-id", default=None)
    jobs_grant.add_argument(
        "--operation",
        dest="operations",
        action="append",
        choices=["inspect", "update", "enable", "disable", "run"],
    )
    jobs_grant.add_argument("--expires-at", default=None)
    jobs_revoke = jobs_sub.add_parser("revoke", help="Revoke a schedule-control lease.")
    jobs_revoke.add_argument("job_id")

    workflows_sub = action_group(
        "workflows", "Inspect durable workflow definitions without executing them."
    )
    workflow_list = workflows_sub.add_parser("list", help="List visible workflows.")
    workflow_list.add_argument("--task-id", default=None)
    workflow_show = workflows_sub.add_parser(
        "show", aliases=["inspect", "describe"], help="Show one workflow."
    )
    workflow_show.add_argument("workflow_id")
    workflow_show.add_argument("legacy_task_id", nargs="?")
    workflow_show.add_argument("--task-id", default=None)

    packs_sub = action_group("packs", "Inspect and control declarative capability packs.")
    packs_sub.add_parser("list", help="List installed packs.")
    packs_search = packs_sub.add_parser("search", help="Search installed packs.")
    packs_search.add_argument("text")
    packs_inspect = packs_sub.add_parser("inspect", help="Inspect one installed pack.")
    packs_inspect.add_argument("pack_id")
    packs_install = packs_sub.add_parser("install", help="Install a local pack source.")
    packs_install.add_argument("source")
    packs_install.add_argument("--disabled", dest="install_disabled", action="store_true")
    for action, help_text in (
        ("enable", "Enable a pack."),
        ("disable", "Disable a pack."),
        ("remove", "Remove a pack."),
    ):
        parser = packs_sub.add_parser(
            action,
            aliases=["uninstall"] if action == "remove" else [],
            help=help_text,
        )
        parser.add_argument("pack_id")

    memory_sub = action_group("memory", "Review durable memory candidates.")
    memory_sub.add_parser("candidates", aliases=["list"], help="List pending memory candidates.")
    memory_inspect = memory_sub.add_parser("inspect", help="Inspect one memory candidate.")
    memory_inspect.add_argument("memory_id")
    memory_promote = memory_sub.add_parser("promote", help="Promote a memory candidate.")
    memory_promote.add_argument("memory_id")
    memory_promote.add_argument(
        "scope", choices=["job", "session", "project", "user", "global", "task"]
    )
    memory_promote.add_argument("scope_id", nargs="?")
    memory_discard = memory_sub.add_parser("discard", help="Discard a memory candidate.")
    memory_discard.add_argument("memory_id")

    mcp_sub = action_group("mcp", "Inspect and reconnect configured MCP servers.")
    mcp_sub.add_parser("list", aliases=["status"], help="List MCP server connection state.")
    for action, help_text in (
        ("tools", "List server tools."),
        ("doctor", "Probe server health."),
        ("reconnect", "Reconnect a server."),
    ):
        parser = mcp_sub.add_parser(action, help=help_text)
        parser.add_argument("server")
    mcp_sub.add_parser("resources", help="List MCP resources.")
    mcp_sub.add_parser("prompts", help="List MCP prompts.")

    skills_sub = action_group("skills", "Inspect and control installed skills.")
    skills_sub.add_parser("list", help="List installed skills.")
    skills_search = skills_sub.add_parser("search", help="Search installed skills.")
    skills_search.add_argument("text")
    for action, help_text in (
        ("inspect", "Inspect a skill."),
        ("enable", "Enable a skill."),
        ("disable", "Disable a skill."),
    ):
        parser = skills_sub.add_parser(action, help=help_text)
        parser.add_argument("skill_id")

    recovery_sub = action_group(
        "inference-recoveries", "Inspect and resolve uncertain provider attempts."
    )
    recovery_sub.add_parser("list", aliases=["status"], help="List unresolved attempts.")
    recovery_show = recovery_sub.add_parser("show", aliases=["inspect"], help="Show one attempt.")
    recovery_show.add_argument("attempt_id")
    recovery_resolve = recovery_sub.add_parser("resolve", help="Record a provider disposition.")
    recovery_resolve.add_argument("attempt_id")
    recovery_resolve.add_argument(
        "--resolution", required=True, choices=["failed", "succeeded", "retry", "abandon"]
    )
    recovery_resolve.add_argument("--note", required=True)
    recovery_resolve.add_argument("--provider-response-id", default=None)
    recovery_resolve.add_argument("--actual-cost", default=None)
    recovery_close = recovery_sub.add_parser(
        "close-liability", aliases=["close"], help="Close retained provider liability."
    )
    recovery_close.add_argument("attempt_id")
    recovery_close.add_argument("--note", required=True)

    artifacts_sub = action_group("artifacts", "Inspect operator-visible durable artifacts.")
    artifact_list = artifacts_sub.add_parser("list", help="List durable artifacts.")
    artifact_list.add_argument("--limit", type=int, default=50)

    candidates_sub = action_group("candidates", "Review learned operator candidates.")
    candidate_list = candidates_sub.add_parser("list", help="List candidates.")
    candidate_list.add_argument("--task-id", default=None)
    for action, help_text in (
        ("inspect", "Inspect a candidate."),
        ("deprecate", "Deprecate a candidate."),
    ):
        parser = candidates_sub.add_parser(action, help=help_text)
        parser.add_argument("candidate_id")
        parser.add_argument("--task-id", default=None)
    candidate_promote = candidates_sub.add_parser("promote", help="Promote a candidate.")
    candidate_promote.add_argument("candidate_id")
    candidate_promote.add_argument("--scope", required=True, choices=["project", "user"])
    candidate_promote.add_argument("--task-id", default=None)

    mutations_sub = action_group("mutations", "Inspect and undo mutation receipts.")
    mutation_list = mutations_sub.add_parser("list", help="List mutation receipts.")
    mutation_list.add_argument("--limit", type=int, default=25)
    mutation_undo = mutations_sub.add_parser("undo", help="Undo one mutation receipt.")
    mutation_undo.add_argument("mutation_id")

    permissions = sub.add_parser("permissions", help="Show grants and pending approvals.")
    globals_(permissions)

    context_sub = action_group("context", "Inspect a compiled context summary.")
    context_show = context_sub.add_parser("show", help="Show context summary.")
    context_show.add_argument("--session-id", default=None)

    generated_sub = action_group("generated-capabilities", "Inspect generated capabilities.")
    generated_list = generated_sub.add_parser("list", help="List generated capabilities.")
    generated_list.add_argument("--task-id", default=None)
    generated_show = generated_sub.add_parser("show", help="Inspect a generated capability.")
    generated_show.add_argument("capability_id")
    generated_show.add_argument("--task-id", default=None)
    generated_promote = generated_sub.add_parser("promote", help="Promote a generated capability.")
    generated_promote.add_argument("capability_id")
    generated_promote.add_argument("scope", choices=["project", "user"])
    generated_promote.add_argument("--task-id", default=None)
    generated_deprecate = generated_sub.add_parser(
        "deprecate", help="Deprecate a generated capability."
    )
    generated_deprecate.add_argument("capability_id")
    generated_deprecate.add_argument("--task-id", default=None)


def apply_argparse_operator_options(ns: Any, command: str, options: Any) -> None:
    """Translate an argparse operator namespace into the shared Options shape."""
    if command not in OPERATOR_COMMANDS:
        return
    group_key = command.replace("-", "_")
    action = getattr(ns, f"{group_key}_action", None)
    if command == "sessions":
        options.args = [] if action is None else [action]
        if action not in {None, "list"}:
            options.args.append(ns.session_id)
    elif command == "tasks":
        if action in {None, "list"}:
            options.args = [] if action is None else [action]
        elif action in {"show", "inspect", "result", "cancel", "interrupt", "resume"}:
            options.args = [action, ns.task_id]
        elif action == "steer":
            options.args = [action, ns.task_id, " ".join(ns.text)]
            options.steer_source_task_id = ns.source_task_id
        elif action == "input":
            options.args = [action, ns.task_id, " ".join(ns.answer)]
        options.task_status = getattr(ns, "status", None)
    elif command == "jobs":
        if action is None:
            options.args = []
        elif action == "list":
            options.args = [action]
            options.jobs_enabled_only = bool(ns.enabled_only)
        elif action in {"show", "enable", "disable", "run", "run-now", "revoke"}:
            options.args = [action, ns.job_id]
        elif action == "grant":
            options.args = [action, ns.job_id, ns.task_id]
            options.job_principal_id = ns.principal_id
            options.job_project_id = ns.project_id
            options.job_operations = tuple(ns.operations or ())
            options.job_expires_at = ns.expires_at
    elif command == "workflows":
        if action is None:
            options.args = []
        elif action == "list":
            options.args = [action] + ([ns.task_id] if ns.task_id else [])
        else:
            task_id = ns.task_id or getattr(ns, "legacy_task_id", None)
            options.args = [action, ns.workflow_id] + ([task_id] if task_id else [])
    elif command == "packs":
        if action is None or action == "list":
            options.args = [] if action is None else [action]
        elif action == "search":
            options.args = [action, ns.text]
        elif action in {"inspect", "enable", "disable", "remove", "uninstall"}:
            options.args = [action, ns.pack_id]
        elif action == "install":
            options.args = [action, ns.source]
            options.pack_enable = not ns.install_disabled
    elif command == "memory":
        if action is None or action in {"candidates", "list"}:
            options.args = [] if action is None else [action]
        elif action in {"inspect", "discard"}:
            options.args = [action, ns.memory_id]
        elif action == "promote":
            options.args = [action, ns.memory_id, ns.scope] + ([ns.scope_id] if ns.scope_id else [])
    elif command == "mcp":
        if action is None or action in {"list", "status", "resources", "prompts"}:
            options.args = [] if action is None else [action]
        else:
            options.args = [action, ns.server]
    elif command == "skills":
        if action is None or action == "list":
            options.args = [] if action is None else [action]
        else:
            options.args = [action, getattr(ns, "skill_id", getattr(ns, "text", ""))]
    elif command == "inference-recoveries":
        if action is None or action in {"list", "status"}:
            options.args = [] if action is None else [action]
        elif action in {"show", "inspect"}:
            options.args = [action, ns.attempt_id]
        elif action == "resolve":
            options.args = [action, ns.attempt_id]
            options.recovery_resolution = ns.resolution
            options.recovery_note = ns.note
            options.recovery_provider_response_id = ns.provider_response_id
            options.recovery_actual_cost = ns.actual_cost
        elif action in {"close", "close-liability"}:
            options.args = [action, ns.attempt_id]
            options.recovery_note = ns.note
    elif command == "artifacts":
        options.args = [] if action is None else [action]
        options.operator_limit = getattr(ns, "limit", 50)
    elif command == "candidates":
        if action is None or action == "list":
            options.args = [] if action is None else [action]
        else:
            options.args = [action, ns.candidate_id]
            options.operator_task_id = ns.task_id
            if action == "promote":
                options.operator_scope = ns.scope
    elif command == "mutations":
        options.args = [] if action is None else [action]
        options.operator_limit = getattr(ns, "limit", 25)
        if action == "undo":
            options.args.append(ns.mutation_id)
    elif command == "context":
        options.args = [] if action is None else [action]
        options.operator_session_id = getattr(ns, "session_id", None)
    elif command == "generated-capabilities":
        if action is None or action == "list":
            options.args = [] if action is None else [action]
            options.operator_task_id = getattr(ns, "task_id", None)
        else:
            options.args = [action, ns.capability_id]
            options.operator_task_id = getattr(ns, "task_id", None)
            if action == "promote":
                options.args.append(ns.scope)
