"""Policy-controlled dependency inspection and acquisition."""

from __future__ import annotations

import importlib.util
import re

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.execution import ExecutionRequest

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class DependencyCapability:
    descriptor = CapabilityDescriptor(
        id="dependency",
        description=(
            "Inspect, resolve, and policy-gated install of named dependencies. "
            "Installs are workspace-local and never accept shell flags or a "
            "free-form package manager command. Operations: inspect/resolve/install."
        ),
        input_schema={
            "type": "object", "required": ["operation", "name"],
            "properties": {
                "operation": {"type": "string", "enum": ["inspect", "resolve", "install"]},
                "name": {"type": "string", "minLength": 1, "maxLength": 128},
                "manager": {"type": "string", "enum": ["python"]},
                "version": {"type": "string", "maxLength": 64},
            },
            "additionalProperties": False,
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.EXECUTE,
            EffectClass.SPAWN_PROCESS, EffectClass.WRITE_LOCAL,
            EffectClass.NETWORK_WRITE,
        }),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, execution_manager=None) -> None:
        self._execution = execution_manager

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        name = str(args.get("name") or "").strip()
        manager = str(args.get("manager") or "python")
        if not _NAME.fullmatch(name):
            return _result(request, ok=False, error="invalid dependency name")
        if manager != "python":
            return _result(request, ok=False, error="only the python manager is supported")
        if operation in {"inspect", "resolve"}:
            found = importlib.util.find_spec(name.replace("-", "_")) is not None
            return _result(request, output=(f"{name}: {'installed' if found else 'missing'}"),
                           metadata={"name": name, "installed": found})
        if operation != "install":
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        if self._execution is None or context is None:
            return _result(request, ok=False, error="dependency install requires execution workspace")
        root = context.workspace.root
        target = f"{root}/.athena/dependencies"
        version = str(args.get("version") or "")
        package = name + (f"=={version}" if version else "")
        # This command is deliberately assembled from validated fields.  It
        # still goes through ExecutionManager, whose sandbox/network profile
        # and task cancellation own the actual process.
        source = (
            f"python -m pip install --disable-pip-version-check --no-input "
            f"--target {target!r} {package!r}"
        )
        result = await self._execution.execute(ExecutionRequest(
            runtime="shell", source=source, task_id=request.task_id or "dependency",
            workspace_id=context.workspace.id, cwd=root,
            network_policy=context.workspace.network_policy,
            workspace_root=root,
        ))
        ok = result.exit_code == 0
        return _result(request, ok=ok, output=result.stdout,
                       error=None if ok else result.stderr or "dependency install failed",
                       metadata={"exit_code": result.exit_code, "package": package,
                                 "target": target})


def _result(request, *, ok=True, output="", error=None, metadata=None):
    return CapabilityResult(
        request.call_id, request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output, error=error, metadata=dict(metadata or {}),
    )


__all__ = ["DependencyCapability"]
