"""Trusted host-selected environments for candidate verification."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolchainBinding:
    """One trusted executable and the read-only roots it needs."""

    name: str
    executable: str
    readonly_roots: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationEnvironment:
    """Trusted, operator-selected toolchain for candidate verification.

    The project workspace remains the candidate's source of truth. This object
    only binds the already-bootstrapped environment used to prove it; its paths
    are resolved by the host service, never by model input.
    """

    project_root: str
    python: str
    uv: str | None
    environment_root: str | None
    environment: Mapping[str, str] = field(default_factory=dict)
    readonly_mounts: tuple[str, ...] = ()
    writable_mounts: tuple[str, ...] = ()
    base_root: str | None = None
    toolchains: tuple[ToolchainBinding, ...] = ()

    @classmethod
    def from_project(
        cls,
        root: str,
        *,
        require_sandbox: bool = True,
        include_project_root: bool = False,
        include_rust: bool = False,
        task_id: str | None = None,
    ) -> "VerificationEnvironment":
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
        cargo = shutil.which("cargo") if include_rust else None
        rustc = shutil.which("rustc") if include_rust else None
        compiler_command = None
        if include_rust:
            compiler_command = shutil.which("gcc") or shutil.which("clang") or shutil.which("cc")
        if include_rust and cargo is None:
            missing.append("cargo")
        if include_rust and rustc is None:
            missing.append("rustc")
        if include_rust and compiler_command is None:
            missing.append("C compiler (gcc, clang, or cc)")
        if missing:
            raise ValueError(
                "development verification environment is not bootstrapped "
                f"(missing: {', '.join(missing)})"
            )
        mounts = [str(environment_root)]
        if uv is not None:
            mounts.insert(0, os.path.realpath(uv))
        environment: dict[str, str] = {
            "UV_PROJECT_ENVIRONMENT": str(environment_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        toolchains: list[ToolchainBinding] = []
        if include_project_root:
            mounts.append(project_root)
        if include_rust and cargo is not None and rustc is not None:
            cargo_home = Path(
                os.environ.get("CARGO_HOME") or (Path(os.environ.get("HOME", "")) / ".cargo")
            ).resolve()
            rustup_home = Path(
                os.environ.get("RUSTUP_HOME") or (Path(os.environ.get("HOME", "")) / ".rustup")
            ).resolve()
            cargo_launcher = Path(cargo).resolve() if cargo else None
            rustc_launcher = Path(rustc).resolve() if rustc else None
            compiler = Path(compiler_command).resolve() if compiler_command else None
            cargo_bin = Path(cargo).absolute().parent if cargo else None
            rustup_settings = rustup_home / "settings.toml"
            uv_cache_raw = os.environ.get("UV_CACHE_DIR")
            if uv_cache_raw:
                uv_cache = Path(uv_cache_raw).resolve()
            elif os.environ.get("XDG_CACHE_HOME"):
                uv_cache = (Path(os.environ["XDG_CACHE_HOME"]) / "uv").resolve()
            else:
                uv_cache = (Path.home() / ".cache" / "uv").resolve()
            ar = shutil.which("ar")
            ranlib = shutil.which("ranlib")
            rust_roots = tuple(
                str(path)
                for path in (
                    cargo_bin,
                    cargo_home / "registry",
                    cargo_home / "git",
                    rustup_settings if rustup_settings.is_file() else None,
                    rustup_home / "toolchains",
                    compiler,
                )
                if path is not None and path.exists()
            )
            mounts.extend((*rust_roots,))
            rust_environment: dict[str, str] = {
                "CARGO_HOME": str(cargo_home),
                "RUSTUP_HOME": str(rustup_home),
                "CARGO_NET_OFFLINE": "true",
            }
            if compiler is not None:
                rust_environment["CC"] = str(compiler)
                target = _rust_target_for_host()
                if target:
                    rust_environment[f"CARGO_TARGET_{target.upper().replace('-', '_')}_LINKER"] = (
                        str(compiler)
                    )
                if ar:
                    rust_environment["AR"] = str(Path(ar).resolve())
                if ranlib:
                    rust_environment["RANLIB"] = str(Path(ranlib).resolve())
            if task_id is not None:
                # Candidate proof must never receive a writable bind of the
                # operator's real UV cache. Derive a private cache from the
                # durable task ID so restart resolves the same environment.
                # This is a host-side copy, not hard links: candidate writes
                # cannot reach the source cache through shared inodes.
                cache_key = hashlib.sha256(f"{project_root}:{task_id}".encode("utf-8")).hexdigest()[
                    :24
                ]
                private_cache = (
                    Path(tempfile.gettempdir()) / f"athena-self-proof-cache-{cache_key}"
                ).resolve()
                private_cache.mkdir(parents=True, exist_ok=True)
                if uv_cache.is_dir() and uv_cache != private_cache:
                    shutil.copytree(uv_cache, private_cache, dirs_exist_ok=True)
                rust_environment["UV_CACHE_DIR"] = str(private_cache)
            environment.update(rust_environment)
            toolchains.append(
                ToolchainBinding(
                    name="rust",
                    executable=str(Path(cargo).absolute()),
                    readonly_roots=tuple(
                        path
                        for path in (
                            str(Path(rustc).absolute()),
                            str(rustc_launcher) if rustc_launcher else "",
                            str(cargo_launcher) if cargo_launcher else "",
                            str(compiler) if compiler else "",
                            str(Path(ar).resolve()) if ar else "",
                            str(Path(ranlib).resolve()) if ranlib else "",
                            *rust_roots,
                        )
                        if path
                    ),
                    environment=rust_environment,
                )
            )
        return cls(
            project_root=project_root,
            python=str(python),
            uv=os.path.realpath(uv) if uv else None,
            environment_root=str(environment_root),
            environment=environment,
            readonly_mounts=tuple(dict.fromkeys(mounts)),
            writable_mounts=(
                (str(Path(environment["UV_CACHE_DIR"]).resolve()),)
                if environment.get("UV_CACHE_DIR")
                else ()
            ),
            base_root=project_root if include_project_root else None,
            toolchains=tuple(toolchains),
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
            "writable_mounts": list(self.writable_mounts),
            "base_root": self.base_root,
            "toolchains": [
                {
                    "name": binding.name,
                    "executable": binding.executable,
                    "readonly_roots": list(binding.readonly_roots),
                    "environment": dict(binding.environment),
                }
                for binding in self.toolchains
            ],
        }

    def identity(self) -> dict[str, Any]:
        """Return the canonical identity used for authority comparisons.

        A persisted environment record is useful evidence, but its strings
        are not permission to mount anything. The service compares this
        identity with a freshly resolved, host-owned environment before it
        allows the record to reach an executor.
        """
        return {
            "project_root": str(Path(self.project_root).resolve()),
            "python": str(Path(self.python).resolve()),
            "uv": str(Path(self.uv).resolve()) if self.uv else None,
            "environment_root": (
                str(Path(self.environment_root).resolve()) if self.environment_root else None
            ),
            "environment": dict(sorted((str(k), str(v)) for k, v in self.environment.items())),
            "readonly_mounts": sorted(str(Path(path).resolve()) for path in self.readonly_mounts),
            "writable_mounts": sorted(str(Path(path).resolve()) for path in self.writable_mounts),
            "base_root": str(Path(self.base_root).resolve()) if self.base_root else None,
            "toolchains": [
                {
                    "name": binding.name,
                    "executable": str(Path(binding.executable).resolve()),
                    "readonly_roots": sorted(
                        str(Path(path).resolve()) for path in binding.readonly_roots
                    ),
                    "environment": dict(
                        sorted((str(k), str(v)) for k, v in binding.environment.items())
                    ),
                }
                for binding in sorted(
                    self.toolchains,
                    key=lambda value: (value.name, value.executable),
                )
            ],
        }

    @classmethod
    def from_record(
        cls,
        record: Mapping[str, Any],
        *,
        expected: "VerificationEnvironment | None" = None,
    ) -> "VerificationEnvironment":
        if not isinstance(record, Mapping):
            raise ValueError("verification environment record must be a mapping")
        project_root = str(record.get("project_root") or "")
        if not project_root or not Path(project_root).is_absolute():
            raise ValueError("verification environment has no project root")
        raw_environment_root = record.get("environment_root")
        environment_root = None
        if raw_environment_root is not None:
            if not Path(str(raw_environment_root)).is_absolute():
                raise ValueError("verification environment root must be absolute")
            environment_root = str(Path(str(raw_environment_root)).resolve())
        if environment_root is None:
            raise ValueError("verification environment root is required")
        try:
            environment = {
                str(key): str(value) for key, value in dict(record.get("environment") or {}).items()
            }
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("verification environment has invalid bindings") from exc
        allowed_environment = {
            "UV_PROJECT_ENVIRONMENT",
            "PYTHONDONTWRITEBYTECODE",
            "CARGO_HOME",
            "RUSTUP_HOME",
            "CARGO_NET_OFFLINE",
            "CC",
            "AR",
            "RANLIB",
            "UV_CACHE_DIR",
        }
        if set(environment) - allowed_environment:
            raise ValueError("verification environment contains an untrusted binding")
        configured = environment.get("UV_PROJECT_ENVIRONMENT")
        if configured and environment_root:
            configured_path = Path(configured)
            if not configured_path.is_absolute():
                raise ValueError("verification environment binding must be absolute")
            if configured_path.resolve() != Path(environment_root):
                raise ValueError("verification environment root does not match its binding")
        python = str(record.get("python") or "")
        if not python or not Path(python).is_absolute():
            raise ValueError("verification environment has invalid Python executable")
        if environment_root:
            python_path = Path(python).resolve()
            if python_path != Path(environment_root) / "bin" / "python":
                raise ValueError("verification environment Python is outside its environment")
        uv = str(record["uv"]) if record.get("uv") else None
        if uv is not None and not Path(uv).is_absolute():
            raise ValueError("verification environment has invalid uv executable")
        mounts = tuple(str(path) for path in (record.get("readonly_mounts") or ()))
        if not mounts or any(not os.path.isabs(path) for path in mounts):
            raise ValueError("verification environment has invalid read-only mounts")
        writable_mounts = tuple(str(path) for path in (record.get("writable_mounts") or ()))
        if any(not os.path.isabs(path) for path in writable_mounts):
            raise ValueError("verification environment has invalid writable mounts")
        base_root = record.get("base_root")
        if base_root is not None:
            if not Path(str(base_root)).is_absolute():
                raise ValueError("verification environment base root must be absolute")
            base_root = str(Path(str(base_root)).resolve())
            if base_root != str(Path(project_root).resolve()):
                raise ValueError("verification environment base root does not match its project")
        allowed_mounts = {str(Path(environment_root).resolve())}
        if uv:
            allowed_mounts.add(str(Path(uv).resolve()))
        if base_root:
            allowed_mounts.add(base_root)
        if environment.get("UV_CACHE_DIR"):
            allowed_mounts.add(str(Path(environment["UV_CACHE_DIR"]).resolve()))
        raw_toolchains = record.get("toolchains") or ()
        toolchains: list[ToolchainBinding] = []
        try:
            for raw in raw_toolchains:
                if not isinstance(raw, Mapping):
                    raise ValueError("verification environment has invalid toolchain")
                executable = str(raw.get("executable") or "")
                roots = tuple(str(path) for path in raw.get("readonly_roots") or ())
                if not executable or not Path(executable).is_absolute():
                    raise ValueError("verification environment has invalid toolchain executable")
                if any(not Path(path).is_absolute() for path in roots):
                    raise ValueError("verification environment has invalid toolchain root")
                binding_environment = {
                    str(key): str(value)
                    for key, value in dict(raw.get("environment") or {}).items()
                }
                if set(binding_environment) - allowed_environment:
                    raise ValueError("verification toolchain contains an untrusted binding")
                toolchains.append(
                    ToolchainBinding(
                        name=str(raw.get("name") or ""),
                        executable=executable,
                        readonly_roots=roots,
                        environment=binding_environment,
                    )
                )
                allowed_mounts.update(
                    {
                        str(Path(executable).resolve()),
                        *(str(Path(path).resolve()) for path in roots),
                    }
                )
        except (AttributeError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("verification environment has invalid toolchains") from exc
        if any(str(Path(path).resolve()) not in allowed_mounts for path in mounts):
            raise ValueError("verification environment contains an untrusted mount")
        if any(str(Path(path).resolve()) not in allowed_mounts for path in writable_mounts):
            raise ValueError("verification environment contains an untrusted writable mount")
        resolved_mounts = {str(Path(path).resolve()) for path in mounts}
        if str(Path(environment_root).resolve()) not in resolved_mounts:
            raise ValueError("verification environment root is not mounted")
        if uv is not None and str(Path(uv).resolve()) not in resolved_mounts:
            raise ValueError("verification environment uv is not mounted")
        parsed = cls(
            project_root=str(Path(project_root).resolve()),
            python=python,
            uv=uv,
            environment_root=str(environment_root) if environment_root else None,
            environment=environment,
            readonly_mounts=mounts,
            writable_mounts=writable_mounts,
            base_root=base_root,
            toolchains=tuple(toolchains),
        )
        if expected is not None and parsed.identity() != expected.identity():
            raise ValueError("verification environment does not match trusted service identity")
        return parsed


def _rust_target_for_host() -> str | None:
    """Return the stable GNU target key used for the verified host linker."""
    if sys.platform != "linux":
        return None
    return {
        "x86_64": "x86_64-unknown-linux-gnu",
        "amd64": "x86_64-unknown-linux-gnu",
        "aarch64": "aarch64-unknown-linux-gnu",
        "arm64": "aarch64-unknown-linux-gnu",
    }.get(platform.machine().lower())


__all__ = ["ToolchainBinding", "VerificationEnvironment"]
