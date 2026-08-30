"""Canonical, secret-free environment identity for executable proofs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from athena.protocol.tasks import WorkspaceSpec


@dataclass(frozen=True)
class VerificationEnvironment:
    """Trusted, operator-selected toolchain for candidate verification.

    The project workspace remains the candidate's source of truth.  This
    object only binds the already-bootstrapped environment used to prove it;
    its paths are resolved by the host service, never by model input.
    """

    project_root: str
    python: str
    uv: str | None
    environment_root: str | None
    environment: Mapping[str, str] = field(default_factory=dict)
    readonly_mounts: tuple[str, ...] = ()

    @classmethod
    def from_project(cls, root: str, *, require_sandbox: bool = True) -> "VerificationEnvironment":
        project_root = str(Path(root).resolve())
        configured_root = os.environ.get("UV_PROJECT_ENVIRONMENT")
        environment_root = Path(configured_root or (Path(project_root) / ".venv"))
        if not environment_root.is_absolute():
            environment_root = Path(project_root) / environment_root
        environment_root = environment_root.resolve()
        python = environment_root / "bin" / "python"
        uv = shutil.which("uv")
        required = {
            "python": python,
            "ruff": environment_root / "bin" / "ruff",
            "mypy": environment_root / "bin" / "mypy",
            "pytest": environment_root / "bin" / "pytest",
        }
        missing = [name for name, path in required.items() if not path.is_file()]
        if uv is None:
            missing.append("uv")
        if require_sandbox and os.name == "posix" and shutil.which("bwrap") is None:
            missing.append("bubblewrap")
        if missing:
            raise ValueError(
                "development verification environment is not bootstrapped "
                f"(missing: {', '.join(missing)})"
            )
        mounts = [str(environment_root)]
        if uv is not None:
            mounts.insert(0, os.path.realpath(uv))
        return cls(
            project_root=project_root,
            python=str(python),
            uv=os.path.realpath(uv) if uv else None,
            environment_root=str(environment_root),
            environment={
                "UV_PROJECT_ENVIRONMENT": str(environment_root),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            readonly_mounts=tuple(dict.fromkeys(mounts)),
        )

    def for_workspace(self, workspace_root: str) -> dict[str, str]:
        """Return candidate-local imports plus this trusted environment."""
        root = str(Path(workspace_root).resolve())
        env = {str(key): str(value) for key, value in self.environment.items()}
        source_root = Path(root) / "src"
        if source_root.is_dir():
            env["PYTHONPATH"] = str(source_root)
        return env

    def to_record(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "python": self.python,
            "uv": self.uv,
            "environment_root": self.environment_root,
            "environment": dict(self.environment),
            "readonly_mounts": list(self.readonly_mounts),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "VerificationEnvironment":
        if not isinstance(record, Mapping):
            raise ValueError("verification environment record must be a mapping")
        project_root = str(record.get("project_root") or "")
        if not project_root:
            raise ValueError("verification environment has no project root")
        environment_root = record.get("environment_root")
        if environment_root is not None:
            environment_root = str(Path(str(environment_root)).resolve())
            if not os.path.isabs(environment_root):
                raise ValueError("verification environment root must be absolute")
            configured = dict(record.get("environment") or {}).get("UV_PROJECT_ENVIRONMENT")
            if configured:
                configured_path = Path(str(configured))
                if not configured_path.is_absolute():
                    configured_path = Path(project_root) / configured_path
                if configured_path.resolve() != Path(environment_root):
                    raise ValueError("verification environment root does not match its binding")
        mounts = tuple(str(path) for path in (record.get("readonly_mounts") or ()))
        if not mounts or any(not os.path.isabs(path) for path in mounts):
            raise ValueError("verification environment has invalid read-only mounts")
        return cls(
            project_root=project_root,
            python=str(record.get("python") or ""),
            uv=str(record["uv"]) if record.get("uv") else None,
            environment_root=str(environment_root) if environment_root else None,
            environment={
                str(key): str(value) for key, value in dict(record.get("environment") or {}).items()
            },
            readonly_mounts=mounts,
        )


class ProjectEnvironmentFingerprint:
    """Compute one stable identity for the environment a proof ran in."""

    _version_cache: dict[tuple[str, int, int], str] = {}

    def describe(
        self,
        workspace: WorkspaceSpec,
        *,
        extras: Mapping[str, Any] | None = None,
        project_profile: Any = None,
        toolchain_names: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        root = Path(workspace.root)
        relevant_files = self._relevant_file_hashes(root)
        record: dict[str, Any] = {
            "python_version": platform.python_version(),
            "python_executable": os.path.realpath(sys.executable),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "execution_backend": workspace.execution_backend or "local",
            "sandbox_backend": workspace.execution_backend or "local",
            "workspace_policy": {
                "readable": [
                    {"path": rule.path, "allow": rule.allow} for rule in workspace.readable
                ],
                "writable": [
                    {"path": rule.path, "allow": rule.allow} for rule in workspace.writable
                ],
                "mutation_mode": getattr(
                    workspace.mutation_mode,
                    "value",
                    workspace.mutation_mode,
                ),
            },
            "network_policy": getattr(
                workspace.network_policy,
                "value",
                workspace.network_policy,
            ),
            "workspace_revision": getattr(workspace, "revision", None),
            "dependency_lock_hash": relevant_files.get(".athena/dependencies.lock.json"),
            "relevant_file_hashes": relevant_files,
            "toolchain": self._toolchain_identity(
                project_profile,
                names=toolchain_names,
            ),
        }
        if extras:
            record["extras"] = dict(extras)
        return record

    def fingerprint(
        self,
        workspace: WorkspaceSpec,
        *,
        extras: Mapping[str, Any] | None = None,
        project_profile: Any = None,
        toolchain_names: tuple[str, ...] | None = None,
    ) -> str:
        payload = json.dumps(
            self.describe(
                workspace,
                extras=extras,
                project_profile=project_profile,
                toolchain_names=toolchain_names,
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def fingerprint_async(
        self,
        workspace: WorkspaceSpec,
        *,
        extras: Mapping[str, Any] | None = None,
        project_profile: Any = None,
        toolchain_names: tuple[str, ...] | None = None,
    ) -> str:
        """Compute the same identity with async tool-version probes."""
        # The relevant-file set is intentionally bounded to dependency and
        # build metadata. Keep this small synchronous walk on the loop while
        # the potentially slow executable probes below are truly async;
        # ``asyncio.to_thread`` is not reliable in every embedded runtime.
        relevant_files = self._relevant_file_hashes(Path(workspace.root))
        record: dict[str, Any] = {
            "python_version": platform.python_version(),
            "python_executable": os.path.realpath(sys.executable),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "execution_backend": workspace.execution_backend or "local",
            "sandbox_backend": workspace.execution_backend or "local",
            "workspace_policy": {
                "readable": [
                    {"path": rule.path, "allow": rule.allow} for rule in workspace.readable
                ],
                "writable": [
                    {"path": rule.path, "allow": rule.allow} for rule in workspace.writable
                ],
                "mutation_mode": getattr(
                    workspace.mutation_mode,
                    "value",
                    workspace.mutation_mode,
                ),
            },
            "network_policy": getattr(
                workspace.network_policy,
                "value",
                workspace.network_policy,
            ),
            "workspace_revision": getattr(workspace, "revision", None),
            "dependency_lock_hash": relevant_files.get(".athena/dependencies.lock.json"),
            "relevant_file_hashes": relevant_files,
            "toolchain": await self._toolchain_identity_async(
                project_profile,
                names=toolchain_names,
            ),
        }
        if extras:
            record["extras"] = dict(extras)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _relevant_file_hashes(root: Path) -> dict[str, str]:
        """Hash bounded dependency/build/config inputs, not all source files."""
        names = {
            ".athena/dependencies.lock.json",
            "requirements.txt",
            "Pipfile",
            "Pipfile.lock",
            "poetry.lock",
            "pyproject.toml",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "Cargo.toml",
            "Cargo.lock",
            "go.mod",
            "go.sum",
            "pom.xml",
            "build.gradle",
            "settings.gradle",
            "Gemfile",
            "Gemfile.lock",
            "composer.json",
            "composer.lock",
            "Makefile",
            "Dockerfile",
        }
        hashes: dict[str, str] = {}
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name
                not in {
                    ".git",
                    ".venv",
                    "venv",
                    "node_modules",
                    "__pycache__",
                }
            )
            for name in sorted(filenames):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if name not in names and relative not in names:
                    continue
                try:
                    hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
                except (OSError, ValueError):
                    continue
        return dict(sorted(hashes.items()))

    @classmethod
    def _toolchain_identity(
        cls,
        project_profile: Any,
        *,
        names: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        """Return stable identities from a small read-only tool allowlist."""
        values: dict[str, str] = {}
        profile_toolchain = getattr(project_profile, "toolchain", None)
        if isinstance(profile_toolchain, Mapping):
            for key, value in profile_toolchain.items():
                name, candidate = str(key), str(value)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    values[name] = cls._executable_identity(name, candidate)
                else:
                    values[name] = candidate
        for name in names or (
            "python",
            "python3",
            "node",
            "npm",
            "rustc",
            "cargo",
            "go",
            "java",
            "gradle",
        ):
            path = shutil.which(name)
            if path:
                values.setdefault(name, cls._executable_identity(name, path))
        return dict(sorted(values.items()))

    @classmethod
    def _executable_identity(cls, name: str, path: str) -> str:
        real = os.path.realpath(path)
        try:
            stat = os.stat(real)
        except OSError:
            return json.dumps({"path": real, "version": "unavailable"}, sort_keys=True)
        key = (real, int(stat.st_mtime_ns), int(stat.st_size))
        version = cls._version_cache.get(key)
        if version is None:
            version = cls._probe_version(name, real)
            cls._version_cache[key] = version
        return json.dumps(
            {
                "path": real,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "version": version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _probe_version(name: str, path: str) -> str:
        """Run only version flags for known toolchain executables."""
        flags = {
            "python": ("--version",),
            "python3": ("--version",),
            "node": ("--version",),
            "npm": ("--version",),
            "rustc": ("--version",),
            "cargo": ("--version",),
            "go": ("version",),
            "java": ("--version",),
            "gradle": ("--version",),
        }.get(name)
        if flags is None:
            return "unprobed"
        safe_path = os.path.dirname(path) or "."
        env = {"PATH": safe_path + os.pathsep + "/usr/bin:/bin"}
        try:
            result = subprocess.run(
                [path, *flags],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1.5,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "unavailable"
        output = (result.stdout or "").strip().splitlines()
        if result.returncode != 0 or not output:
            return "unavailable"
        return output[0][:256]

    @classmethod
    async def _toolchain_identity_async(
        cls,
        project_profile: Any,
        *,
        names: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        profile_toolchain = getattr(project_profile, "toolchain", None)
        candidates: dict[str, str] = {}
        if isinstance(profile_toolchain, Mapping):
            for key, value in profile_toolchain.items():
                name, candidate = str(key), str(value)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    candidates[name] = candidate
                else:
                    values[name] = candidate
        for name in names or (
            "python",
            "python3",
            "node",
            "npm",
            "rustc",
            "cargo",
            "go",
            "java",
            "gradle",
        ):
            path = shutil.which(name)
            if path:
                candidates.setdefault(name, path)
        results = await asyncio.gather(
            *(cls._executable_identity_async(name, path) for name, path in candidates.items())
        )
        values.update({name: value for name, value in results})
        return dict(sorted(values.items()))

    @classmethod
    async def _executable_identity_async(cls, name: str, path: str) -> tuple[str, str]:
        real = os.path.realpath(path)
        try:
            stat = os.stat(real)
        except OSError:
            return name, json.dumps({"path": real, "version": "unavailable"}, sort_keys=True)
        key = (real, int(stat.st_mtime_ns), int(stat.st_size))
        version = cls._version_cache.get(key)
        if version is None:
            version = await cls._probe_version_async(name, real)
            cls._version_cache[key] = version
        return name, json.dumps(
            {
                "path": real,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "version": version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    async def _probe_version_async(name: str, path: str) -> str:
        flags = {
            "python": ("--version",),
            "python3": ("--version",),
            "node": ("--version",),
            "npm": ("--version",),
            "rustc": ("--version",),
            "cargo": ("--version",),
            "go": ("version",),
            "java": ("--version",),
            "gradle": ("--version",),
        }.get(name)
        if flags is None:
            return "unprobed"
        safe_path = os.path.dirname(path) or "."
        env = {"PATH": safe_path + os.pathsep + "/usr/bin:/bin"}
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                path,
                *flags,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=1.5)
        except (OSError, asyncio.TimeoutError):
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            return "unavailable"
        text = (output or b"").decode("utf-8", errors="replace").strip().splitlines()
        if process.returncode != 0 or not text:
            return "unavailable"
        return text[0][:256]


__all__ = ["ProjectEnvironmentFingerprint"]
