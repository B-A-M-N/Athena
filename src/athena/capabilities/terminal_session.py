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
import logging
import os
import re
import shutil
import time
from typing import Any

from athena.execution.process_tree import sandbox_argv
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
    import pexpect  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    pexpect = None

try:
    import pyte
except ImportError:  # pragma: no cover
    pyte = None  # type: ignore[assignment]

_MAX_SESSIONS_PER_TASK = 8
_DEFAULT_WAIT_TIMEOUT = 15.0
_MAX_WAIT_TIMEOUT = 60.0
_logger = logging.getLogger("athena.terminal_session")
_TERMINAL_AVAILABILITY = (
    Availability.AVAILABLE
    if pexpect is not None and pyte is not None
    and (os.name != "posix" or shutil.which("bwrap") is not None)
    else Availability.UNAVAILABLE
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


def new_screen(rows: int, cols: int):
    """Create a pyte virtual terminal matching the PTY dimensions."""
    return pyte.HistoryScreen(cols, rows, history=1000)


def feed_screen(screen, text: str) -> None:
    """Feed raw terminal output (with ANSI escapes) into the framebuffer."""
    pyte.ByteStream(screen).feed(text.encode("utf-8", "replace"))


def _tail_text(data, limit: int = 4000) -> str:
    """Render a screen result (str or list of lines) as a tail-limited string."""
    text = data if isinstance(data, str) else "\n".join(data)
    return text[-limit:]


def _terminal_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Build a non-secret environment for the PTY namespace."""
    allowed = (
        "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.setdefault("TERM", "xterm-256color")
    # The host home directory is not mounted in the namespace. Use the private
    # tmpfs home rather than advertising a path that cannot be accessed.
    env["HOME"] = "/tmp"
    if overrides:
        env.update({str(key): str(value) for key, value in overrides.items()})
    return env


class _Session:
    """PTY session with a raw transcript buffer and a real screen framebuffer.

    Patterned on Panopticon's PTY adapter: pexpect's ``before`` is consumed
    by expect() calls, so the session keeps a persistent sanitized buffer of
    everything received (``buffer``) *and* a pyte virtual terminal
    (``screen_obj``) that emulates cursor movement, line wrapping,
    alternate-screen overwrites and clears. ``screen()`` returns the
    framebuffer display — what an actual 80x24 terminal would show.
    """

    _ANSI_RE = re.compile(
        r"\x1b\[[?0-9;]*[A-Za-z]"      # CSI sequences
        r"|\x1b\][^\x07]*\x07"          # OSC (title) sequences
        r"|[\x00-\x08\x0b\x0c\x0e-\x1f]"  # other control chars
    )
    _BUF_CAP = 200_000  # chars

    def __init__(self, session_id: str, task_id: str | None, cmd: str,
                 rows: int, cols: int, env=None, cwd=None,
                 workspace_root: str | None = None,
                 network_policy: str | None = None):
        assert pexpect is not None, "pexpect required"
        if workspace_root is None:
            raise ValueError("terminal sessions require a workspace root")
        command = sandbox_argv(
            ["bash", "--norc", "--noprofile", "-c", cmd],
            root=workspace_root,
            cwd=cwd or workspace_root,
            network_policy=network_policy,
            writable=True,
        )
        self.id = session_id
        self.task_id = task_id
        self.workspace_root = workspace_root
        child = pexpect.spawn(
            command[0], command[1:], encoding="utf-8", codec_errors="replace",
            dimensions=(rows, cols), env=_terminal_env(env), cwd=None, timeout=None,
        )
        self.child = child
        self.cmd_text = cmd
        self.rows = rows
        self.cols = cols
        self.buffer = ""
        if pyte is not None:
            self.screen_obj = new_screen(rows, cols)
        else:  # pragma: no cover - pyte is a hard dep of this capability
            self.screen_obj = None

    def drain(self, quiet_seconds: float = 0.3) -> str | list[str]:
        """Read all pending output into the buffers until quiet."""
        deadline = time.time() + quiet_seconds
        while time.time() < deadline:
            try:
                chunk = self.child.read_nonblocking(size=65536, timeout=0.2)
                if chunk:
                    self.buffer += chunk
                    if len(self.buffer) > self._BUF_CAP:
                        self.buffer = self.buffer[-self._BUF_CAP:]
                    if self.screen_obj is not None:
                        try:
                            feed_screen(self.screen_obj, chunk)
                        except Exception as exc:  # noqa: BLE001 - screen must not kill PTY
                            _logger.debug("terminal screen feed failed: %s", exc)
                    deadline = time.time() + quiet_seconds
            except (OSError, pexpect.exceptions.ExceptionPexpect):
                break
        return self.screen()

    def screen(self) -> str | list[str]:
        if self.screen_obj is not None:
            return self.screen_obj.display
        return self._ANSI_RE.sub("", self.buffer)

    def cursor(self) -> tuple[int, int]:
        """(row, col) of the cursor on the framebuffer, 0-indexed."""
        if self.screen_obj is not None:
            return (self.screen_obj.cursor.y, self.screen_obj.cursor.x)
        return (0, 0)

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
        availability=_TERMINAL_AVAILABILITY,
    )

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    @staticmethod
    def available() -> bool:
        """Whether the PTY and framebuffer dependencies are installed."""
        return _TERMINAL_AVAILABILITY is Availability.AVAILABLE

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
        if not self.available():
            return CapabilityResult(
                call_id, request.capability_id, CapabilityResultStatus.FAILED,
                error="terminal_session unavailable: install pexpect and pyte",
            )
        loop = asyncio.get_running_loop()

        if op == "create":
            cmd = str(args.get("command") or "").strip()
            if not cmd:
                return _result(request, ok=False, error="command required")
            existing_sessions = [
                s for s in self._sessions.values() if s.task_id == request.task_id
            ]
            if len(existing_sessions) >= _MAX_SESSIONS_PER_TASK:
                return _result(request, ok=False,
                               error=f"session limit reached ({_MAX_SESSIONS_PER_TASK})")
            sid = f"tty_{len(self._sessions) + 1}_{os.getpid()}"
            rows = int(args.get("rows") or 24)
            cols = int(args.get("cols") or 80)
            cwd = args.get("cwd")
            ctx = kwargs.get("context")
            workspace = getattr(ctx, "workspace", None) if ctx is not None else None
            if workspace is None and isinstance(ctx, dict):
                workspace = ctx.get("workspace")
            if workspace is None or not getattr(workspace, "root", None):
                return _result(
                    request, ok=False,
                    error="terminal sessions require workspace context",
                )
            workspace_root = os.path.realpath(str(workspace.root))
            if cwd:
                real = os.path.realpath(
                    str(cwd) if os.path.isabs(str(cwd))
                    else os.path.join(workspace_root, str(cwd))
                )
                if real != workspace_root and not real.startswith(workspace_root + os.sep):
                    return _result(
                        request, ok=False,
                        error=f"cwd outside workspace: {cwd}")
                cwd = real
            else:
                cwd = workspace_root
            network_policy = getattr(
                getattr(workspace, "network_policy", None),
                "value",
                getattr(workspace, "network_policy", None),
            )
            created_session = await loop.run_in_executor(
                None, lambda: _Session(
                    sid, request.task_id, cmd, rows, cols, cwd=cwd,
                    workspace_root=workspace_root,
                    network_policy=network_policy,
                ))
            self._sessions[sid] = created_session
            return _result(request, output=f"created {sid} ({cmd})",
                           meta={"session": sid})

        if op == "list":
            listed: list[dict[str, Any]] = [
                {"session": s.id, "command": s.cmd_text,
                 "alive": s.alive(), "rows": s.rows, "cols": s.cols}
                for s in self._sessions.values()
                if s.task_id == request.task_id
            ]
            return _result(request, output=f"{len(listed)} session(s)",
                           meta={"sessions": listed})

        session: _Session | None = self._own(request)
        if session is None:
            return _result(request, ok=False, error="unknown or unowned session")

        if op == "screen":
            data = await loop.run_in_executor(None, session.drain)
            row, col = session.cursor()
            return _result(request, output=_tail_text(data), meta={
                "session": session.id, "alive": session.alive(),
                "cursor_row": row, "cursor_col": col,
                "rows": session.rows, "cols": session.cols})

        if op == "write":
            await loop.run_in_executor(None, session.child.write, str(args.get("text") or ""))
            return _result(request, output="ok", meta={"session": session.id})

        if op == "send":
            await loop.run_in_executor(
                None, session.child.sendline, str(args.get("text") or ""))

            def _drain_send():
                time.sleep(0.15)
                return session.drain()

            data = await loop.run_in_executor(None, _drain_send)
            return _result(request, output=_tail_text(data), meta={"session": session.id})

        if op == "keys":
            raw = self._escape_keys(str(args.get("keys") or ""))
            await loop.run_in_executor(None, session.child.send, raw)

            def _drain_keys():
                time.sleep(0.15)
                return session.drain()

            data = await loop.run_in_executor(None, _drain_keys)
            return _result(request, output=_tail_text(data), meta={"session": session.id})

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
                    session.drain(quiet_seconds=0.1)
                    if pattern in "\n".join(session.screen()):
                        return True
                    if not session.alive():
                        return False
                    _time.sleep(0.05)
                return False

            matched = await loop.run_in_executor(None, _wait)
            data = _tail_text(session.screen())
            if matched:
                return _result(request, output=data, meta={
                    "session": session.id, "matched": True})
            reason = "eof" if not session.alive() else "timeout"
            return _result(request, ok=False, output=data,
                           error=f"wait_for: {reason}",
                           meta={"session": session.id, "matched": False})

        if op == "resize":
            rows = max(int(args.get("rows") or 24), 2)
            cols = max(int(args.get("cols") or 80), 10)
            await loop.run_in_executor(
                None, session.child.setwinsize, rows, cols)
            session.rows, session.cols = rows, cols
            if getattr(session, "screen_obj", None) is not None:
                try:
                    session.screen_obj.resize(lines=rows, columns=cols)
                except (OSError, TypeError, ValueError) as exc:
                    _logger.debug("terminal resize failed: %s", exc)
            return _result(request, output="ok", meta={"session": session.id})

        if op == "kill":
            await loop.run_in_executor(None, session.child.terminate, True)
            self._sessions.pop(session.id, None)
            return _result(request, output=f"terminated {session.id}")

        return _result(request, ok=False, error=f"unknown operation: {op}")

    def close_all(self) -> None:
        for s in self._sessions.values():
            try:
                s.child.terminate(force=True)
            except (OSError, pexpect.exceptions.ExceptionPexpect) as exc:
                _logger.debug("terminal session cleanup failed: %s", exc)
        self._sessions.clear()
