"""`watch` — subscribe to reality; observations push into the event stream.

Filesystem watches (polling-based, no external deps) and process-exit
watches emit canonical events (`WatchObserved`) through the durable event
store while their owning task runs. Watches are task-scoped, bounded, and
cleaned up on task completion.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
import os
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

_logger = logging.getLogger("athena.watch")


class _FileWatch:
    def __init__(self, wid: str, root: str, pattern: str, task_id):
        self.id = wid
        self.root = os.path.realpath(root)
        self.pattern = pattern or "*"
        self.task_id = task_id
        self._snapshot = self._scan()

    @staticmethod
    def _fingerprint(path: str) -> tuple[str, int, int, str]:
        """Return a content-aware fingerprint without following symlinks.

        mtime/size polling misses an edit when a program preserves timestamps
        (and can also miss a same-size replacement on coarse filesystems).
        Symlinks are represented by their link target rather than by reading
        the target outside the watched root.
        """
        stat = os.lstat(path)
        if os.path.islink(path):
            digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
            kind = "symlink"
        elif os.path.isfile(path):
            digest_obj = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest_obj.update(chunk)
            digest = digest_obj.hexdigest()
            kind = "file"
        else:
            digest = ""
            kind = "other"
        return kind, stat.st_size, stat.st_mtime_ns, digest

    def _scan(self) -> dict[str, tuple[str, int, int, str]]:
        out: dict[str, tuple[str, int, int, str]] = {}
        for d, _, fs in os.walk(self.root):
            if any(part in (".git", "__pycache__", "node_modules")
                   for part in d.split(os.sep)):
                continue
            for f in fs:
                p = os.path.join(d, f)
                try:
                    rel = os.path.relpath(p, self.root)
                    if fnmatch.fnmatch(rel, self.pattern):
                        out[rel] = self._fingerprint(p)
                except OSError:
                    # A file can disappear between os.walk and lstat. That is
                    # a normal polling race; the next snapshot will report a
                    # deletion if it remains absent.
                    continue
        return out

    def poll(self) -> list[str]:
        """Return relative paths changed/added/removed since last poll."""
        now = self._scan()
        changed = [p for p, fingerprint in now.items()
                   if self._snapshot.get(p) != fingerprint]
        removed = [p for p in self._snapshot if p not in now]
        self._snapshot = now
        return changed + [f"{p} (removed)" for p in removed]


class WatchRegistry:
    """Owns live watchers; the service polls them on its event loop."""

    def __init__(self) -> None:
        self.file_watches: dict[str, _FileWatch] = {}
        self.process_watches: dict[str, dict] = {}  # watch id -> info
        self.sink: Any = None

    async def poll_all(self, sink) -> int:
        """Emit WatchObserved events for anything that changed. Returns count."""
        emitted = 0
        for w in list(self.file_watches.values()):
            try:
                changed = await asyncio.get_running_loop().run_in_executor(
                    None, w.poll)
            except (OSError, RuntimeError) as exc:
                _logger.warning("file watch %s poll failed: %s", w.id, exc)
                continue
            if changed:
                emitted += 1
                await sink(
                    "WatchObserved",
                    {"watch": w.id, "kind": "files", "changes": changed[:20]},
                    task_id=w.task_id)
        for watch_id, info in list(self.process_watches.items()):
            pid = info["pid"]
            if _process_identity(pid) != info.get("start_identity"):
                emitted += 1
                exit_code = info.pop("exit_code", None)
                await sink("WatchObserved",
                           {"watch": info["id"], "kind": "process",
                            "pid": pid, "exited": True,
                            "exit_code": exit_code},
                           task_id=info["task_id"])
                self.process_watches.pop(watch_id, None)
        return emitted

    def remove_task(self, task_id: str | None) -> None:
        """Remove all watchers owned by a finalized task."""
        for wid, watch in list(self.file_watches.items()):
            if watch.task_id == task_id:
                self.file_watches.pop(wid, None)
        for wid, info in list(self.process_watches.items()):
            if info.get("task_id") == task_id:
                self.process_watches.pop(wid, None)

    def close(self) -> None:
        """Drop all subscriptions during service shutdown."""
        self.file_watches.clear()
        self.process_watches.clear()


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
            if root is None:
                return _result(request, ok=False, error="workspace required")
            root_real = os.path.realpath(root)
            path_real = os.path.realpath(path if os.path.isabs(path)
                                         else os.path.join(root_real, path))
            if path_real != root_real and not path_real.startswith(root_real + os.sep):
                return _result(request, ok=False,
                               error=f"watch path outside workspace: {path}")
            path = path_real
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
            self.registry.process_watches[wid] = {
                "id": wid, "task_id": request.task_id,
                "pid": pid, "start_identity": _process_identity(pid)}
            # A PID can be reused between validation and insertion.  Refuse a
            # watch if the identity could not be captured.
            if self.registry.process_watches[wid].get("start_identity") is None:
                self.registry.process_watches.pop(wid, None)
                return _result(request, ok=False, error=f"cannot identify pid {pid}")
            return _result(request, output=f"watching pid {pid}",
                           meta={"watch_id": wid})

        if op == "list":
            items = ([f"file  {k} -> {v.root}"
                      for k, v in self.registry.file_watches.items()
                      if v.task_id == request.task_id]
                     + [f"proc  {v['pid']} -> {k}"
                        for k, v in self.registry.process_watches.items()
                        if v.get("task_id") == request.task_id])
            return _result(request, output="\n".join(items) or "(none)")

        if op == "stop":
            wid = str(args.get("watch_id") or "")
            file_watch = self.registry.file_watches.get(wid)
            process_watch = self.registry.process_watches.get(wid)
            owner = (file_watch.task_id if file_watch is not None
                     else process_watch.get("task_id") if process_watch else None)
            if owner is not None and owner != request.task_id:
                return _result(request, ok=False, error=f"unowned watch {wid}")
            removed = self.registry.file_watches.pop(wid, None) is not None
            removed = self.registry.process_watches.pop(wid, None) is not None or removed
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


def _process_identity(pid: int) -> str | None:
    """Return a PID start identity, preventing PID-reuse observations."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            stat = handle.read()
        # ``comm`` is parenthesized and may contain spaces; splitting the
        # complete line would shift the field index.  starttime is field 22,
        # i.e. index 19 after the closing ``) ``.
        _, rest = stat.rsplit(") ", 1)
        fields = rest.split()
        return fields[19] if len(fields) > 19 else None
    except (OSError, ValueError):
        return None
