"""PowerShell runtime (secondary, BUILDSPEC 46 "strongly recommended").

Registers the ``powershell`` runtime name. On POSIX hosts where PowerShell
Core (pwsh) is unavailable the runtime reports availability as False so the
model never sees a runtime that doesn't exist.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
import traceback

from athena.execution.process_tree import interrupt_group, kill_tree, spawn_owned
from athena.execution.runtimes.base import BaseRuntime
from athena.execution.runtimes.shell import (
    detect_end_of_execution,
    strip_active_line,
    _parse_marker_exit_code,
)
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
)

__all__ = ["PowerShellRuntime"]


class PowerShellRuntime(BaseRuntime):
    """Minimal persistent PowerShell runtime using a marker protocol."""

    name = "powershell"
    aliases: tuple[str, ...] = ("pwsh", "ps1", "cmd")
    start_cmd: list[str] | None = None

    @staticmethod
    def available() -> bool:
        return shutil.which("pwsh") is not None or shutil.which("powershell") is not None

    def _make_session(self, *, env=None, cwd=None, sandbox_root=None,
                      network_policy=None) -> "_PSSession":
        cmd = "pwsh" if shutil.which("pwsh") else "powershell"
        sess = _PSSession(
            env=env, cwd=cwd, start_cmd=[cmd, "-NoProfile", "-NoLogo"],
            sandbox_root=sandbox_root, network_policy=network_policy)
        sess.start()
        return sess

    def _run(self, session, request, execution_id):
        yield from session.run(request.source, request.timeout, execution_id)

    def _interrupt_session(self, session) -> None:
        session.interrupt()

    def _close_session(self, session) -> None:
        session.close()


class _PSSession:
    """One long-lived PowerShell process speaking the marker protocol."""

    def __init__(self, env=None, cwd=None, start_cmd=None, sandbox_root=None,
                 network_policy=None):
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

    def start(self) -> None:
        self.process = spawn_owned(
            list(self.start_cmd) if self.start_cmd else ["powershell", "-NoProfile", "-NoLogo"],
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

    def _handle_stream(self, stream, is_error):
        try:
            for line in iter(stream.readline, ""):
                if detect_end_of_execution(line):
                    marker, _, rest = line.partition("##end_of_execution##")
                    if marker:
                        self.output_queue.put({"err": is_error, "text": strip_active_line(marker)})
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

    def run(self, code, timeout, execution_id):
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
            # PowerShell marker protocol: wrap code in a script block and emit sentinel
            wrapped = (
                "{\n"
                f"{code}\n"
                "} ; $athena_rc = $LASTEXITCODE\n"
                "Write-Output \"##end_of_execution##$athena_rc\"\n"
            )
            if process.stdin is None:
                raise BrokenPipeError("PowerShell stdin is unavailable")
            process.stdin.write(wrapped + "\n")
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
