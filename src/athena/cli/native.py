"""Launch the native Athena terminal frontend.

The native window owns the PTY and keyboard input. A Python session runs as
the PTY child and sends its canonical ``ProjectionState`` over the native
frontend's Unix-socket bridge. This keeps the native compositor a projection
of Athena rather than a second service or agent loop.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def native_binary() -> Path:
    """Return the development binary, with an explicit override for installs."""
    configured = os.environ.get("ATHENA_NATIVE_BIN")
    if configured:
        return Path(configured).expanduser()
    repository = Path(__file__).resolve().parents[3]
    return repository / "native" / "target" / "debug" / "athena-terminal"


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
    return command


def launch(options: Any) -> int:
    """Run the native terminal until its window or child session exits."""
    binary = native_binary()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        print(
            "athena native: native binary not found; build it with "
            "`cargo build --manifest-path native/Cargo.toml --offline` or "
            "set ATHENA_NATIVE_BIN",
            file=sys.stderr,
        )
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
        env = os.environ.copy()
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


__all__ = ["launch", "launch_async", "native_binary", "worker_command"]
