"""Policy-controlled dependency inspection and acquisition."""

from __future__ import annotations

import importlib.util
import importlib.metadata
import hashlib
import json
import os
import platform
import shlex
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.execution.dependencies import environment_fingerprint, record_hashes, verify_record_files
from athena.execution.dependencies import resolve_dependency_environment
from athena.affordances.models import DependencyRequirement
from athena.protocol.execution import ExecutionRequest
from athena.protocol.messages import utcnow

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HOST_INTERPRETER_BACKENDS = frozenset(
    {"local", "shadow", "sandbox", "sandboxed-local", "verification"}
)


class DependencyManager(Protocol):
    """Contract implemented by policy-routed dependency managers."""

    name: str

    def package_spec(self, name: str, version: str) -> str: ...

    def target(self, root: str) -> str: ...


class PythonDependencyManager:
    """The first dependency manager, with no free-form shell surface."""

    name = "python"

    @staticmethod
    def package_spec(name: str, version: str) -> str:
        return f"{name}=={version}"

    @staticmethod
    def target(root: str) -> str:
        return str(Path(root) / ".athena" / "dependencies")


class NodeDependencyManager:
    """Structured npm acquisition into a workspace-local prefix."""

    name = "node"

    @staticmethod
    def package_spec(name: str, version: str) -> str:
        if not re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", name):
            raise ValueError("invalid npm package name")
        return f"{name}@{version}" if version else name

    @staticmethod
    def target(root: str) -> str:
        return str(Path(root) / ".athena" / "node")


class DependencyCapability:
    descriptor = CapabilityDescriptor(
        id="dependency",
        description=(
            "Inspect, resolve, and policy-gated install of named dependencies. "
            "Installs are workspace-local and never accept shell flags or a "
            "free-form package manager command. Operations: inspect/resolve/"
            "install/replay."
        ),
        input_schema={
            "type": "object",
            "required": ["operation", "name"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["inspect", "resolve", "install", "replay"],
                },
                "name": {"type": "string", "minLength": 1, "maxLength": 128},
                "manager": {"type": "string", "enum": ["python", "node"]},
                "version": {"type": "string", "maxLength": 64},
            },
            "additionalProperties": False,
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
                EffectClass.NETWORK_WRITE,
            }
        ),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, execution_manager=None) -> None:
        self._execution = execution_manager
        self._managers: dict[str, DependencyManager] = {
            "python": PythonDependencyManager(),
            "node": NodeDependencyManager(),
        }

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        name = str(args.get("name") or "").strip()
        manager = str(args.get("manager") or "python")
        valid_name = _NAME.fullmatch(name) or (
            manager == "node" and re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", name)
        )
        if not valid_name:
            return _result(request, ok=False, error="invalid dependency name")
        dependency_manager = self._managers.get(manager)
        if dependency_manager is None:
            return _result(request, ok=False, error=f"unsupported dependency manager: {manager}")
        backend = getattr(getattr(context, "workspace", None), "execution_backend", None) or "local"
        if operation in {"inspect", "resolve"}:
            lock = _read_lock(context)
            record = _lock_record(lock, name) if lock else None
            host_installed = (
                importlib.util.find_spec(name.replace("-", "_")) is not None
                if manager == "python"
                else False
            )
            task_runtime_available = await self._runtime_probe(
                request, context=context, name=name, record=record, manager=manager
            )
            metadata = {
                "name": name,
                "installed": host_installed,
                "host_installed": host_installed,
                "task_runtime_available": task_runtime_available,
                "workspace_locked": record is not None,
                "environment": {
                    "task_execution_backend": backend,
                    "task_runtime": "host-python"
                    if backend in _HOST_INTERPRETER_BACKENDS
                    else backend,
                    "host_interpreter": sys.executable,
                },
            }
            if record:
                metadata["lock"] = record
                output = f"{name}: installed ({record.get('resolved_version', 'unknown')})"
            else:
                runtime_state = (
                    "available"
                    if task_runtime_available is True
                    else "missing"
                    if task_runtime_available is False
                    else "unknown"
                )
                output = f"{name}: host={'installed' if host_installed else 'missing'}; task-runtime={runtime_state}"
            return _result(request, output=output, metadata=metadata)
        if operation != "install":
            if operation == "replay":
                return await self._replay(
                    request,
                    context=context,
                    manager=dependency_manager,
                    name=name,
                )
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        if self._execution is None or context is None:
            return _result(
                request, ok=False, error="dependency install requires execution workspace"
            )
        capabilities_fn = getattr(self._execution, "backend_capabilities", None)
        capabilities = (
            capabilities_fn(backend)
            if callable(capabilities_fn)
            else {"dependency_installation": ("python",) if backend == "local" else ()}
        )
        supported_managers = (
            tuple(getattr(capabilities, "dependency_installation", ()))
            if not isinstance(capabilities, dict)
            else tuple(capabilities.get("dependency_installation", ()))
        )
        if manager not in supported_managers:
            return _result(
                request,
                ok=False,
                error=(
                    f"dependency {operation} is unsupported for execution backend {backend!r}; "
                    f"backend supports: {', '.join(supported_managers) or 'none'}"
                ),
            )
        root = context.workspace.root
        target = dependency_manager.target(root)
        Path(target).mkdir(parents=True, exist_ok=True)
        command_target = target
        if backend == "container":
            command_target = (
                "/workspace/.athena/dependencies"
                if manager == "python"
                else "/workspace/.athena/node"
            )
        version = str(args.get("version") or "")
        package = dependency_manager.package_spec(name, version) if version else name
        # This command is deliberately assembled from validated fields.  It
        # still goes through ExecutionManager, whose sandbox/network profile
        # and task cancellation own the actual process.
        if manager == "python":
            executable = sys.executable if backend in _HOST_INTERPRETER_BACKENDS else "python3"
            source = (
                f"{shlex.quote(executable)} -m pip install --disable-pip-version-check --no-input "
                f"--target {shlex.quote(command_target)} {shlex.quote(package)}"
            )
        else:
            source = (
                f"npm install --ignore-scripts --no-audit --no-fund --prefix {shlex.quote(command_target)} "
                f"{shlex.quote(package)}"
            )
        result = await self._execution.execute(
            ExecutionRequest(
                runtime="shell",
                source=source,
                task_id=request.task_id or "dependency",
                workspace_id=context.workspace.id,
                backend=context.workspace.execution_backend or "local",
                cwd=root,
                network_policy=context.workspace.network_policy,
                workspace_root=root,
            )
        )
        ok = result.exit_code == 0
        lock_record = None
        lock_error = None
        if ok:
            try:
                lock_record = (
                    _record_installed_package(
                        target,
                        name=name,
                        requested_version=version,
                        task_id=request.task_id or "dependency",
                        call_id=request.call_id,
                    )
                    if manager == "python"
                    else _record_node_package(
                        target,
                        name=name,
                        requested_version=version,
                        task_id=request.task_id or "dependency",
                        call_id=request.call_id,
                    )
                )
                _write_lock(context, lock_record)
            except (OSError, TypeError, ValueError) as exc:
                # The package may have been installed, but a successful
                # acquisition without a reproducibility record is not a
                # successful Athena dependency operation.
                lock_error = f"dependency installed but lock recording failed: {exc}"
                ok = False
        return _result(
            request,
            ok=ok,
            output=result.stdout,
            error=None if ok else lock_error or result.stderr or "dependency install failed",
            metadata={
                "exit_code": result.exit_code,
                "package": package,
                "target": target,
                **({"lock": lock_record} if lock_record else {}),
            },
        )

    async def _runtime_probe(
        self, request, *, context, name: str, record: dict | None, manager: str
    ):
        """Probe the actual task Python, rather than reporting host imports."""
        if self._execution is None or context is None:
            return None
        backend = getattr(context.workspace, "execution_backend", None) or "local"
        if backend not in _HOST_INTERPRETER_BACKENDS:
            return None
        if manager == "node":
            target = NodeDependencyManager.target(context.workspace.root)
            source = (
                "try { require.resolve(" + repr(name) + ", {paths: [" + repr(target) + "]}); "
                "console.log('1'); } catch (_) { console.log('0'); }"
            )
            runtime = "node"
        else:
            target = PythonDependencyManager.target(context.workspace.root)
            source = (
                "import importlib.util; "
                f"print('1' if importlib.util.find_spec({name.replace('-', '_')!r}) else '0')"
            )
            runtime = "python"
        result = await self._execution.execute(
            ExecutionRequest(
                runtime=runtime,
                source=source,
                task_id=request.task_id or "dependency-inspect",
                workspace_id=context.workspace.id,
                backend=context.workspace.execution_backend or "local",
                cwd=context.workspace.root,
                network_policy=context.workspace.network_policy,
                workspace_root=context.workspace.root,
                env={"PYTHONPATH": target} if manager == "python" and os.path.isdir(target) else {},
            )
        )
        if result.exit_code != 0:
            return False
        return result.stdout.strip().splitlines()[-1:] == ["1"]

    async def _replay(
        self,
        request: CapabilityRequest,
        *,
        context: Any,
        manager: DependencyManager,
        name: str,
    ):
        """Replay one exact lock entry through the governed executor."""
        if self._execution is None or context is None:
            return _result(
                request, ok=False, error="dependency replay requires execution workspace"
            )
        backend = getattr(context.workspace, "execution_backend", None) or "local"
        capabilities_fn = getattr(self._execution, "backend_capabilities", None)
        capabilities = (
            capabilities_fn(backend)
            if callable(capabilities_fn)
            else {"dependency_installation": ("python",) if backend == "local" else ()}
        )
        supported_managers = (
            tuple(getattr(capabilities, "dependency_installation", ()))
            if not isinstance(capabilities, dict)
            else tuple(capabilities.get("dependency_installation", ()))
        )
        if manager.name not in supported_managers:
            return _result(
                request,
                ok=False,
                error=(
                    f"dependency replay is unsupported for execution backend {backend!r}; "
                    f"backend supports: {', '.join(supported_managers) or 'none'}"
                ),
            )
        lock = _read_lock(context)
        record = _lock_record(lock, name)
        if record is None:
            return _result(request, ok=False, error=f"dependency {name!r} is not in the lock")
        if str(record.get("manager") or "") != manager.name:
            return _result(
                request, ok=False, error="locked dependency manager does not match request"
            )
        version = str(record.get("resolved_version") or "")
        expected_hashes = sorted(str(item) for item in record.get("record_hashes") or ())
        if not version or not expected_hashes:
            return _result(
                request,
                ok=False,
                error="lock entry lacks a resolved version or content hashes; replay refused",
            )
        root = str(context.workspace.root)
        target = manager.target(root)
        Path(target).mkdir(parents=True, exist_ok=True)
        command_target = target
        if backend == "container":
            command_target = (
                "/workspace/.athena/dependencies"
                if manager.name == "python"
                else "/workspace/.athena/node"
            )
        if manager.name == "python":
            executable = sys.executable if backend in _HOST_INTERPRETER_BACKENDS else "python3"
            locked_closure = record.get("closure")
            replay_packages = (
                locked_closure
                if isinstance(locked_closure, list) and locked_closure
                else [record]
            )
            package_specs = " ".join(
                shlex.quote(manager.package_spec(package_name, package_version))
                for item in replay_packages
                for package_name, package_version in [
                    (
                        str(item.get("name") or (name if item is record else "")),
                        str(item.get("resolved_version") or ""),
                    )
                ]
                if package_name and package_version
            )
            source = (
                f"{shlex.quote(executable)} -m pip install --disable-pip-version-check --no-input --no-deps "
                f"--upgrade --target {shlex.quote(command_target)} "
                f"{package_specs}"
            )
        else:
            source = (
                f"npm install --ignore-scripts --no-audit --no-fund --prefix {shlex.quote(command_target)} "
                f"{shlex.quote(manager.package_spec(name, version))}"
            )
        result = await self._execution.execute(
            ExecutionRequest(
                runtime="shell",
                source=source,
                task_id=request.task_id or "dependency",
                workspace_id=context.workspace.id,
                backend=context.workspace.execution_backend or "local",
                cwd=root,
                network_policy=context.workspace.network_policy,
                workspace_root=root,
            )
        )
        if result.exit_code != 0:
            return _result(
                request,
                ok=False,
                output=result.stdout,
                error=result.stderr or "dependency replay failed",
                metadata={"exit_code": result.exit_code, "target": target},
            )
        if manager.name == "node":
            try:
                node_verified = _record_node_package(
                    target,
                    name=name,
                    requested_version=version,
                    task_id=request.task_id or "dependency",
                    call_id=request.call_id,
                )
                if expected_hashes and sorted(node_verified["record_hashes"]) != expected_hashes:
                    raise ValueError("npm package metadata hash mismatch")
                expected_lock_hash = str(record.get("package_lock_sha256") or "")
                if expected_lock_hash and node_verified.get("package_lock_sha256") != expected_lock_hash:
                    raise ValueError("npm package-lock.json hash mismatch")
            except (OSError, TypeError, ValueError) as exc:
                return _result(
                    request,
                    ok=False,
                    output=result.stdout,
                    error=f"dependency replay verification failed: {exc}",
                    metadata={"exit_code": result.exit_code, "target": target},
                )
            return _result(
                request,
                output=result.stdout,
                metadata={
                    "exit_code": result.exit_code,
                    "target": target,
                    "manager": manager.name,
                    "lock": dict(record),
                    "environment": {"target": target, "manager": manager.name},
                },
            )
        try:
            environment = resolve_dependency_environment(
                root,
                (
                    DependencyRequirement(
                        name=name,
                        manager=manager.name,
                        version=version,
                    ),
                ),
                expected_fingerprint=record.get("environment_fingerprint"),
            )
        except (OSError, TypeError, ValueError) as exc:
            return _result(
                request,
                ok=False,
                output=result.stdout,
                error=f"dependency replay verification failed: {exc}",
                metadata={"exit_code": result.exit_code, "target": target},
            )
        return _result(
            request,
            output=result.stdout,
            metadata={
                "exit_code": result.exit_code,
                "target": target,
                "manager": manager.name,
                "lock": dict(record),
                "environment": environment.to_metadata(),
                "provenance": {
                    "task_id": request.task_id,
                    "call_id": request.call_id,
                    "lock_source": str(_lock_path(context)),
                },
            },
        )


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


def _lock_record(lock: dict, name: str) -> dict[str, Any] | None:
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict):
        return None
    wanted = name.replace("-", "_").casefold()
    for key, value in packages.items():
        if str(key).replace("-", "_").casefold() == wanted and isinstance(value, dict):
            return value
    return None


def _record_installed_package(
    target: str,
    *,
    name: str,
    requested_version: str,
    task_id: str,
    call_id: str,
) -> dict:
    """Build a lock record from the installed distribution's own metadata."""
    normalized = name.replace("-", "_").casefold()
    distribution = next(
        (
            candidate
            for candidate in importlib.metadata.distributions(path=[target])
            if str(candidate.metadata.get("Name") or "").replace("-", "_").casefold() == normalized
        ),
        None,
    )
    if distribution is None:
        raise ValueError(f"could not find installed distribution for {name}")
    resolved_version = str(distribution.version or "")
    if not resolved_version:
        raise ValueError(f"could not resolve installed version for {name}")
    hashes = record_hashes(distribution)
    closure = _python_dependency_closure(target, distribution)
    runtime_identity = _runtime_identity()
    record = {
        "name": name,
        "manager": "python",
        "requested_version": requested_version or None,
        "resolved_version": resolved_version,
        "source": "python-index",
        "target": target,
        "record_hashes": hashes,
        "closure": closure,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "runtime_identity": runtime_identity,
        "owner": {"task_id": task_id, "call_id": call_id},
        "recorded_at": utcnow().isoformat(),
    }
    record["environment_fingerprint"] = environment_fingerprint(
        [
            {
                "name": item["name"],
                "resolved_version": item["resolved_version"],
                "record_hashes": item["record_hashes"],
            }
            for item in closure
        ],
        runtime_identity=runtime_identity,
    )
    return record


def _python_dependency_closure(target: str, root_distribution: Any) -> list[dict[str, Any]]:
    """Capture the installed transitive closure, not just the requested root."""
    distributions = {
        str(item.metadata.get("Name") or "").replace("-", "_").casefold(): item
        for item in importlib.metadata.distributions(path=[target])
        if item.metadata.get("Name")
    }
    queue = [root_distribution]
    visited: set[str] = set()
    closure: list[dict[str, Any]] = []
    while queue:
        distribution = queue.pop(0)
        name = str(distribution.metadata.get("Name") or "").strip()
        normalized = name.replace("-", "_").casefold()
        if not name or normalized in visited:
            continue
        visited.add(normalized)
        if callable(getattr(distribution, "locate_file", None)):
            verify_record_files(distribution)
        closure.append(
            {
                "name": name,
                "resolved_version": str(distribution.version or ""),
                "record_hashes": record_hashes(distribution),
                "runtime_identity": _runtime_identity(),
            }
        )
        for raw_requirement in getattr(distribution, "requires", None) or ():
            dependency_name = _requirement_name(raw_requirement)
            dependency = distributions.get(dependency_name.replace("-", "_").casefold())
            if dependency is not None:
                queue.append(dependency)
    if not closure:
        raise ValueError("installed dependency closure is empty")
    return sorted(closure, key=lambda item: str(item["name"]).casefold())


def _requirement_name(raw_requirement: str) -> str:
    try:
        from packaging.requirements import Requirement

        return Requirement(str(raw_requirement)).name
    except (ImportError, TypeError, ValueError):
        return re.split(r"[ <>=!~;\[]", str(raw_requirement), maxsplit=1)[0].strip()


def _record_node_package(
    target: str,
    *,
    name: str,
    requested_version: str,
    task_id: str,
    call_id: str,
) -> dict:
    package_json = Path(target) / "node_modules" / Path(name) / "package.json"
    try:
        raw = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"could not find installed npm package metadata for {name}") from exc
    resolved_version = str(raw.get("version") or "")
    if not resolved_version:
        raise ValueError(f"npm package {name!r} has no resolved version")
    package_hash = _sha256_file(package_json)
    lock_path = Path(target) / "package-lock.json"
    if not lock_path.is_file():
        raise ValueError("npm package-lock.json is missing; reproducible replay is refused")
    lock_hash = _sha256_file(lock_path)
    lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_packages = lock_data.get("packages") if isinstance(lock_data, dict) else None
    if not isinstance(lock_packages, dict):
        raise ValueError("npm package-lock.json has no package map")
    identity = lock_packages.get(f"node_modules/{name}") or {}
    if not isinstance(identity, dict) or str(identity.get("version") or "") != resolved_version:
        raise ValueError("npm package identity does not match package-lock.json")
    return {
        "name": name,
        "manager": "node",
        "requested_version": requested_version or None,
        "resolved_version": resolved_version,
        "source": "npm-registry",
        "target": target,
        "record_hashes": [f"package.json:sha256={package_hash}"],
        "package_lock_sha256": lock_hash,
        "package_lock_identity": {
            "name": name,
            "version": resolved_version,
            "resolved": identity.get("resolved"),
            "integrity": identity.get("integrity"),
        },
        "runtime_identity": "node",
        "owner": {"task_id": task_id, "call_id": call_id},
        "recorded_at": utcnow().isoformat(),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_identity() -> str:
    from athena.execution.dependencies import _python_runtime_identity

    return _python_runtime_identity()


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
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


__all__ = ["DependencyCapability"]
