"""Startup diagnostics for Athena's fail-closed local sandbox."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
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


def _profile_requires_restricted_execution(config: Any) -> bool:
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


def doctor_startup(o: Any, config: Any) -> int:
    """Start the service briefly and report readiness-owned checks."""
    from athena.cli.app import ServiceUnavailable, build_service

    sandbox_ready, sandbox_detail = _bubblewrap_probe()
    requires_sandbox = _profile_requires_restricted_execution(config)
    if sandbox_ready:
        print(f"Sandbox: READY (bubblewrap {sandbox_detail})")
    else:
        print(f"Sandbox: UNAVAILABLE (fail-closed: {sandbox_detail})")
    try:
        service = build_service(config)
    except ServiceUnavailable as exc:
        print(f"startup health: failed ({exc})")
        return 1

    async def probe() -> dict[str, Any]:
        try:
            await service.start()
            health = service.startup_health()
            health["operational_matrix"] = service.operational_matrix()
            return health
        finally:
            await service.stop()

    try:
        health = asyncio.run(probe())
    except Exception as exc:  # broad-exception: startup diagnostics fail closed
        print(f"startup health: failed ({exc})")
        return 1
    print(f"startup health: {health.get('status', 'unknown')}")
    for name, check in (health.get("checks") or {}).items():
        status = check.get("status", "unknown") if isinstance(check, dict) else "unknown"
        print(f"  {name}: {status}")
    matrix = health.get("operational_matrix") or {}
    for cell in matrix.get("execution") or ():
        print(
            "  execution/{backend}/{runtime}: available={available} persistent={persistent} "
            "state={state} reattach={reattach} deps={deps}".format(
                backend=cell.get("backend"),
                runtime=cell.get("runtime"),
                available="yes" if cell.get("available") else "no",
                persistent="yes" if cell.get("persistent_session") else "no",
                state="yes" if cell.get("persistent_runtime_state") else "no",
                reattach="yes" if cell.get("reattach") else "no",
                deps=",".join(cell.get("dependency_installation") or ()) or "none",
            )
        )
    mcp = matrix.get("mcp") or {}
    if isinstance(mcp, dict):
        for name, status in sorted(mcp.items()):
            state = status.get("state", "unknown") if isinstance(status, dict) else "unknown"
            print(f"  mcp/{name}: {state}")
    memory = matrix.get("memory") or {}
    if isinstance(memory, dict):
        print(f"  semantic-memory: {memory.get('state', 'unknown')}")
    if requires_sandbox and not sandbox_ready:
        print("  capability profile: restricted execution is not ready", file=sys.stderr)
        return 1
    return 0 if health.get("status") == "ok" else 1
