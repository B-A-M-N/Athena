"""`terminal_session` — an interactive PTY with SCREEN awareness (P0).

Beyond run-and-capture execution: Athena can drive TUIs, REPLs, installers,
SSH sessions, and debuggers. Sessions are persistent (survive across calls),
owned by the creating task, and renderable as a text screen.

Operations:
    create   spawn a command under a PTY  -> session_id
    screen   return the current visible screen (rows x cols)
    write    send raw text (no newline)
    send     send text + newline
    keys     send control keys (e.g. "C-c", "ESC", "Enter")
    wait_for block until a pattern appears on screen (timeout-bounded)
    resize   change pty dimensions
    list     list live sessions owned by this task
    kill     terminate a session
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)

try:
    import pexpect
except ImportError:  # pragma: no cover
    pexpect = None  # type: ignore[assignment]

_MAX_SESSIONS_PER_TASK = 8
_DEFAULT_WAIT_TIMEOUT = 15.0
_MAX_WAIT_TIMEOUT = 60.0


def _result(request, ok=True, output="", error="", meta=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=None if ok else error,
        metadata=dict(meta or {}),
    )


class _Session:
    """PTY session with its own accumulating output buffer.

    Patterned on Panopticon's PTY adapter: pexpect's ``before`` is consumed
    by expect() calls, so the session keeps a persistent sanitized buffer of
    everything received. ``screen`` returns this buffer, not ``child.before``.
    """

    _ANSI_RE = re.compile(
        r"\x1b\[[?0-9;]*[A-Za-z]"      # CSI sequences
        r"|\x1b\][^\x07]*\x07"          # OSC (title) sequences
        r"|[\x00-\x08\x0b\x0c\x0e-\x1f]"  # other control chars
    )
    _BUF_CAP = 200_000  # chars

    def __init__(self, session_id: str, task_id: str | None, cmd: str,
                 rows: int, cols: int, env=None, cwd=None):
        assert pexpect is not None, "pexpect required"
        self.id = session_id
        self.task_id = task_id
        child = pexpect.spawn(
            cmd, encoding="utf-8", codec_errors="replace",
            dimensions=(rows, cols), env=env, cwd=cwd, timeout=None,
        )
        self.child = child
        self.rows = rows
        self.cols = cols
        self.buffer = ""

    def drain(self, quiet_seconds: float = 0.3) -> str:
        """Read all pending output into the buffer until quiet."""
        deadline = time.time() + quiet_seconds
        while time.time() < deadline:
            try:
                chunk = self.child.read_nonblocking(size=65536, timeout=0.2)
                if chunk:
                    self.buffer += chunk
                    if len(self.buffer) > self._BUF_CAP:
                        self.buffer = self.buffer[-self._BUF_CAP:]
                    deadline = time.time() + quiet_seconds
            except Exception:
                break
        return self.screen()

    def screen(self) -> str:
        return self._ANSI_RE.sub("", self.buffer)

    def alive(self) -> bool:
        return self.child.isalive()


class TerminalSessionCapability:
    """Interactive PTY sessions with screen awareness."""

    descriptor = CapabilityDescriptor(
        id="terminal_session",
        description=(
            "Operate interactive terminals under a PTY: TUIs, REPLs, shells, "
            "installers, SSH. Create persistent sessions, read the live "
            "screen, write/send text/keys, wait for output patterns, resize, "
            "and terminate. Operations: create/screen/write/send/keys/"
            "wait_for/list/resize/kill."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "create", "screen", "write", "send", "keys",
                    "wait_for", "list", "resize", "kill"]},
                "session": {"type": "string"},
                "command": {"type": "string"},
                "text": {"type": "string"},
                "keys": {"type": "string"},
                "pattern": {"type": "string"},
                "timeout": {"type": "number"},
                "rows": {"type": "integer"},
                "cols": {"type": "integer"},
                "cwd": {"type": "string"},
            },
        },
        effects=frozenset({
            EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS,
            EffectClass.WRITE_LOCAL, EffectClass.READ_LOCAL,
        }),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    # -- helpers ---------------------------------------------------------
    def _own(self, request: CapabilityRequest) -> _Session | None:
        sess = self._sessions.get(str((request.arguments or {}).get("session") or ""))
        if sess is None or sess.task_id != request.task_id:
            return None
        return sess

    def _escape_keys(self, keys: str):
        """Map friendly names to pexpect escapes; pass through unknown."""
        named = {
            "enter": "\n", "esc": "\x1b", "tab": "\t",
            "C-c": "\x03", "C-d": "\x04", "C-z": "\x1a",
            "C-l": "\x0c", "backspace": "\x7f",
        }
        low = keys.strip()
        if low in named:
            return named[low]
        return keys

    async def invoke(self, request: CapabilityRequest, **kwargs) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        call_id = request.call_id
        if pexpect is None:
            return CapabilityResult(
                call_id, request.capability_id, CapabilityResultStatus.FAILED,
                error="pexpect not installed",
            )
        loop = asyncio.get_running_loop()

        if op == "create":
            cmd = str(args.get("command") or "").strip()
            if not cmd:
                return _result(request, ok=False, error="command required")
            owned = [s for s in self._sessions.values() if s.task_id == request.task_id]
            if len(owned) >= _MAX_SESSIONS_PER_TASK:
                return _result(request, ok=False,
                               error=f"session limit reached ({_MAX_SESSIONS_PER_TASK})")
            sid = f"tty_{len(self._sessions) + 1}_{os.getpid()}"
            rows = int(args.get("rows") or 24)
            cols = int(args.get("cols") or 80)
            cwd = args.get("cwd")
            sess = await loop.run_in_executor(
                None, lambda: _Session(sid, request.task_id, cmd, rows, cols, cwd=cwd))
            self._sessions[sid] = sess
            return _result(request, output=f"created {sid} ({cmd})",
                           meta={"session": sid})

        sess = self._own(request)
        if sess is None:
            return _result(request, ok=False, error="unknown or unowned session")

        if op == "screen":
            data = await loop.run_in_executor(None, sess.drain)
            return _result(request, output=data[-4000:], meta={
                "session": sess.id, "alive": sess.alive()})

        if op == "write":
            await loop.run_in_executor(None, sess.child.write, str(args.get("text") or ""))
            return _result(request, output="ok", meta={"session": sess.id})

        if op == "send":
            await loop.run_in_executor(
                None, sess.child.sendline, str(args.get("text") or ""))

            def _drain_send():
                time.sleep(0.15)
                return sess.drain()

            data = await loop.run_in_executor(None, _drain_send)
            return _result(request, output=data[-4000:], meta={"session": sess.id})

        if op == "keys":
            raw = self._escape_keys(str(args.get("keys") or ""))
            await loop.run_in_executor(None, sess.child.send, raw)

            def _drain_keys():
                time.sleep(0.15)
                return sess.drain()

            data = await loop.run_in_executor(None, _drain_keys)
            return _result(request, output=data[-4000:], meta={"session": sess.id})

        if op == "wait_for":
            pattern = str(args.get("pattern") or "")
            if not pattern:
                return _result(request, ok=False, error="pattern required")
            timeout = min(float(args.get("timeout") or _DEFAULT_WAIT_TIMEOUT),
                          _MAX_WAIT_TIMEOUT)

            def _wait():
                """Poll the session's own buffer for the pattern.

                (Not child.expect(): expect() consumes output into its
                internal buffer, racing the persistent session buffer.)
                """
                import time as _time

                deadline = _time.monotonic() + timeout
                while _time.monotonic() < deadline:
                    sess.drain(quiet_seconds=0.1)
                    if pattern in sess.screen():
                        return True
                    if not sess.alive():
                        return False
                    _time.sleep(0.05)
                return False

            matched = await loop.run_in_executor(None, _wait)
            data = sess.screen()
            if matched:
                return _result(request, output=data[-4000:], meta={
                    "session": sess.id, "matched": True})
            reason = "eof" if not sess.alive() else "timeout"
            return _result(request, ok=False, output=data[-4000:],
                           error=f"wait_for: {reason}",
                           meta={"session": sess.id, "matched": False})

        if op == "resize":
            rows = max(int(args.get("rows") or 24), 2)
            cols = max(int(args.get("cols") or 80), 10)
            await loop.run_in_executor(
                None, sess.child.setwinsize, rows, cols)
            sess.rows, sess.cols = rows, cols
            return _result(request, output="ok", meta={"session": sess.id})

        if op == "kill":
            await loop.run_in_executor(None, sess.child.terminate, True)
            self._sessions.pop(sess.id, None)
            return _result(request, output=f"terminated {sess.id}")

        return _result(request, ok=False, error=f"unknown operation: {op}")

    def close_all(self) -> None:
        for s in self._sessions.values():
            try:
                s.child.terminate(force=True)
            except Exception:
                pass
        self._sessions.clear()
