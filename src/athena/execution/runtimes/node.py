"""Node.js runtime (secondary, BUILDSPEC 46 "strongly recommended").

Registers the ``node`` runtime name. Uses a persistent ``node`` worker
subprocess that evaluates source in a shared context (preserving module-level
state across executions) and streams output back as JSON frames — mirroring the
Python worker protocol (BUILDSPEC 48) for process isolation.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time

from athena.execution.process_tree import interrupt_group, kill_tree, spawn_owned
from athena.execution.runtimes.base import BaseRuntime
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
)

__all__ = ["NodeRuntime"]

_NODE_WORKER = r"""
const vm = require('vm');
const context = vm.createContext({});
function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
context.console = {
  log: (...a) => send({ type: 'out', data: a.map(String).join(' ') }),
  error: (...a) => send({ type: 'err', data: a.map(String).join(' ') }),
  warn: (...a) => send({ type: 'err', data: a.map(String).join(' ') }),
};
context.globalThis = context;
let buf = Buffer.alloc(0);

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function tryMessage() {
  const nl = buf.indexOf(10); // '\n'
  if (nl === -1) return false;
  const length = parseInt(buf.slice(0, nl).toString('utf8').trim(), 10);
  if (isNaN(length) || buf.length < nl + 1 + length) return false;
  const payload = buf.slice(nl + 1, nl + 1 + length).toString('utf8');
  buf = buf.slice(nl + 1 + length);
  let msg;
  try { msg = JSON.parse(payload); }
  catch (e) { send({ type: 'fatal', data: 'bad stdin' }); return true; }
  let success = true;
  try {
    new vm.Script(msg.source).runInContext(context, { timeout: 10000 });
  } catch (e) {
    send({ type: 'err', data: (e && e.stack ? e.stack : String(e)) });
    success = false;
  }
  send({ type: 'done', ok: success });
  return true;
}

process.stdin.on('data', (chunk) => {
  buf = Buffer.concat([buf, chunk]);
  while (tryMessage()) {}
});
"""


class NodeRuntime(BaseRuntime):
    name = "node"
    aliases = ("nodejs", "js")

    @staticmethod
    def available() -> bool:
        return shutil.which("node") is not None

    def _make_session(
        self, *, env=None, cwd=None, sandbox_root=None, network_policy=None
    ) -> "_NodeSession":
        sess = _NodeSession(
            env=env, cwd=cwd, sandbox_root=sandbox_root, network_policy=network_policy
        )
        sess.start()
        return sess

    def _run(self, session, request, execution_id):
        yield from session.run(request.source, request.timeout, execution_id)

    def _interrupt_session(self, session) -> None:
        session.interrupt()

    def _close_session(self, session) -> None:
        session.close()


class _NodeSession:
    def __init__(self, env=None, cwd=None, sandbox_root=None, network_policy=None) -> None:
        self.env = env or {}
        self.cwd = cwd
        self.sandbox_root = sandbox_root
        self.network_policy = network_policy
        self.process: subprocess.Popen | None = None
        self.frames: "queue.Queue" = queue.Queue()
        self.lock = threading.Lock()

    def start(self) -> None:
        self.process = spawn_owned(
            [shutil.which("node") or "node", "-e", _NODE_WORKER],
            env=self.env,
            cwd=self.cwd,
            sandbox_root=self.sandbox_root,
            network_policy=self.network_policy,
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
                if not isinstance(frame, dict):
                    frame = {"type": "out", "data": str(frame)}
                self.frames.put(frame)
        except ValueError:
            pass

    def close(self) -> None:
        with self.lock:
            if self.process:
                kill_tree(self.process)
                try:
                    if self.process.stdin is not None:
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

    def run(self, code, timeout, current):
        process = self.process
        if process is None or process.poll() is not None:
            self.close()
            self.start()
            process = self.process
        if process is None:
            yield ExecutionEvent(
                type=ExecutionEventType.EXITED,
                execution_id=current,
                exit_status=ExecutionExitStatus.FAILED,
                exit_code=1,
            )
            return
        message = json.dumps({"source": code})
        try:
            if process.stdin is None:
                raise BrokenPipeError("node worker stdin is unavailable")
            process.stdin.write(f"{len(message)}\n{message}")
            process.stdin.flush()
        except Exception:
            yield ExecutionEvent(
                type=ExecutionEventType.STDERR,
                execution_id=current,
                data="node worker lost; state reset",
            )
            self.close()
            self.start()
            yield ExecutionEvent(
                type=ExecutionEventType.EXITED,
                execution_id=current,
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
                        execution_id=current,
                        exit_status=ExecutionExitStatus.FAILED,
                        exit_code=1,
                    )
                    return
                if deadline and time.monotonic() > deadline:
                    self.timeout_kill()
                    yield from self._drain(0.5, current)
                    yield ExecutionEvent(
                        type=ExecutionEventType.EXITED,
                        execution_id=current,
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
                        execution_id=current,
                        exit_status=ExecutionExitStatus.FAILED,
                        exit_code=1,
                    )
                    return
                break
            if ftype == "out":
                if frame.get("data"):
                    yield ExecutionEvent(
                        type=ExecutionEventType.STDOUT, execution_id=current, data=frame["data"]
                    )
            elif ftype == "err":
                yield ExecutionEvent(
                    type=ExecutionEventType.STDERR, execution_id=current, data=frame.get("data", "")
                )
            if deadline and time.monotonic() > deadline:
                self.timeout_kill()
                yield from self._drain(0.5, current)
                yield ExecutionEvent(
                    type=ExecutionEventType.EXITED,
                    execution_id=current,
                    exit_status=ExecutionExitStatus.TIMED_OUT,
                    exit_code=None,
                )
                return
        yield ExecutionEvent(
            type=ExecutionEventType.EXITED,
            execution_id=current,
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
