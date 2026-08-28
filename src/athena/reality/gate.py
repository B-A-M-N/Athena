"""The execution reality boundary.

The capability name is not a sufficient safety boundary: ``execute``, a
generated capability, or a workflow can mutate a project without calling
``fs``.  ``RealityGate`` therefore classifies the concrete request at the
dispatcher boundary and, for speculative workspaces, binds every
project-sensitive operation to one sticky shadow workspace.

The gate is deliberately deterministic and has no model-facing decision.  A
normal direct workspace is untouched.  A speculative workspace is opened
lazily on the first project-sensitive operation and then reused for all later
operations in that task, so reads and writes observe one coherent reality.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    EffectClass,
)
from athena.protocol.tasks import MutationMode, WorkspaceSpec


class ExecutionDisposition(str, Enum):
    """How a concrete capability invocation reaches its target."""

    DIRECT = "direct"
    ISOLATED = "isolated"
    TRANSACTIONAL = "transactional"
    SPECULATIVE = "speculative"


@dataclass(frozen=True)
class RealityRoute:
    """Resolved workspace and audit metadata for one invocation."""

    workspace: WorkspaceSpec
    disposition: ExecutionDisposition
    transaction_id: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "transaction_id": self.transaction_id,
        }


class RealityGate:
    """Lazily bind speculative task actions to a shadow transaction."""

    _READ_ONLY_OPERATIONS = frozenset({
        "read", "list", "stat", "screen", "wait_for", "status", "inspect",
        "tree", "usage", "tables", "schema", "explain", "search", "recall",
        "sources", "evidence", "gaps", "describe", "dependencies", "provenance",
        "history", "created_this_task", "workflows", "skills", "runtimes",
        "permissions", "devices", "changed_files", "overview", "cpu", "memory",
        "disk", "network", "ports", "toolchain", "services", "gpu", "env",
    })
    _PROCESS_CAPABILITIES = frozenset({
        "execute", "terminal_session", "debugger", "process", "shell", "bash",
    })
    _PROJECT_CAPABILITIES = frozenset({
        "database", "dependency", "fs", "workspace", "workflow", "scratch",
    })

    def __init__(self, shadow_engine) -> None:
        self._shadow = shadow_engine
        self._active: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def active_branch(self, task_id: str | None):
        """Return the task's lazy transaction branch, if one exists."""
        return self._active.get(task_id) if task_id else None

    def active_branches(self) -> tuple[Any, ...]:
        return tuple(self._active.values())

    async def route(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        effects: Mapping[EffectClass, Any] | frozenset[EffectClass] | tuple[EffectClass, ...],
        descriptor: CapabilityDescriptor,
    ) -> RealityRoute:
        """Resolve the execution workspace for a concrete request.

        ``effects`` has already passed descriptor and policy resolution.  The
        gate only decides the reality target; it never expands capability
        authority or overrides policy.
        """
        mode = _mutation_mode(workspace.mutation_mode)
        if mode is MutationMode.DIRECT:
            return RealityRoute(workspace, ExecutionDisposition.DIRECT)

        # Once a task has a candidate reality, every workspace-bound read must
        # observe it too.  Otherwise ``fs.write -> fs.read`` would inspect two
        # different projects.  Unrelated stores (memory, schedules, research,
        # etc.) never enter this branch.
        active = self._active.get(request.task_id) if request.task_id else None
        if active is not None and self._is_workspace_bound(request, descriptor):
            if not _same_root(workspace.root, active.base_workspace.root):
                raise PermissionError(
                    "task workspace changed while a speculative transaction is active"
                )
            _translate_workspace_arguments(
                request, workspace.root, active.shadow_workspace.root
            )
            return RealityRoute(
                active.shadow_workspace,
                ExecutionDisposition.SPECULATIVE,
                transaction_id=active.id,
            )

        if not self._is_project_sensitive(request, effects, descriptor):
            return RealityRoute(workspace, ExecutionDisposition.DIRECT)
        if mode is MutationMode.READ_ONLY:
            raise PermissionError(
                f"project mutation denied by read-only workspace: {request.capability_id}"
            )
        if request.task_id is None:
            raise PermissionError(
                "speculative project mutations require a task-scoped invocation"
            )

        lock = self._locks.setdefault(request.task_id, asyncio.Lock())
        async with lock:
            branch = self._active.get(request.task_id)
            if branch is None:
                branch = await self._shadow.open_branch(
                    task_id=request.task_id,
                    base_workspace=workspace,
                    proposal=[],
                )
                self._active[request.task_id] = branch
            target = branch.shadow_workspace
            _translate_workspace_arguments(request, workspace.root, target.root)
            return RealityRoute(
                target,
                ExecutionDisposition.SPECULATIVE,
                transaction_id=branch.id,
            )

    @staticmethod
    def _is_project_sensitive(
        request: CapabilityRequest,
        effects,
        descriptor: CapabilityDescriptor,
    ) -> bool:
        """Classify mutation risk without relying on operation names alone."""
        capability_id = str(request.capability_id)
        operation = str(
            (request.arguments or {}).get("operation")
            or (request.arguments or {}).get("action")
            or ""
        ).casefold()
        effect_set = set(effects or ())

        # Arbitrary process/code execution is opaque: it can rewrite a project
        # regardless of whether the source happens to contain a write command.
        if capability_id in RealityGate._PROCESS_CAPABILITIES:
            return operation not in RealityGate._READ_ONLY_OPERATIONS

        # Generated/project affordances execute code supplied by the task and
        # inherit the same boundary even when their declared envelope only
        # contains EXECUTE/READ_LOCAL.
        origin = getattr(descriptor.origin, "value", descriptor.origin)
        if origin in {"generated", "project", "user"} and (
            EffectClass.EXECUTE in effect_set
            or EffectClass.WRITE_LOCAL in effect_set
            or EffectClass.DELETE in effect_set
        ):
            return True

        if capability_id in RealityGate._PROJECT_CAPABILITIES:
            if operation in RealityGate._READ_ONLY_OPERATIONS:
                return False
            return bool(
                effect_set
                & {EffectClass.WRITE_LOCAL, EffectClass.DELETE,
                   EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}
            )

        # A native capability with an explicit local mutation contract is
        # project-sensitive unless it is clearly a read-only operation. This
        # keeps new mutation-capable capabilities safe by default.
        return bool(
            effect_set & {EffectClass.WRITE_LOCAL, EffectClass.DELETE}
        ) and operation not in RealityGate._READ_ONLY_OPERATIONS

    @staticmethod
    def _is_workspace_bound(
        request: CapabilityRequest, descriptor: CapabilityDescriptor,
    ) -> bool:
        capability_id = str(request.capability_id)
        if capability_id in RealityGate._PROJECT_CAPABILITIES | RealityGate._PROCESS_CAPABILITIES:
            return True
        origin = getattr(descriptor.origin, "value", descriptor.origin)
        return origin in {"generated", "project", "user"}


def _mutation_mode(value: MutationMode | str | None) -> MutationMode:
    if isinstance(value, MutationMode):
        return value
    try:
        return MutationMode(str(value or MutationMode.DIRECT.value))
    except ValueError:
        return MutationMode.DIRECT


def _translate_workspace_arguments(
    request: CapabilityRequest, base_root: str, target_root: str,
) -> None:
    """Translate absolute task-root arguments into the shadow root.

    Relative paths remain relative to the routed workspace.  Shell source is
    intentionally not rewritten: absolute host paths are not made writable by
    the sandbox, which is safer than trying to parse arbitrary code.
    """
    base = os.path.realpath(os.path.abspath(base_root))
    target = os.path.realpath(os.path.abspath(target_root))
    args = dict(request.arguments or {})
    for key in ("path", "destination", "cwd", "workdir"):
        value = args.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            continue
        candidate = os.path.realpath(os.path.abspath(value))
        if candidate == base or candidate.startswith(base + os.sep):
            args[key] = target + candidate[len(base):]
    object.__setattr__(request, "arguments", args)


def _same_root(first: str, second: str) -> bool:
    return os.path.realpath(os.path.abspath(first)) == os.path.realpath(
        os.path.abspath(second)
    )


__all__ = ["ExecutionDisposition", "RealityGate", "RealityRoute"]
