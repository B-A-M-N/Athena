"""`process` + `machine` — rich process control and machine introspection (P0).

`process`: inspect, list tree, and resource usage for OS processes. Mutating
operations are restricted to live Athena-owned runtime processes. Arbitrary
host-process control, when enabled in a deployment, must use a separately
authorized host-process capability.

`machine`: read-only introspection of the machine Athena runs on —
CPU/memory/disk/network/ports/environment/toolchain. No mutation ops.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
from typing import Any, ClassVar

from athena.execution.process_tree import process_start_identity
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


def _result(request, ok=True, output="", error="", meta=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=None if ok else error,
        metadata=dict(meta or {}),
    )


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"


_UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:%\\-]{0,254}$")


def _valid_unit_name(value: str) -> bool:
    return _UNIT_NAME.fullmatch(value) is not None


# ---------------------------------------------------------------------------
# process
# ---------------------------------------------------------------------------


class ProcessCapability:
    """Inspect host processes; mutate only Athena-owned runtime processes."""

    descriptor = CapabilityDescriptor(
        id="process",
        description=(
            "Process control: list processes, inspect one (cmdline/exe/cwd/"
            "status/fd count), inspect the full process tree of a PID, check "
            "resource usage, write to a process's stdin, send signals "
            "(TERM/KILL/INT/HUP/custom), and wait for exit. Operations: "
            "list/inspect/tree/usage/write_stdin/signal/wait."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "inspect", "tree", "usage", "write_stdin", "signal", "wait"],
                },
                "pid": {"type": "integer"},
                "start_identity": {"type": "string", "minLength": 1},
                "pattern": {"type": "string"},
                "limit": {"type": "integer"},
                "signal": {"type": "string"},
                "text": {"type": "string"},
                "timeout": {"type": "number"},
            },
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.PRIVILEGED,
            }
        ),
        origin=CapabilityOrigin.NATIVE,
    )

    _SIGNALS: ClassVar[dict[str, int]] = {
        "TERM": 15,
        "KILL": 9,
        "INT": 2,
        "HUP": 1,
        "QUIT": 3,
        "USR1": 10,
        "USR2": 12,
        "CONT": 18,
        "STOP": 19,
    }

    def __init__(self, execution_manager=None) -> None:
        self.execution_manager = execution_manager

    def _owns(
        self,
        request: CapabilityRequest,
        pid: int,
        start_identity: str | None = None,
    ) -> bool:
        return bool(
            self.execution_manager is not None
            and self.execution_manager.owns_process(request.task_id, pid, start_identity)
        )

    def _require_owned(
        self,
        request: CapabilityRequest,
        pid: int,
        start_identity: str | None = None,
    ) -> CapabilityResult | None:
        if not os.path.exists(f"/proc/{pid}"):
            return _result(request, ok=False, error=f"no such pid {pid}")
        if not self._owns(request, pid, start_identity):
            return _result(
                request,
                ok=False,
                error="process is not a live Athena-owned runtime process",
            )
        return None

    async def invoke(self, request: CapabilityRequest, **kwargs) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        loop = asyncio.get_running_loop()

        if op == "list":
            limit = max(int(args.get("limit") or 40), 1)

            def _ps():
                _rc, out, _err = _run(
                    ["ps", "-eo", "pid,ppid,pcpu,pmem,etime,comm", "--no-headers", "--sort=-pcpu"]
                )
                lines = out.splitlines()[:limit]
                return "\n".join(lines)

            out = await loop.run_in_executor(None, _ps)
            return _result(request, output=out or "(none)")

        pid = int(args.get("pid") or 0)
        if op != "list" and pid <= 0:
            return _result(request, ok=False, error="pid required")

        if op == "inspect":

            def _inspect():
                info: dict[str, Any] = {"pid": pid}
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        info["cmdline"] = (
                            f.read().replace(b"\0", b" ").decode(errors="replace").strip()
                        )
                    with open(f"/proc/{pid}/status") as f:
                        for line in f:
                            if line.startswith(
                                ("State:", "PPid:", "Threads:", "VmRSS:", "VmSize:")
                            ):
                                k, _, v = line.partition(":")
                                info[k.strip().lower()] = v.strip()
                    try:
                        info["cwd"] = os.readlink(f"/proc/{pid}/cwd")
                    except OSError:
                        pass
                    try:
                        info["exe"] = os.readlink(f"/proc/{pid}/exe")
                    except OSError:
                        pass
                    try:
                        info["start_identity"] = process_start_identity(pid)
                    except OSError:
                        pass
                except FileNotFoundError:
                    return None
                except PermissionError as exc:
                    return {"pid": pid, "error": str(exc)}
                return info

            info = await loop.run_in_executor(None, _inspect)
            if info is None:
                return _result(request, ok=False, error=f"no such pid {pid}")
            return _result(request, output=json.dumps(info, indent=2))

        if op == "tree":

            def _tree():
                _rc, out, _ = _run(["ps", "-eo", "pid,ppid,pgid,comm", "--no-headers"])
                rows = []
                for ln in out.splitlines():
                    parts = ln.split(None, 3)
                    if len(parts) == 4:
                        rows.append((int(parts[0]), int(parts[1]), parts[2], parts[3]))
                by_parent: dict[int, list] = {}
                for p, pp, pg, c in rows:
                    by_parent.setdefault(pp, []).append((p, pp, pg, c))
                collected: list[str] = []

                def walk(pid_, depth):
                    for p, pp, pg, c in by_parent.get(pid_, []):
                        collected.append(f"{'  ' * depth}{p} {c}")
                        walk(p, depth + 1)

                walk(pid, 0)
                root = next((r for r in rows if r[0] == pid), None)
                head = f"{root[0]} {root[3]}" if root else f"(root {pid} gone)"
                return "\n".join([head] + collected)

            tree = await loop.run_in_executor(None, _tree)
            return _result(request, output=tree or f"(no children of {pid})")

        if op == "usage":

            def _usage():
                _rc, out, _ = _run(["ps", "-p", str(pid), "-o", "pid,pcpu,pmem,rss,vsz,etime,comm"])
                return out.strip()

            out = await loop.run_in_executor(None, _usage)
            return _result(request, output=out)

        if op == "write_stdin":
            owned_error = self._require_owned(
                request, pid, str(args.get("start_identity") or "") or None
            )
            if owned_error is not None:
                return owned_error
            text = str(args.get("text") or "")
            fd = f"/proc/{pid}/fd/0"
            if not os.access(fd, os.W_OK):
                return _result(request, ok=False, error=f"stdin of {pid} not writable by athena")
            fd_handle = os.open(fd, os.O_WRONLY)

            def _write():
                try:
                    os.write(fd_handle, (text + "\n").encode())
                finally:
                    os.close(fd_handle)

            await loop.run_in_executor(None, _write)
            return _result(request, output="written")

        if op == "signal":
            owned_error = self._require_owned(
                request, pid, str(args.get("start_identity") or "") or None
            )
            if owned_error is not None:
                return owned_error
            sig_name = str(args.get("signal") or "TERM").upper()
            sig = self._SIGNALS.get(sig_name)
            if sig is None and sig_name.isdigit():
                sig = int(sig_name)
            if sig is None:
                return _result(request, ok=False, error=f"unknown signal {sig_name}")

            def _kill():
                os.kill(pid, sig)

            try:
                await loop.run_in_executor(None, _kill)
            except ProcessLookupError:
                return _result(request, ok=False, error=f"no such pid {pid}")
            except PermissionError:
                return _result(request, ok=False, error=f"permission denied for pid {pid}")
            return _result(request, output=f"sent {sig_name} to {pid}")

        if op == "wait":
            timeout = min(float(args.get("timeout") or 10.0), 60.0)

            def _wait():
                import time

                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    if not os.path.exists(f"/proc/{pid}"):
                        return True
                    time.sleep(0.1)
                return not os.path.exists(f"/proc/{pid}")

            exited = await loop.run_in_executor(None, _wait)
            return _result(
                request,
                ok=exited,
                output=f"{pid} exited" if exited else "",
                error="" if exited else "still running after timeout",
            )

        return _result(request, ok=False, error=f"unknown operation: {op}")


# ---------------------------------------------------------------------------
# machine
# ---------------------------------------------------------------------------


class MachineCapability:
    """Read-only introspection of the host machine."""

    descriptor = CapabilityDescriptor(
        id="machine",
        description=(
            "Machine introspection: OS/kernel/arch, CPU load & cores, memory, "
            "disk usage/mounts, network interfaces & listening ports, "
            "installed toolchains, key environment facts, systemd services, "
            "GPU summary when present. Operations: overview/cpu/memory/disk/"
            "network/ports/toolchain/services/gpu/env."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "overview",
                        "cpu",
                        "memory",
                        "disk",
                        "network",
                        "ports",
                        "toolchain",
                        "services",
                        "gpu",
                        "env",
                    ],
                },
                "name": {"type": "string", "maxLength": 256},
                "unit": {
                    "type": "string",
                    "maxLength": 255,
                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.@:%\\-]{0,254}$",
                },
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    async def invoke(self, request: CapabilityRequest, **kwargs) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "overview")
        loop = asyncio.get_running_loop()

        def _out(cmd: list[str]) -> str:
            _rc, out, err = _run(cmd)
            return (out or err).strip()

        if op == "overview":

            def _ov():
                mem = {}
                try:
                    with open("/proc/meminfo") as f:
                        for line in f:
                            k, _, v = line.partition(":")
                            mem[k.strip()] = v.split()[0]
                except OSError:
                    pass
                load1, load5, load15 = os.getloadavg()
                return (
                    f"host      {platform.node()}\n"
                    f"os        {platform.system()} {platform.release()} "
                    f"({platform.machine()})\n"
                    f"distro    {platform.freedesktop_os_release().get('PRETTY_NAME', '?')}"
                    if hasattr(platform, "freedesktop_os_release")
                    else ""
                ) + (
                    f"\npython    {platform.python_version()}"
                    f"\ncpus      {os.cpu_count()} logical"
                    f"\nload      {load1:.2f} {load5:.2f} {load15:.2f}"
                    f"\nmem total {int(mem.get('MemTotal', 0)) // 1024} MB"
                    f"\nmem avail {int(mem.get('MemAvailable', 0)) // 1024} MB"
                )

            # ``os.getloadavg()`` is a tiny, local kernel read.  Keeping this
            # summary on the event-loop thread avoids a host-specific libc
            # deadlock observed when that call is dispatched through the
            # default executor during clean release-test startup.
            text = _ov()
            return _result(request, output=text)

        if op == "cpu":
            out = await loop.run_in_executor(None, lambda: _out(["lscpu"]))
            return _result(request, output=out[:3000])

        if op == "memory":

            def _mem():
                with open("/proc/meminfo") as f:
                    head = [next(f) for _ in range(8)]
                return "".join(head)

            text = await loop.run_in_executor(None, _mem)
            return _result(request, output=text)

        if op == "disk":
            out = await loop.run_in_executor(
                None,
                lambda: _out(
                    [
                        "df",
                        "-h",
                        "--output=source,fstype,size,used,avail,pcent,target",
                    ]
                ),
            )
            return _result(request, output=out)

        if op == "network":
            out = await loop.run_in_executor(None, lambda: _out(["ip", "-brief", "addr"]))
            return _result(request, output=out)

        if op == "ports":
            out = await loop.run_in_executor(None, lambda: _out(["ss", "-tlnp"]))
            return _result(request, output=out[:4000])

        if op == "toolchain":

            def _tc():
                tools = [
                    "git",
                    "python3",
                    "node",
                    "npm",
                    "cargo",
                    "go",
                    "rustc",
                    "docker",
                    "uv",
                    "pip",
                    "make",
                    "gcc",
                    "g++",
                    "java",
                ]
                found = []
                for t in tools:
                    p = shutil.which(t)
                    if p:
                        found.append(f"{t}: {p}")
                return "\n".join(found)

            text = await loop.run_in_executor(None, _tc)
            return _result(request, output=text)

        if op == "services":
            unit = str(args.get("unit") or "").strip()
            if unit and not _valid_unit_name(unit):
                return _result(request, ok=False, error="invalid systemd unit name")

            def _svc():
                if unit:
                    return _out(["systemctl", "status", unit, "--no-pager", "-l"]) or _out(
                        ["systemctl", "--user", "status", unit, "--no-pager", "-l"]
                    )
                return _out(
                    [
                        "systemctl",
                        "list-units",
                        "--type=service",
                        "--state=running",
                        "--no-pager",
                        "--no-legend",
                    ]
                )

            text = await loop.run_in_executor(None, _svc)
            return _result(request, output=text[:6000])

        if op == "gpu":

            def _gpu():
                nvidia = _out(
                    [
                        "nvidia-smi",
                        "--query-gpu=name,memory.total,utilization.gpu,memory.used",
                        "--format=csv,noheader",
                    ]
                )
                if nvidia and "not found" not in nvidia.lower():
                    return nvidia
                lspci = _out(["lspci"])
                gpu_lines = [
                    line
                    for line in lspci.splitlines()
                    if "vga" in line.lower() or "3d" in line.lower()
                ]
                return "\n".join(gpu_lines) or "(no gpu info)"

            text = await loop.run_in_executor(None, _gpu)
            return _result(request, output=text)

        if op == "env":
            name = str(args.get("name") or "")
            if name:
                val = os.environ.get(name)
                if val is None:
                    return _result(request, ok=False, error=f"env {name} unset")
                # Never leak secret-shaped values.
                lowered = name.lower()
                if any(k in lowered for k in ("key", "token", "secret", "password")):
                    return _result(request, output=f"{name}=<redacted>", meta={"redacted": True})
                return _result(request, output=f"{name}={val}")
            keys = sorted(
                k
                for k in os.environ
                if not any(s in k.lower() for s in ("key", "token", "secret", "password"))
            )
            return _result(request, output="\n".join(keys))

        return _result(request, ok=False, error=f"unknown operation: {op}")
