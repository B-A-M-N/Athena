"""Persistent shell runtime.

Adapted from Classic Open Interpreter's ``subprocess_language.py`` +
``shell.py``: a long-lived shell process reads code from stdin, streams
stdout/stderr, detects the end-of-execution marker, and supports interrupt.
State persists across executions within a runtime session (BHV-058). Process
groups ensure cancellation kills the owned tree (BHV-362).
"""

from __future__ import annotations

import platform
import queue
import re
import secrets
import subprocess
import threading
import time
import traceback

from athena.execution.process_tree import (
    interrupt_group,
    kill_tree,
    spawn_owned,
)
from athena.execution.runtimes.base import BaseRuntime
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
)


class ShellRuntime(BaseRuntime):
    """Persistent shell runtime sharing a long-lived subprocess per session."""

    name = "shell"
    aliases: tuple[str, ...] = ("bash", "sh", "zsh")
    start_cmd: list[str] | None = None

    def _make_session(
        self, *, env=None, cwd=None, sandbox_root=None, network_policy=None
    ) -> "_SubprocessSession":
        sess = _SubprocessSession(
            env=env,
            cwd=cwd,
            start_cmd=self.start_cmd,
            sandbox_root=sandbox_root,
            network_policy=network_policy,
        )
        sess.start()
        return sess

    def _run(self, session, request, execution_id):
        yield from session.run(request.source, request.timeout, execution_id)

    def _interrupt_session(self, session) -> None:
        session.interrupt()

    def _close_session(self, session) -> None:
        session.close()


class _SubprocessSession:
    """One long-lived shell process speaking the OI marker protocol."""

    def __init__(self, env=None, cwd=None, start_cmd=None, sandbox_root=None, network_policy=None):
        self.env = env or {}
        self.cwd = cwd
        self.start_cmd = start_cmd
        self.sandbox_root = sandbox_root
        self.network_policy = network_policy
        self.process: subprocess.Popen | None = None
        self.output_queue: queue.Queue = queue.Queue()
        self.done = threading.Event()
        self.exit_code: int | None = 0
        self._exit_lock = threading.Lock()
        self._marker: str | None = None

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        if self.start_cmd:
            start_cmd = list(self.start_cmd)
        elif platform.system() == "Windows":
            start_cmd = ["cmd.exe"]
        else:
            start_cmd = ["bash", "--norc", "--noprofile"]
        self.process = spawn_owned(
            start_cmd,
            env=self.env,
            cwd=self.cwd,
            sandbox_root=self.sandbox_root,
            network_policy=self.network_policy,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0,
            encoding="utf-8",
            errors="replace",
        )
        threading.Thread(
            target=self._handle_stream, args=(self.process.stdout, False), daemon=True
        ).start()
        threading.Thread(
            target=self._handle_stream, args=(self.process.stderr, True), daemon=True
        ).start()

    def close(self) -> None:
        # A cancelled execution may be blocked in run() waiting for output.
        # Wake that queue loop before killing the process so its bridge thread
        # can return and the event loop's executor can shut down cleanly.
        self.done.set()
        if self.process:
            kill_tree(self.process)
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
                if self.process.stdout is not None:
                    self.process.stdout.close()
            except Exception:
                pass
            self.process = None

    def interrupt(self) -> None:
        if self.process and self.process.poll() is None:
            interrupt_group(self.process)

    # -- streaming -- ---------------------------------------------------- #
    def _handle_stream(self, stream, is_error):
        try:
            for line in iter(stream.readline, ""):
                marker_token = self._marker
                if marker_token and detect_end_of_execution(line, marker_token):
                    output, _, rest = line.partition(marker_token)
                    if output:
                        self.output_queue.put({"err": is_error, "text": strip_active_line(output)})
                    code = _parse_marker_exit_code(rest)
                    if code is not None:
                        with self._exit_lock:
                            self.exit_code = code
                    self.done.set()
                else:
                    stripped = strip_active_line(line)
                    if stripped:
                        self.output_queue.put({"err": is_error, "text": stripped})
        except ValueError:
            pass

    # -- execution -- ---------------------------------------------------- #
    def run(self, code, timeout, execution_id):
        self._marker = f"##athena_end_{secrets.token_hex(16)}##"
        code_processed = preprocess_shell(code, marker=self._marker)
        process = self.process
        if process is None or process.poll() is not None:
            self.close()
            self.start()
            process = self.process
        if process is None:
            yield ExecutionEvent(
                type=ExecutionEventType.EXITED,
                execution_id=execution_id,
                exit_status=ExecutionExitStatus.FAILED,
                exit_code=1,
            )
            return

        self.done.clear()
        with self._exit_lock:
            self.exit_code = 0
        try:
            if process.stdin is None:
                raise BrokenPipeError("shell stdin is unavailable")
            process.stdin.write(code_processed + "\n")
            process.stdin.flush()
        except Exception:
            yield ExecutionEvent(
                type=ExecutionEventType.STDERR,
                execution_id=execution_id,
                data=traceback.format_exc(),
            )
            self.close()
            self.start()
            yield ExecutionEvent(
                type=ExecutionEventType.EXITED,
                execution_id=execution_id,
                exit_status=ExecutionExitStatus.FAILED,
                exit_code=1,
            )
            return

        deadline = time.monotonic() + timeout.total_seconds() if timeout else None

        while True:
            try:
                out = self.output_queue.get(timeout=0.1)
            except queue.Empty:
                if self.done.is_set():
                    break
                if process.poll() is not None:
                    rc = process.returncode
                    with self._exit_lock:
                        self.exit_code = rc if rc is not None else 0
                    break
                if deadline and time.monotonic() > deadline:
                    yield ExecutionEvent(
                        type=ExecutionEventType.EXITED,
                        execution_id=execution_id,
                        exit_status=ExecutionExitStatus.TIMED_OUT,
                        exit_code=None,
                    )
                    kill_tree(process)
                    return
                continue
            yield ExecutionEvent(
                type=ExecutionEventType.STDOUT if not out["err"] else ExecutionEventType.STDERR,
                execution_id=execution_id,
                data=out["text"],
            )

        for _ in range(3):
            try:
                out = self.output_queue.get(timeout=0.2)
            except queue.Empty:
                break
            yield ExecutionEvent(
                type=ExecutionEventType.STDOUT if not out["err"] else ExecutionEventType.STDERR,
                execution_id=execution_id,
                data=out["text"],
            )
        with self._exit_lock:
            exit_code = self.exit_code
        if exit_code != 0:
            yield ExecutionEvent(
                type=ExecutionEventType.EXITED,
                execution_id=execution_id,
                exit_status=ExecutionExitStatus.FAILED,
                exit_code=exit_code,
            )
            return
        yield ExecutionEvent(
            type=ExecutionEventType.EXITED,
            execution_id=execution_id,
            exit_status=ExecutionExitStatus.EXITED,
            exit_code=0,
        )


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
def preprocess_shell(code: str, *, marker: str | None = None) -> str:
    """Run a script as one complete construct without per-line injection.

    The whole script is wrapped in a command group (NOT a subshell) so state
    (variables, cd, functions) persists across executions. A single
    end-of-execution sentinel carrying the real exit status (``$?``) is
    printed afterwards so failures aren't masked as success.

    If the user code calls ``exit``, the shell dies and the sentinel isn't
    printed — ``_SubprocessSession.run()`` detects the process death and
    reports the exit code directly; the next execution restarts the shell.
    """
    marker = marker or "##end_of_execution##"
    wrapped = f"{{\n{code}\n}} ; athena_rc=$?\nprintf '%s%%s\\n' \"$athena_rc\"" % marker
    return wrapped


def _parse_marker_exit_code(rest: str) -> int | None:
    m = re.fullmatch(r"\s*(-?\d+)\s*", rest)
    if m is None:
        return None
    return int(m.group(0))


def detect_end_of_execution(line: str, marker: str | None = None) -> bool:
    return (marker or "##end_of_execution##") in line


def strip_active_line(line: str) -> str:
    return re.sub(r"##active_line_\d+##", "", line)


def detect_error_handler(line: str, marker: str | None = None) -> bool:
    return detect_end_of_execution(line, marker)


def has_multiline_commands(script_text: str) -> bool:
    patterns = [
        r"\\$",
        r"\|$",
        r"&&\s*$",
        r"\|\|\s*$",
        r"<\($",
        r"\($",
        r"{\s*$",
        r"\bif\b",
        r"\bwhile\b",
        r"\bfor\b",
        r"do\s*$",
        r"then\s*$",
    ]
    for line in script_text.splitlines():
        if any(re.search(p, line.rstrip()) for p in patterns):
            return True
    return False


__all__ = ["ShellRuntime"]
