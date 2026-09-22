"""Neutral environment inventory probes for CapabilityReflection.

These helpers gather bounded host/repository evidence only. They make no
policy decisions and never grant authority.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "_available_memory_bytes",
    "_command_inventory",
    "_graphics_inventory",
    "_git_remote_inventory",
    "_host_resource_inventory",
    "_permission_inventory",
    "_port_inventory",
]


def _available_memory_bytes() -> int | None:
    """Return bounded host memory evidence without invoking a subprocess."""
    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages) * int(page_size)
    except (AttributeError, OSError, ValueError):
        return None


def _fresh_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inventory_record(
    value: Any,
    *,
    source: str,
    available: bool | None = None,
    remediation: str | None = None,
) -> dict[str, Any]:
    if available is None:
        available = bool(value)
    return {
        "status": "available" if available else "unavailable",
        "value": value,
        "source": source,
        "fresh_at": _fresh_at(),
        "remediation": remediation if not available else None,
    }


def _command_inventory(names: tuple[str, ...], *, source: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for name in names:
        executable = shutil.which(name)
        records[name] = _inventory_record(
            {"executable": executable},
            source=source,
            available=executable is not None,
            remediation=f"install or configure {name}" if executable is None else None,
        )
    return records


def _port_inventory() -> dict[str, Any]:
    source = "/proc/net/tcp"
    listening = 0
    try:
        path = Path(source)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[1:4097]
        listening = sum(1 for line in lines if len(line.split()) > 3 and line.split()[3] == "0A")
        return _inventory_record({"listening_tcp": listening}, source=source, available=True)
    except OSError:
        return _inventory_record(
            {"listening_tcp": listening},
            source=source,
            available=False,
            remediation="expose a bounded host port inventory source",
        )


def _graphics_inventory() -> dict[str, Any]:
    displays = {
        "DISPLAY": os.environ.get("DISPLAY"),
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY"),
    }
    return _inventory_record(
        {"display_environment": displays, "xrandr": shutil.which("xrandr")},
        source="environment-and-PATH:graphics",
        available=bool(displays["DISPLAY"] or displays["WAYLAND_DISPLAY"]),
        remediation="attach a display server or configure a graphics adapter",
    )


def _permission_inventory(workspace_root: Path | None) -> dict[str, Any]:
    writable = bool(workspace_root and os.access(workspace_root, os.W_OK))
    value = {
        "uid": getattr(os, "getuid", lambda: None)(),
        "gid": getattr(os, "getgid", lambda: None)(),
        "workspace_writable": writable,
    }
    return _inventory_record(
        value,
        source="os.access-and-identity",
        available=workspace_root is None or writable,
        remediation="supply a writable workspace or grant the task's declared permission"
        if workspace_root is not None and not writable
        else None,
    )


def _host_resource_inventory(workspace_root: str | None) -> dict[str, Any]:
    """Return a small, bounded host-resource passport for planning."""
    resources: dict[str, Any] = {
        "cpu_count": os.cpu_count(),
        "load_average": None,
        "process_count": None,
        "workspace_disk": None,
        "mounts": [],
    }
    try:
        resources["load_average"] = [float(value) for value in os.getloadavg()]
    except (AttributeError, OSError):
        pass
    proc = Path("/proc")
    try:
        if proc.is_dir():
            process_count = 0
            for entry in proc.iterdir():
                if entry.name.isdigit():
                    process_count += 1
                if process_count >= 4096:
                    break
            resources["process_count"] = process_count
            mounts = proc / "mounts"
            if mounts.is_file():
                records: list[dict[str, str]] = []
                for line in mounts.read_text(encoding="utf-8", errors="replace").splitlines()[:64]:
                    fields = line.split()
                    if len(fields) >= 3:
                        records.append(
                            {
                                "target": fields[1][:256],
                                "filesystem": fields[2][:64],
                            }
                        )
                resources["mounts"] = records
    except OSError:
        pass
    if workspace_root:
        try:
            usage = shutil.disk_usage(workspace_root)
            resources["workspace_disk"] = {
                "total_bytes": int(usage.total),
                "free_bytes": int(usage.free),
                "used_bytes": int(usage.used),
            }
        except OSError:
            pass
    return resources


async def _git_remote_inventory(workspace_root: Path | None) -> list[dict[str, str]]:
    """List remote identities without exposing embedded credentials."""
    if workspace_root is None or not (workspace_root / ".git").exists():
        return []
    try:
        from athena.concurrency import run_blocking

        def _run_git():
            return subprocess.run(
                ["git", "-C", str(workspace_root), "remote", "-v"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )

        result = await run_blocking(_run_git)
    except (OSError, subprocess.SubprocessError):
        return []
    remotes: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in (result.stdout or "").splitlines()[:32]:
        fields = line.split()
        if len(fields) < 3:
            continue
        name, raw_url, direction = fields[:3]
        parsed = re.match(
            r"^(?:(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://)?(?:(?:[^/@]+)@)?(?P<host>[^/:]+)(?::\d+)?(?P<path>/.*)?$",
            raw_url,
        )
        safe_url = raw_url
        if parsed and parsed.group("host"):
            safe_url = f"{parsed.group('scheme') + '://' if parsed.group('scheme') else ''}{parsed.group('host')}{parsed.group('path') or ''}"
        key = (name, safe_url)
        if key not in seen:
            remotes.append({"name": name[:128], "url": safe_url[:512], "direction": direction[:16]})
            seen.add(key)
    return remotes


__all__ = [
    "_available_memory_bytes",
    "_command_inventory",
    "_graphics_inventory",
    "_git_remote_inventory",
    "_host_resource_inventory",
    "_permission_inventory",
    "_port_inventory",
]
