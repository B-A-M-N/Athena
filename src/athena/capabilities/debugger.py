"""`debugger` — first-class debugging via debugpy's DAP (P0).

Python debugging: spawn a debugged process (or attach to a running
debugpy-enabled one), set breakpoints, continue/pause, read stack frames and
variables, evaluate expressions in a frame, step over/into/out.

Requires the `debugpy` extra (`pip install athena-agent[debug]`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from athena.protocol.capabilities import (
    Availability,
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)

try:
    import debugpy  # noqa: T100 - optional debugger dependency
except ImportError:
    debugpy = None  # type: ignore[assignment]

# Importability is not usability.  The current implementation has no DAP
# client and its launch helper would otherwise create a raw host subprocess
# outside ExecutionManager.  Keep the descriptor out of the model surface
# until launch/attach/stepping can use a governed runtime session.
_DEBUGGER_AVAILABILITY = Availability.UNAVAILABLE
_logger = logging.getLogger("athena.debugger")


def _result(request, ok=True, output="", error="", meta=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=None if ok else error,
        metadata=dict(meta or {}),
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class DebuggerCapability:
    """Debug Python programs through debugpy (DAP)."""

    descriptor = CapabilityDescriptor(
        id="debugger",
        description=(
            "Launch Python scripts under debugpy, record breakpoints for a "
            "future DAP client, inspect status, and detach. Full interactive "
            "DAP attach/stepping/stack inspection is not exposed until a DAP "
            "client is available. Operations: launch/breakpoint/detach/status."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "launch", "breakpoint", "detach", "status"]},
                "session": {"type": "string"},
                "script": {"type": "string"},
                "pid": {"type": "integer"},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "condition": {"type": "string"},
                "expression": {"type": "string"},
                "frame_id": {"type": "integer"},
                "granularity": {"type": "string", "enum": [
                    "over", "into", "out"]},
            },
        },
        effects=frozenset({
            EffectClass.EXECUTE,
            EffectClass.SPAWN_PROCESS,
            EffectClass.READ_LOCAL,
            EffectClass.WRITE_LOCAL,
        }),
        origin=CapabilityOrigin.NATIVE,
        availability=_DEBUGGER_AVAILABILITY,
    )

    def __init__(self) -> None:
        if debugpy is None:
            raise ImportError(
                "debugger capability requires debugpy; "
                "install athena-agent[debug]")
        self._sessions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def available() -> bool:
        return _DEBUGGER_AVAILABILITY is Availability.AVAILABLE

    def _own(self, request) -> dict | None:
        sid = str((request.arguments or {}).get("session") or "")
        sess = self._sessions.get(sid)
        if sess is None or sess["task_id"] != request.task_id:
            return None
        return sess

    async def invoke(self, request: CapabilityRequest, **kwargs) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        if not self.available():
            return _result(
                request, ok=False,
                error=("debugger unavailable: DAP client and governed "
                       "ExecutionManager backend are not implemented"),
            )
        if debugpy is None:
            return _result(request, ok=False,
                           error="debugpy not installed; install athena-agent[debug]")
        loop = asyncio.get_running_loop()

        if op == "launch":
            script = str(args.get("script") or "").strip()
            if not script or not os.path.isfile(script):
                return _result(request, ok=False, error="script path required")
            port = _free_port()
            sid = f"dbg_{len(self._sessions) + 1}_{os.getpid()}"

            def _spawn():
                proc = subprocess_popen_debugpy(port, script)
                return proc

            proc = await loop.run_in_executor(None, _spawn)
            self._sessions[sid] = {
                "task_id": request.task_id, "port": port,
                "proc": proc, "paused": False, "breakpoints": {},
                "session_id": sid,
            }
            # debugpy holds the process until a client attaches when spawned
            # with --wait-for-client; breakpoints recorded now apply on
            # attach. Give the listener a moment to come up.
            await asyncio.sleep(1.0)
            return _result(
                request,
                output=f"launched {script} under debugger; "
                       f"session={sid} port={port} pid={proc.pid}",
                meta={"session": sid, "port": port})

        sess = self._own(request)
        if sess is None:
            return _result(request, ok=False, error="unknown or unowned session")

        if op == "status":
            alive = sess["proc"].poll() is None if sess.get("proc") else True
            return _result(request, output=(
                f"session={sess.get('session_id', '')}"
                f" alive={alive} paused={sess['paused']} "
                f"breakpoints={list(sess['breakpoints'])}"
            ))

        if op == "breakpoint":
            file_ = str(args.get("file") or "")
            line = int(args.get("line") or 0)
            if not file_ or line <= 0:
                return _result(request, ok=False,
                               error="file and line required")
            cond = args.get("condition")
            bps = sess.setdefault("breakpoints", {})
            bps.setdefault(file_, []).append((line, cond))
            # Breakpoints apply when the debug client is attached; record for
            # now — full DAP client wiring lands with the attach flow.
            return _result(request,
                           output=f"recorded breakpoint {file_}:{line}")

        if op == "detach":
            sid = str(args.get("session"))
            proc = sess.get("proc")
            if proc is not None and proc.poll() is None:
                proc.terminate()
            self._sessions.pop(str(args.get("session")), None)
            return _result(request, output=f"detached {args.get('session')}")

        # The remaining ops require a live DAP client session.
        return _result(
            request, ok=False,
            error=f"{op} requires an active DAP client; launch/attach first "
                  f"(DAP stepping wired via debugpy client in [debug] extra)")

    def close_all(self) -> None:
        for sess in self._sessions.values():
            try:
                proc = sess.get("proc")
                if proc is not None and proc.poll() is None:
                    proc.terminate()
            except OSError as exc:
                _logger.debug("debugger session cleanup failed: %s", exc)
        self._sessions.clear()


def subprocess_popen_debugpy(port: int, script: str):
    import subprocess as sp

    return sp.Popen([
        "python3", "-m", "debugpy", "--listen", f"127.0.0.1:{port}",
        "--wait-for-client", script,
    ])
