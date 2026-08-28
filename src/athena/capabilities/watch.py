"""`watch` — subscribe to reality; observations push into the event stream.

Filesystem watches (polling-based, no external deps) and process-exit
watches emit canonical events (`WatchObserved`) through the durable event
store while their owning task runs. Watches are task-scoped, bounded, and
cleaned up on task completion.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
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

_logger = logging.getLogger("athena.watch")


class _FileWatch:
    def __init__(
        self,
        wid: str,
        root: str,
        pattern: str,
        task_id,
        *,
        max_files: int = 10_000,
        max_bytes_per_poll: int = 10 * 1024 * 1024,
        ignore_patterns: tuple[str, ...] = (),
        interval: float = 0.0,
        debounce: float = 0.0,
        workspace=None,
        observer_id: str | None = None,
        observer_profile: str | None = None,
        observer_policy=None,
        observer_budget=None,
    ):
        self.id = wid
        self.root = os.path.realpath(root)
        self.pattern = pattern or "*"
        self.task_id = task_id
        self.max_files = max(1, max_files)
        self.max_bytes_per_poll = max(1, max_bytes_per_poll)
        self.ignore_patterns = tuple(ignore_patterns)
        self.interval = max(0.0, interval)
        self.debounce = max(0.0, debounce)
        self.workspace = workspace
        self.observer_id = observer_id
        self.observer_profile = observer_profile
        self.observer_policy = observer_policy
        self.observer_budget = observer_budget
        self.degraded = False
        self.scanned_files = 0
        self.hashed_bytes = 0
        self._next_poll = 0.0
        self._pending: list[str] = []
        self._last_emit = 0.0
        self._snapshot = self._scan()

    @staticmethod
    def _fingerprint(path: str, *, hash_content: bool = True) -> tuple[str, int, int, str]:
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
            if hash_content:
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest_obj.update(chunk)
                digest = digest_obj.hexdigest()
            else:
                digest = "unhashed"
            kind = "file"
        else:
            digest = ""
            kind = "other"
        return kind, stat.st_size, stat.st_mtime_ns, digest

    def _scan(self) -> dict[str, tuple[str, int, int, str]]:
        out: dict[str, tuple[str, int, int, str]] = {}
        self.degraded = False
        self.scanned_files = 0
        self.hashed_bytes = 0
        remaining = self.max_bytes_per_poll
        for d, _, fs in os.walk(self.root):
            if any(part in (".git", "__pycache__", "node_modules") for part in d.split(os.sep)):
                continue
            for f in fs:
                p = os.path.join(d, f)
                try:
                    rel = os.path.relpath(p, self.root)
                    if any(fnmatch.fnmatch(rel, ignored) for ignored in self.ignore_patterns):
                        continue
                    if not fnmatch.fnmatch(rel, self.pattern):
                        continue
                    if self.scanned_files >= self.max_files:
                        self.degraded = True
                        break
                    size = os.path.getsize(p)
                    hash_content = size <= remaining
                    out[rel] = self._fingerprint(p, hash_content=hash_content)
                    self.scanned_files += 1
                    if hash_content:
                        remaining -= size
                        self.hashed_bytes += size
                    else:
                        self.degraded = True
                except OSError:
                    # A file can disappear between os.walk and lstat. That is
                    # a normal polling race; the next snapshot will report a
                    # deletion if it remains absent.
                    continue
            if self.scanned_files >= self.max_files:
                break
        return out

    def poll(self) -> list[str]:
        """Return relative paths changed/added/removed since last poll."""
        now = time.monotonic()
        if now < self._next_poll:
            return []
        self._next_poll = now + self.interval
        snapshot = self._scan()
        changed = [p for p, fingerprint in snapshot.items() if self._snapshot.get(p) != fingerprint]
        removed = [p for p in self._snapshot if p not in snapshot]
        self._snapshot = snapshot
        self._pending.extend(changed + [f"{p} (removed)" for p in removed])
        if not self._pending:
            return []
        if self.debounce and time.monotonic() - self._last_emit < self.debounce:
            return []
        emitted = self._pending[:]
        self._pending.clear()
        self._last_emit = time.monotonic()
        return emitted


class WatchRegistry:
    """Owns live watchers; the service polls them on its event loop."""

    def __init__(self, observer_runner=None) -> None:
        self.file_watches: dict[str, _FileWatch] = {}
        self.process_watches: dict[str, dict] = {}  # watch id -> info
        self.sink: Any = None
        self.observer_runner = observer_runner

    def bind_observer_runner(self, runner) -> None:
        self.observer_runner = runner

    def add_file(
        self,
        *,
        root: str,
        pattern: str = "*",
        task_id: str | None = None,
        watch_id: str | None = None,
        max_files: int = 10_000,
        max_bytes_per_poll: int = 10 * 1024 * 1024,
        ignore_patterns: tuple[str, ...] = (),
        interval: float = 0.0,
        debounce: float = 0.0,
        workspace=None,
        observer_id: str | None = None,
        observer_profile: str | None = None,
        observer_policy=None,
        observer_budget=None,
    ) -> str:
        """Install a file watcher for a capability or durable contract.

        The registry is deliberately the only owner of live watcher objects.
        A durable contract can provide ``watch_id`` so rehydration replaces
        the same logical subscription rather than creating duplicates.
        Callers must resolve and authorize the root before reaching here.
        """
        resolved_root = os.path.realpath(root)
        if not os.path.isdir(resolved_root):
            raise ValueError(f"not a directory: {root}")
        wid = watch_id or new_id("watch")
        self.file_watches[wid] = _FileWatch(
            wid,
            resolved_root,
            pattern or "*",
            task_id,
            max_files=max_files,
            max_bytes_per_poll=max_bytes_per_poll,
            ignore_patterns=ignore_patterns,
            interval=interval,
            debounce=debounce,
            workspace=workspace,
            observer_id=observer_id,
            observer_profile=observer_profile,
            observer_policy=observer_policy,
            observer_budget=observer_budget,
        )
        return wid

    def add_process(
        self,
        *,
        pid: int,
        start_identity: str,
        task_id: str | None = None,
        watch_id: str | None = None,
        workspace=None,
        observer_id: str | None = None,
        observer_profile: str | None = None,
        observer_policy=None,
        observer_budget=None,
    ) -> str:
        """Install a process watcher after the caller has checked authority."""
        if pid <= 0 or not os.path.exists(f"/proc/{pid}"):
            raise ValueError(f"no such pid {pid}")
        current_identity = _process_identity(pid)
        if not start_identity or current_identity != str(start_identity):
            raise ValueError(f"process identity changed for pid {pid}")
        wid = watch_id or new_id("watch")
        self.process_watches[wid] = {
            "id": wid,
            "task_id": task_id,
            "pid": pid,
            "start_identity": str(start_identity),
            "workspace": workspace,
            "observer_id": observer_id,
            "observer_profile": observer_profile,
            "observer_policy": observer_policy,
            "observer_budget": observer_budget,
        }
        return wid

    def remove(self, watch_id: str) -> bool:
        """Remove one watcher, returning whether it existed."""
        removed = self.file_watches.pop(watch_id, None) is not None
        removed = self.process_watches.pop(watch_id, None) is not None or removed
        return removed

    async def poll_all(self, sink) -> int:
        """Emit WatchObserved events for anything that changed. Returns count."""
        emitted = 0
        for w in list(self.file_watches.values()):
            try:
                # The scan is explicitly bounded by max_files and
                # max_bytes_per_poll. Keep it inline so a restricted runtime
                # whose asyncio executor cannot create worker threads does
                # not silently stop delivering observations.
                changed = w.poll()
            except (OSError, RuntimeError) as exc:
                _logger.warning("file watch %s poll failed: %s", w.id, exc)
                continue
            if changed or w.degraded:
                emitted += 1
                observation = await self._observe(
                    task_id=w.task_id,
                    observer_id=w.observer_id,
                    input_value={
                        "kind": "files",
                        "changes": changed[:20],
                        "root": w.root,
                        "degraded": w.degraded,
                        "scanned_files": w.scanned_files,
                        "hashed_bytes": w.hashed_bytes,
                    },
                    workspace=w.workspace,
                    profile=w.observer_profile,
                    task_policy=w.observer_policy,
                    task_budget=w.observer_budget,
                )
                await sink(
                    "WatchObserved",
                    {
                        "watch": w.id,
                        "kind": "files",
                        "root": w.root,
                        "changes": changed[:20],
                        "degraded": w.degraded,
                        "scanned_files": w.scanned_files,
                        "hashed_bytes": w.hashed_bytes,
                        **({"observation": observation} if observation else {}),
                    },
                    task_id=w.task_id,
                )
        for watch_id, info in list(self.process_watches.items()):
            pid = info["pid"]
            if _process_identity(pid) != info.get("start_identity"):
                emitted += 1
                exit_code = info.pop("exit_code", None)
                observation = await self._observe(
                    task_id=info["task_id"],
                    observer_id=info.get("observer_id"),
                    input_value={
                        "kind": "process",
                        "pid": pid,
                        "exited": True,
                        "exit_code": exit_code,
                    },
                    workspace=info.get("workspace"),
                    profile=info.get("observer_profile"),
                    task_policy=info.get("observer_policy"),
                    task_budget=info.get("observer_budget"),
                )
                await sink(
                    "WatchObserved",
                    {
                        "watch": info["id"],
                        "kind": "process",
                        "pid": pid,
                        "exited": True,
                        "exit_code": exit_code,
                        **(
                            {
                                "root": os.path.realpath(str(info["workspace"].root)),
                            }
                            if info.get("workspace") is not None
                            else {}
                        ),
                        **({"observation": observation} if observation else {}),
                    },
                    task_id=info["task_id"],
                )
                self.process_watches.pop(watch_id, None)
        return emitted

    async def _observe(
        self,
        *,
        task_id,
        observer_id,
        input_value,
        workspace,
        profile,
        task_policy,
        task_budget,
    ) -> dict[str, Any] | None:
        if self.observer_runner is None or not observer_id or workspace is None:
            return None
        try:
            return await self.observer_runner(
                task_id,
                observer_id,
                input_value,
                workspace,
                profile=profile,
                task_policy=task_policy,
                task_budget=task_budget,
            )
        except Exception as exc:  # noqa: BLE001 - observation must not kill polling
            _logger.warning("generated observer %s failed: %s", observer_id, exc)
            return {"status": "failed", "error": str(exc)}

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
            "by the model. File polling is bounded and subscriptions are "
            "volatile across service restart. Operations: file/process/list/stop."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": ["file", "process", "list", "stop"]},
                "path": {"type": "string"},
                "pid": {"type": "integer"},
                "watch_id": {"type": "string"},
                "pattern": {"type": "string", "maxLength": 512},
                "max_files": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "max_bytes_per_poll": {"type": "integer", "minimum": 1, "maximum": 100_000_000},
                "ignore": {
                    "type": "array",
                    "maxItems": 50,
                    "items": {"type": "string", "maxLength": 512},
                },
                "interval": {"type": "number", "minimum": 0, "maximum": 3600},
                "debounce": {"type": "number", "minimum": 0, "maximum": 60},
                "observer_id": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, registry: WatchRegistry | None = None, execution_manager=None) -> None:
        self.registry = registry or WatchRegistry()
        self.execution_manager = execution_manager

    def bind_sink(self, sink) -> None:
        self.registry.sink = sink

    async def invoke(self, request: CapabilityRequest, context=None, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        root = context.workspace.root if context else None

        if op == "file":
            path = str(args.get("path") or root or "")
            if root is None:
                return _result(request, ok=False, error="workspace required")
            root_real = os.path.realpath(root)
            path_real = os.path.realpath(
                path if os.path.isabs(path) else os.path.join(root_real, path)
            )
            if path_real != root_real and not path_real.startswith(root_real + os.sep):
                return _result(request, ok=False, error=f"watch path outside workspace: {path}")
            path = path_real
            if not os.path.isdir(path):
                return _result(request, ok=False, error=f"not a directory: {path}")
            wid = self.registry.add_file(
                root=path,
                pattern=str(args.get("pattern") or "*"),
                task_id=request.task_id,
                max_files=int(args.get("max_files") or 10_000),
                max_bytes_per_poll=int(args.get("max_bytes_per_poll") or 10 * 1024 * 1024),
                ignore_patterns=tuple(str(item) for item in (args.get("ignore") or ())),
                interval=float(args.get("interval") or 0.0),
                debounce=float(args.get("debounce") or 0.0),
                workspace=context.workspace,
                observer_id=str(args.get("observer_id") or "") or None,
                observer_profile=getattr(context, "autonomy", None),
                observer_policy=getattr(context, "capability_policy", None),
                observer_budget=getattr(context, "resource_budget", None),
            )
            return _result(request, output=f"watching {path}", meta={"watch_id": wid})

        if op == "process":
            pid = int(args.get("pid") or 0)
            if pid <= 0 or not os.path.exists(f"/proc/{pid}"):
                return _result(request, ok=False, error=f"no such pid {pid}")
            wid = new_id("watch")
            start_identity = _process_identity(pid)
            # A PID can be reused between validation and insertion.  Refuse a
            # watch if the identity could not be captured.
            if start_identity is None:
                return _result(request, ok=False, error=f"cannot identify pid {pid}")
            if self.execution_manager is not None and not self.execution_manager.owns_process(
                request.task_id, pid, start_identity
            ):
                return _result(request, ok=False, error="process is not Athena-owned")
            wid = self.registry.add_process(
                pid=pid,
                start_identity=start_identity,
                task_id=request.task_id,
                workspace=context.workspace if context else None,
                observer_id=str(args.get("observer_id") or "") or None,
                observer_profile=getattr(context, "autonomy", None),
                observer_policy=getattr(context, "capability_policy", None),
                observer_budget=getattr(context, "resource_budget", None),
            )
            return _result(request, output=f"watching pid {pid}", meta={"watch_id": wid})

        if op == "list":
            items = [
                f"file  {k} -> {v.root}"
                for k, v in self.registry.file_watches.items()
                if v.task_id == request.task_id
            ] + [
                f"proc  {v['pid']} -> {k}"
                for k, v in self.registry.process_watches.items()
                if v.get("task_id") == request.task_id
            ]
            return _result(request, output="\n".join(items) or "(none)")

        if op == "stop":
            wid = str(args.get("watch_id") or "")
            file_watch = self.registry.file_watches.get(wid)
            process_watch = self.registry.process_watches.get(wid)
            owner = (
                file_watch.task_id
                if file_watch is not None
                else process_watch.get("task_id")
                if process_watch
                else None
            )
            if owner is not None and owner != request.task_id:
                return _result(request, ok=False, error=f"unowned watch {wid}")
            removed = self.registry.remove(wid)
            if not removed:
                return _result(request, ok=False, error=f"unknown watch {wid}")
            return _result(request, output="stopped")

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
