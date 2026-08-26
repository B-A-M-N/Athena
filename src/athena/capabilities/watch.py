"""`watch` — subscribe to reality; observations push into the event stream.

Filesystem watches (polling-based, no external deps) and process-exit
watches emit canonical events (`WatchObserved`) through the durable event
store while their owning task runs. Watches are task-scoped, bounded, and
cleaned up on task completion.
"""

from __future__ import annotations

import asyncio
import os
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
from athena.protocol.ids import new_id

__all__ = ["WatchCapability", "WatchRegistry"]


class _FileWatch:
    def __init__(self, wid: str, root: str, pattern: str, task_id):
        self.id = wid
        self.root = root
        self.pattern = pattern
        self.task_id = task_id
        self._snapshot = self._scan()
    def _scan(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for d, _, fs in os.walk(self.root):
            if any(part in (".git", "__pycache__", "node_modules")
                   for part in d.split(os.sep)):
                continue
            for f in fs:
                p = os.path.join(d, f)
                try:
                    out[os.path.relpath(p, self.root)] = os.path.getmtime(p)
                except OSError:
                    pass
        return out

    def poll(self) -> list[str]:
        """Return relative paths changed/added/removed since last poll."""
        now = self._scan()
        changed = [p for p, m in now.items()
                   if self._snapshot.get(p) != m]
        removed = [p for p in self._snapshot if p not in now]
        self._snapshot = now
        return changed + [f"{p} (removed)" for p in removed]


class WatchRegistry:
    """Owns live watchers; the service polls them on its event loop."""

    def __init__(self) -> None:
        self.file_watches: dict[str, _FileWatch] = {}
        self.process_watches: dict[int, dict] = {}  # pid -> info
        self.sink: Any = None

    async def poll_all(self, sink) -> int:
        """Emit WatchObserved events for anything that changed. Returns count."""
        emitted = 0
        for w in list(self.file_watches.values()):
            try:
                changed = await asyncio.get_running_loop().run_in_executor(
                    None, w.poll)
            except Exception:
                continue
            if changed:
                emitted += 1
                await sink(
                    "WatchObserved",
                    {"watch": w.id, "kind": "files", "changes": changed[:20]},
                    task_id=w.task_id)
        for pid, info in list(self.process_watches.items()):
            if not os.path.exists(f"/proc/{pid}"):
                emitted += 1
                code = info.pop("exit_code", None)
                await sink("WatchObserved",
                           {"watch": info["id"], "kind": "process",
                            "pid": pid, "exited": True},
                           task_id=info["task_id"])
                self.process_watches.pop(pid, None)
        return emitted


class WatchCapability:
    descriptor = CapabilityDescriptor(
        id="watch",
        description=(
            "Subscribe to reality: watch files/directories for changes and "
            "watch processes for exit. Observations are pushed into the "
            "durable event stream as WatchObserved events rather than polled "
            "by the model. Operations: file/process/list/stop."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "file", "process", "list", "stop"]},
                "path": {"type": "string"},
                "pid": {"type": "integer"},
                "watch_id": {"type": "string"},
                "pattern": {"type": "string"},
            },
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, registry: WatchRegistry | None = None) -> None:
        self.registry = registry or WatchRegistry()

    def bind_sink(self, sink) -> None:
        self.registry.sink = sink

    async def invoke(self, request: CapabilityRequest, context=None,
                     **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        root = context.workspace.root if context else None

        if op == "file":
            path = str(args.get("path") or root or "")
            if not os.path.isdir(path):
                return _result(request, ok=False,
                               error=f"not a directory: {path}")
            wid = new_id("watch")
            self.registry.file_watches[wid] = _FileWatch(
                wid, path, str(args.get("pattern") or ""),
                request.task_id)
            return _result(request, output=f"watching {path}",
                           meta={"watch_id": wid})

        if op == "process":
            pid = int(args.get("pid") or 0)
            if pid <= 0 or not os.path.exists(f"/proc/{pid}"):
                return _result(request, ok=False, error=f"no such pid {pid}")
            wid = new_id("watch")
            self.registry.process_watches[pid] = {
                "id": wid, "task_id": request.task_id}
            return _result(request, output=f"watching pid {pid}",
                           meta={"watch_id": wid})

        if op == "list":
            items = ([f"file  {k} -> {v.root}"
                      for k, v in self.registry.file_watches.items()]
                     + [f"proc  {k} -> {v['id']}"
                        for k, v in self.registry.process_watches.items()])
            return _result(request, output="\n".join(items) or "(none)")

        if op == "stop":
            wid = str(args.get("watch_id") or "")
            removed = (self.registry.file_watches.pop(wid, None) is not None)
            if not removed:
                for pid, info in list(self.registry.process_watches.items()):
                    if info["id"] == wid:
                        self.registry.process_watches.pop(pid, None)
                        removed = True
            return _result(request, output="stopped" if removed
                           else f"unknown watch {wid}")

        return _result(request, ok=False, error=f"unknown operation: {op}")


def _result(request, ok=True, output="", error="", meta=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=None if ok else error,
        metadata=dict(meta or {}),
    )
