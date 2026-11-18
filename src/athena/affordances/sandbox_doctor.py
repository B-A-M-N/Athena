"""Startup diagnostics for Athena's fail-closed local sandbox."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any


def _bubblewrap_probe() -> tuple[bool, str]:
    """Probe the exact network namespace primitive used by restricted code."""
    if os.name != "posix":
        return False, "restricted execution is unavailable on this platform"
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False, "bubblewrap is not installed; restricted execution fails closed"
    command = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-net",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        "/usr",
        "/usr",
        "/usr/bin/true",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"bubblewrap preflight failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip().splitlines()
        return False, f"bubblewrap preflight failed: {detail[-1] if detail else 'unknown error'}"
    try:
        version = subprocess.run(
            [bwrap, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = "version unavailable"
    return True, version or "version unavailable"


def profile_requires_restricted_execution(config: Any) -> bool:
    """Return whether configured required capabilities need Bubblewrap."""
    restricted_ids = {
        "sandbox",
        "sandboxed-local",
        "terminal",
        "terminal_session",
        "generated_capability",
        "synthesis",
    }
    for raw in getattr(config, "effective_required_capabilities", ()) or ():
        kind, separator, identifier = str(raw).partition(":")
        candidate = identifier if separator else kind
        if candidate.casefold() in restricted_ids:
            return True
    return False


def sandbox_status(config: Any = None) -> dict[str, Any]:
    """Return the concrete sandbox backend state for operator projections."""
    executable = shutil.which("bwrap") if os.name == "posix" else None
    available, detail = _bubblewrap_probe()
    configured = [
        str(value)
        for value in getattr(config, "effective_required_capabilities", ()) or ()
        if str(value).partition(":")[-1].casefold()
        in {
            "sandbox",
            "sandboxed-local",
            "terminal",
            "terminal_session",
            "generated_capability",
            "synthesis",
        }
    ]
    return {
        "backend": "bubblewrap",
        "available": available,
        "executable": executable,
        "detail": detail,
        "surfaces_require_it": [
            "restricted_execution",
            "generated_capability",
            "synthesis",
            "terminal_session",
        ],
        "configured_required_surfaces": configured,
    }
