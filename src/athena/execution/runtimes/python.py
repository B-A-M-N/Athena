"""Persistent Python runtime.

Generated Python MUST NOT execute using ``exec()`` inside the Athena
orchestration process (BUILDSPEC section 48). This runtime keeps a dedicated
worker subprocess that evaluates code and preserves module-level state across
executions (Scenario B: ``x = 40`` then ``print(x + 2)`` returns ``42``).

The worker communicates over a pipe protocol: a length-prefixed JSON payload
carries the source; output is streamed back as JSON frames; Crash isolation,
interruptibility, and state reset come from process boundaries.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

from athena.execution.process_tree import interrupt_group, kill_tree, spawn_owned
from athena.execution.runtimes.base import BaseRuntime
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
)

_WORKER_SOURCE = r"""
import json, sys, traceback
state = {}
class _Emitter:
    def __init__(self, err=False):
        self.err = err
    def write(self, text):
        if text:
            sys.__stdout__.write(json.dumps({"type":"err" if self.err else "out","data":text})+"\n")
            sys.__stdout__.flush()
    def flush(self):
        pass
while True:
    try:
        length_line = sys.stdin.readline()
        if not length_line:
            break
        length = int(length_line.strip())
        payload = sys.stdin.read(length)
        msg = json.loads(payload)
    except Exception:
        sys.stdout.write(json.dumps({"type":"fatal","data":"bad stdin"})+"\n")
        sys.stdout.flush()
        break

    code = msg.get("source", "")
    out = _Emitter()
    err = _Emitter(err=True)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    success = True
    try:
        ns = dict(state)
        exec(code, ns)
        state.update({k: v for k, v in ns.items() if not k.startswith("__")})
        sys.stdout, sys.stderr = old_stdout, old_stderr
    except SystemExit as e:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        sys.stdout.write(json.dumps({"type":"err","data":str(e)})+"\n")
        sys.stdout.flush()
        success = False
    except BaseException:
        tb = traceback.format_exc()
        sys.stdout, sys.stderr = old_stdout, old_stderr
        sys.stdout.write(json.dumps({"type":"err","data":tb})+"\n")
        sys.stdout.flush()
        success = False
    # Emit done frame with explicit success/failure
    sys.stdout.write(json.dumps({"type":"done","ok":success})+"\n")
    sys.stdout.flush()
"""


class PythonRuntime(BaseRuntime):
    """Persistent Python runtime backed by a dedicated worker process."""

    name = "python"
    aliases = ("py", "python3")

    def _make_session(
        self,
        *,
        env=None,
        cwd=None,
        sandbox_root=None,
        network_policy=None,
        writable_paths=None,
        read_only_paths=(),
        toolchain_paths=(),
        writable_toolchain_paths=(),
    ) -> "_PythonSession":
        sess = _PythonSession(
            env=env,
            cwd=cwd,
            sandbox_root=sandbox_root,
            network_policy=network_policy,
            writable_paths=writable_paths,
            read_only_paths=read_only_paths,
            toolchain_paths=toolchain_paths,
            writable_toolchain_paths=writable_toolchain_paths,
        )
        sess.start()
        return sess

    def _run(self, session, request, execution_id):
        yield from session.run(request.source, request.timeout, execution_id)

    def _interrupt_session(self, session) -> None:
        session.interrupt()

    def _close_session(self, session) -> None:
        session.close()


class _PythonSession:
    def __init__(
        self,
        env=None,
        cwd=None,
        sandbox_root=None,
        network_policy=None,
        writable_paths=None,
        read_only_paths=(),
        toolchain_paths=(),
        writable_toolchain_paths=(),
    ) -> None:
        self.env = env or {}
        self.cwd = cwd
        self.sandbox_root = sandbox_root
        self.network_policy = network_policy
        self.writable_paths = writable_paths
        self.read_only_paths = read_only_paths
        self.toolchain_paths = toolchain_paths
        self.writable_toolchain_paths = writable_toolchain_paths
        self.process: subprocess.Popen | None = None
        self.frames: queue.Queue = queue.Queue()
        self.lock = threading.Lock()

    def start(self) -> None:
        self.process = spawn_owned(
            [sys.executable, "-u", "-c", _WORKER_SOURCE],
            env=self.env,
            cwd=self.cwd,
            sandbox_root=self.sandbox_root,
            network_policy=self.network_policy,
            writable_paths=self.writable_paths,
            read_only_paths=self.read_only_paths,
            toolchain_paths=self.toolchain_paths,
            writable_toolchain_paths=self.writable_toolchain_paths,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        threading.Thread(target=self._read_loop, args=(self.process.stdout,), daemon=True).start()

    def _read_loop(self, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    frame = {"type": "out", "data": line.rstrip("\n")}
                self.frames.put(frame)
        except ValueError:
            pass

    def close(self) -> None:
        with self.lock:
            if self.process:
                kill_tree(self.process)
                try:
                    if self.process.stdin:
                        self.process.stdin.close()
                except Exception:
                    pass
                self.process = None

    def interrupt(self) -> None:
        with self.lock:
            proc = self.process
        if proc and proc.poll() is None:
            interrupt_group(proc)

    def timeout_kill(self, grace: float = 2.0) -> None:
        """Escalate a timeout: SIGINT first, then after a short grace SIGKILL
        the whole tree and reset this session so the next run() starts fresh."""
        with self.lock:
            proc = self.process
        if proc and proc.poll() is None:
            interrupt_group(proc)
            time.sleep(grace)
        self.close()

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
        message = json.dumps({"source": code})
        try:
            if process.stdin is None:
                raise BrokenPipeError("python worker stdin is unavailable")
            process.stdin.write(f"{len(message)}\n")
            process.stdin.write(message)
            process.stdin.flush()
        except Exception:
            yield ExecutionEvent(
                type=ExecutionEventType.STDERR,
                execution_id=execution_id,
                data="worker lost; state reset",
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
                frame = self.frames.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None:
                    self.frames = queue.Queue()
                    yield ExecutionEvent(
                        type=ExecutionEventType.EXITED,
                        execution_id=execution_id,
                        exit_status=ExecutionExitStatus.FAILED,
                        exit_code=1,
                    )
                    return
                if deadline and time.monotonic() > deadline:
                    self.timeout_kill()
                    yield from self._drain(timeout=0.5, execution_id=execution_id)
                    yield ExecutionEvent(
                        type=ExecutionEventType.EXITED,
                        execution_id=execution_id,
                        exit_status=ExecutionExitStatus.TIMED_OUT,
                        exit_code=None,
                    )
                    return
                continue
            ftype = frame.get("type")
            if ftype == "done":
                # Check explicit success/failure from worker
                ok = frame.get("ok", True)
                if not ok:
                    yield ExecutionEvent(
                        type=ExecutionEventType.EXITED,
                        execution_id=execution_id,
                        exit_status=ExecutionExitStatus.FAILED,
                        exit_code=1,
                    )
                    return
                break
            if ftype == "out":
                if frame.get("data"):
                    yield ExecutionEvent(
                        type=ExecutionEventType.STDOUT,
                        execution_id=execution_id,
                        data=frame["data"],
                    )
            elif ftype == "err":
                yield ExecutionEvent(
                    type=ExecutionEventType.STDERR,
                    execution_id=execution_id,
                    data=frame.get("data", ""),
                )
            if deadline and time.monotonic() > deadline:
                self.timeout_kill()
                yield from self._drain(timeout=0.5, execution_id=execution_id)
                yield ExecutionEvent(
                    type=ExecutionEventType.EXITED,
                    execution_id=execution_id,
                    exit_status=ExecutionExitStatus.TIMED_OUT,
                    exit_code=None,
                )
                return
        yield ExecutionEvent(
            type=ExecutionEventType.EXITED,
            execution_id=execution_id,
            exit_status=ExecutionExitStatus.EXITED,
            exit_code=0,
        )

    def _drain(self, timeout, execution_id):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                frame = self.frames.get(timeout=0.05)
            except queue.Empty:
                continue
            if frame.get("type") == "out" and frame.get("data"):
                yield ExecutionEvent(
                    type=ExecutionEventType.STDOUT, execution_id=execution_id, data=frame["data"]
                )
            elif frame.get("type") == "err":
                yield ExecutionEvent(
                    type=ExecutionEventType.STDERR,
                    execution_id=execution_id,
                    data=frame.get("data", ""),
                )


__all__ = ["PythonRuntime"]
