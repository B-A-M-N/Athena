"""Launch the native Athena terminal frontend.

The native window owns the PTY and keyboard input. A Python session runs as
the PTY child and sends its canonical ``ProjectionState`` over the native
frontend's Unix-socket bridge. This keeps the native compositor a projection
of Athena rather than a second service or agent loop.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import importlib.util
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


NATIVE_REQUIREMENTS_MESSAGE = (
    "Athena native frontend requires Linux x86_64 GNU/glibc >= 2.34, X11, Xft and OpenGL."
)
_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "FREEINFERENCE_API_KEY",
        "FREEINFERENCE_API_BASE_URL",
        "FREEINFERENCE_API_ENDPOINT",
        "FREEINFERENCE_MODEL",
    }
)
_ENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class NativePreflight:
    """Deterministic host checks for the Linux native companion."""

    def __init__(self, failures: tuple[str, ...] = ()) -> None:
        self.failures = failures

    @property
    def ok(self) -> bool:
        return not self.failures


def native_preflight() -> NativePreflight:
    """Check native frontend ABI and display prerequisites before launch."""
    failures: list[str] = []
    if platform.system() != "Linux":
        failures.append(f"operating system is {platform.system() or 'unknown'}, not Linux")
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64"}:
        failures.append(f"CPU architecture is {machine or 'unknown'}, not x86_64")

    libc_name, libc_version = platform.libc_ver()
    if libc_name.lower() != "glibc":
        failures.append(f"GNU/glibc is required (detected {libc_name or 'unknown'})")
    elif _version_tuple(libc_version) < (2, 34):
        failures.append(f"GNU/glibc >= 2.34 is required (detected {libc_version or 'unknown'})")

    libraries = {
        "X11": ctypes.util.find_library("X11"),
        "Xft": ctypes.util.find_library("Xft"),
        "OpenGL": ctypes.util.find_library("GL"),
    }
    missing = [name for name, library in libraries.items() if not library]
    if missing:
        failures.append(f"missing shared libraries: {', '.join(missing)}")

    display = os.environ.get("DISPLAY", "").strip()
    if not display:
        failures.append("DISPLAY is not set")
    elif libraries["X11"] and not _display_is_usable(libraries["X11"], display):
        failures.append(f"DISPLAY {display!r} is not usable")
    return NativePreflight(tuple(failures))


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split(".")[:2])
    except (AttributeError, TypeError, ValueError):
        return ()


def _display_is_usable(library: str, display_name: str) -> bool:
    """Open and close the configured X11 display without spawning a probe."""
    try:
        x11 = ctypes.CDLL(library)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        handle = x11.XOpenDisplay(display_name.encode("utf-8"))
        if not handle:
            return False
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay(handle)
        return True
    except (AttributeError, OSError, UnicodeError):
        return False


def native_binary() -> Path:
    """Resolve a packaged/release native binary without guessing a debug path."""
    configured = os.environ.get("ATHENA_NATIVE_BIN")
    if configured:
        return Path(configured).expanduser()
    try:
        spec = importlib.util.find_spec("athena_native")
        if spec is not None and spec.origin is not None:
            companion = Path(spec.origin).resolve().parent / "athena-terminal"
            if companion.is_file():
                return companion
    except (ImportError, OSError, RuntimeError, ValueError):
        pass
    installed = Path(sys.prefix) / "bin" / "athena-terminal"
    if installed.is_file():
        return installed
    repository = Path(__file__).resolve().parents[3]
    return repository / "native" / "target" / "release" / "athena-terminal"


def worker_command(options: Any) -> list[str]:
    """Build the PTY-child command without copying credentials into argv."""
    command = [sys.executable, "-m", "athena.cli.native_session"]
    for flag, value in (
        ("--config", getattr(options, "config_path", None)),
        ("--db", getattr(options, "db_path", None)),
        ("--workspace", getattr(options, "workspace", None)),
        ("--autonomy", getattr(options, "autonomy", None)),
        ("--model", getattr(options, "model", None)),
        ("--criteria", getattr(options, "criteria", None)),
    ):
        if value:
            command.extend((flag, str(value)))
    if getattr(options, "verbose", False):
        command.append("--verbose")
    if getattr(options, "mascot", None):
        command.extend(("--mascot", str(options.mascot)))
    if getattr(options, "animations", True) is False:
        command.append("--no-animations")
    if getattr(options, "reduced_motion", False):
        command.append("--reduced-motion")
    return command


def _load_credential_env(env: dict[str, str]) -> None:
    """Load supported provider values from the user's private env file.

    This is deliberately a small assignment parser rather than ``source``:
    launching Athena must not execute arbitrary shell code from a credential
    file. Existing process values win, and only the provider settings needed
    by the native worker are considered.
    """
    candidates: list[Path] = []
    configured = env.get("ATHENA_CREDENTIAL_ENV_FILE")
    if configured:
        candidates.append(Path(configured).expanduser())
    config_home = Path(env.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    candidates.append(config_home / "opencodex" / "opencodex.env")
    seen: set[Path] = set()
    for path in candidates:
        path = path.resolve(strict=False)
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _ENV_ASSIGNMENT.match(line.strip())
            if match is None or match.group(1) not in _CREDENTIAL_ENV_NAMES:
                continue
            name, raw_value = match.groups()
            try:
                parsed = shlex.split(raw_value, comments=True)
            except ValueError:
                continue
            value = parsed[0] if parsed else ""
            if value and name not in env:
                env[name] = value
        return


def launch(options: Any) -> int:
    """Run the native terminal until its window or child session exits."""
    binary = native_binary()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        print(
            "athena native: native binary not found; build it with "
            "`cargo build --release --manifest-path native/Cargo.toml --offline`, "
            "install the packaged native asset, or set ATHENA_NATIVE_BIN",
            file=sys.stderr,
        )
        return 2
    preflight = native_preflight()
    if not preflight.ok:
        print(f"athena native: {NATIVE_REQUIREMENTS_MESSAGE}", file=sys.stderr)
        for failure in preflight.failures:
            print(f"  {failure}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="athena-native-") as runtime:
        socket_path = str(Path(runtime) / "projection.sock")
        argv = [
            str(binary),
            "--bridge-socket",
            socket_path,
            "--command",
            shlex.join(worker_command(options)),
        ]
        if getattr(options, "mascot", None):
            argv.extend(("--mascot", str(options.mascot)))
        if getattr(options, "animations", True) is False:
            argv.append("--no-animations")
        if getattr(options, "reduced_motion", False):
            argv.append("--reduced-motion")
        env = os.environ.copy()
        _load_credential_env(env)
        repository_src = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = repository_src + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        try:
            process = subprocess.run(  # architecture-lint: allow subprocess-outside-approved-backends reason=owned native frontend
                argv, env=env, check=False
            )
        except OSError as exc:
            print(f"athena native: could not launch {binary}: {exc}", file=sys.stderr)
            return 2
    return int(process.returncode)


async def launch_async(options: Any) -> int:
    """Async wrapper used by callers that already own an event loop."""
    return await asyncio.to_thread(launch, options)


__all__ = [
    "NATIVE_REQUIREMENTS_MESSAGE",
    "NativePreflight",
    "launch",
    "launch_async",
    "native_binary",
    "native_preflight",
    "worker_command",
]
