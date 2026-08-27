"""Policy-controlled dependency inspection and acquisition."""

from __future__ import annotations

import importlib.util
import importlib.metadata
import json
import os
import platform
import re
import sys
import tempfile
from pathlib import Path

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.execution import ExecutionRequest
from athena.protocol.messages import utcnow

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
            lock = _read_lock(context)
            record = lock.get("packages", {}).get(name) if lock else None
            found = record is not None or importlib.util.find_spec(name.replace("-", "_")) is not None
            metadata = {"name": name, "installed": found}
            if record:
                metadata["lock"] = record
                output = f"{name}: installed ({record.get('resolved_version', 'unknown')})"
            else:
                output = f"{name}: {'installed' if found else 'missing'}"
            return _result(request, output=output, metadata=metadata)
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
        lock_record = None
        lock_error = None
        if ok:
            try:
                lock_record = _record_installed_package(
                    target, name=name, requested_version=version,
                    task_id=request.task_id or "dependency", call_id=request.call_id,
                )
                _write_lock(context, lock_record)
            except (OSError, TypeError, ValueError) as exc:
                # The package may have been installed, but a successful
                # acquisition without a reproducibility record is not a
                # successful Athena dependency operation.
                lock_error = f"dependency installed but lock recording failed: {exc}"
                ok = False
        return _result(request, ok=ok, output=result.stdout,
                       error=None if ok else lock_error or result.stderr or "dependency install failed",
                       metadata={"exit_code": result.exit_code, "package": package,
                                 "target": target, **({"lock": lock_record} if lock_record else {})})


def _lock_path(context) -> Path:
    workspace = getattr(context, "workspace", None)
    if workspace is None or not getattr(workspace, "root", None):
        raise ValueError("dependency operation requires workspace context")
    return Path(workspace.root) / ".athena" / "dependencies.lock.json"


def _read_lock(context) -> dict:
    try:
        data = json.loads(_lock_path(context).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_installed_package(
    target: str, *, name: str, requested_version: str,
    task_id: str, call_id: str,
) -> dict:
    """Build a lock record from the installed distribution's own metadata."""
    normalized = name.replace("-", "_").casefold()
    distribution = next(
        (
            candidate for candidate in importlib.metadata.distributions(path=[target])
            if str(candidate.metadata.get("Name") or "")
            .replace("-", "_").casefold() == normalized
        ),
        None,
    )
    if distribution is None:
        raise ValueError(f"could not find installed distribution for {name}")
    resolved_version = str(distribution.version or "")
    if not resolved_version:
        raise ValueError(f"could not resolve installed version for {name}")
    record_hashes: list[str] = []
    record_text = distribution.read_text("RECORD")
    if record_text:
        for line in record_text.splitlines():
            parts = line.split(",", 2)
            if len(parts) >= 2 and parts[1].startswith("sha256="):
                record_hashes.append(f"{parts[0]}:{parts[1]}")
        record_hashes.sort()
    return {
        "name": name,
        "manager": "python",
        "requested_version": requested_version or None,
        "resolved_version": resolved_version,
        "source": "python-index",
        "target": target,
        "record_hashes": record_hashes[:10_000],
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "owner": {"task_id": task_id, "call_id": call_id},
        "recorded_at": utcnow().isoformat(),
    }


def _write_lock(context, record: dict) -> None:
    path = _lock_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _read_lock(context)
    lock.setdefault("format", 1)
    lock.setdefault("packages", {})[str(record["name"])] = record
    fd, tmp_name = tempfile.mkstemp(prefix="dependencies.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(lock, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _result(request, *, ok=True, output="", error=None, metadata=None):
    return CapabilityResult(
        request.call_id, request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output, error=error, metadata=dict(metadata or {}),
    )


__all__ = ["DependencyCapability"]
