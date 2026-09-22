"""Neutral async bridges shared by persistence, interfaces, and runtimes."""

from __future__ import annotations

import asyncio
import os
import threading
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Callable


_BLOCKING_POOL_LIMITS = {"short": 16, "long": 8}
_LOOP_STATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, "_BlockingLoopState"] = (
    weakref.WeakKeyDictionary()
)
_LOOP_STATES_LOCK = threading.Lock()


class _BlockingLoopState:
    """Bounded blocking admission for one event loop."""

    def __init__(self) -> None:
        self.slots = {
            name: asyncio.Semaphore(limit) for name, limit in _BLOCKING_POOL_LIMITS.items()
        }
        self.active = {name: 0 for name in _BLOCKING_POOL_LIMITS}
        self.drained = asyncio.Event()
        self.drained.set()

    def reserve(self, pool: str) -> None:
        self.active[pool] += 1
        self.drained.clear()

    def release(self, pool: str) -> None:
        self.active[pool] -= 1
        self.slots[pool].release()
        if not any(self.active.values()):
            self.drained.set()


def _loop_state(loop: asyncio.AbstractEventLoop) -> _BlockingLoopState:
    with _LOOP_STATES_LOCK:
        state = _LOOP_STATES.get(loop)
        if state is None:
            state = _BlockingLoopState()
            _LOOP_STATES[loop] = state
        return state


async def run_blocking(
    function: Callable[..., Any],
    *args: Any,
    _pool: str = "short",
    **kwargs: Any,
) -> Any:
    """Run one blocking call on a bounded, owned daemon-thread pool.

    ``short`` is for bounded filesystem/state work.  ``long`` is reserved for
    SSH, terminal, and process operations that may occupy a worker while
    waiting for external state.  Admission is cancellable; once a worker has
    started, cancellation leaves its slot reserved until that worker returns.
    The helper is intentionally below execution and state: it provides thread
    scheduling mechanics, not process or task authority.
    """
    if _pool not in _BLOCKING_POOL_LIMITS:
        raise ValueError(f"unknown blocking pool: {_pool}")
    loop = asyncio.get_running_loop()
    state = _loop_state(loop)
    await state.slots[_pool].acquire()
    state.reserve(_pool)
    result: asyncio.Future[Any] = loop.create_future()
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    outcome: tuple[Any, BaseException | None] | None = None
    outcome_lock = threading.Lock()

    def complete() -> None:
        if result.done():
            return
        with outcome_lock:
            current = outcome
        if current is None:
            return
        value, error = current
        if error is None:
            result.set_result(value)
        else:
            result.set_exception(error)

    def schedule_complete() -> None:
        """Publish worker completion while tolerating loop teardown."""
        try:
            loop.call_soon_threadsafe(complete)
        except RuntimeError:
            # A cancelled caller may close its loop before a daemon worker
            # finishes.  There is no future left to notify in that case.
            return

    def schedule_release() -> None:
        """Return admission only after the underlying worker has ended."""
        try:
            loop.call_soon_threadsafe(state.release, _pool)
        except RuntimeError:
            # The loop is gone; its semaphore cannot admit new work.  The
            # daemon worker is still allowed to finish without a callback.
            return

    def drain_wakeup() -> None:
        try:
            os.read(read_fd, 4096)
        except (BlockingIOError, OSError):
            pass
        complete()

    reader_installed = False
    try:
        try:
            loop.add_reader(read_fd, drain_wakeup)
            reader_installed = True
        except (NotImplementedError, OSError):
            reader_installed = False

        def worker() -> None:
            nonlocal outcome
            current: tuple[Any, BaseException | None]
            try:
                try:
                    value = function(*args, **kwargs)
                except BaseException as exc:
                    current = (None, exc)
                else:
                    current = (value, None)
                with outcome_lock:
                    outcome = current
                schedule_release()
                if reader_installed:
                    try:
                        os.write(write_fd, b"x")
                    except (BlockingIOError, OSError):
                        schedule_complete()
                else:
                    # Some event loops (Windows proactor, embedded/custom
                    # loops) do not implement add_reader.  Publish completion
                    # explicitly rather than waiting for a pipe event.
                    schedule_complete()
            except BaseException:
                # A failure in the completion bridge must not strand pool
                # admission.  The blocking function's own outcome is already
                # captured above whenever it ran to the bridge.
                schedule_release()

        try:
            threading.Thread(
                target=worker,
                name=f"athena-blocking-{_pool}",
                daemon=True,
            ).start()
        except BaseException:
            state.release(_pool)
            raise
        return await result
    finally:
        if reader_installed:
            try:
                loop.remove_reader(read_fd)
            except (OSError, RuntimeError):
                pass
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


async def shutdown_blocking(*, timeout: float | None = None) -> None:
    """Wait for blocking workers owned by the current event loop to drain."""
    state = _loop_state(asyncio.get_running_loop())
    waiter = state.drained.wait()
    if timeout is None:
        await waiter
    else:
        await asyncio.wait_for(waiter, timeout=max(0.0, float(timeout)))


__all__ = ["ReferenceCountedKeyedLocks", "run_blocking", "shutdown_blocking"]


class ReferenceCountedKeyedLocks:
    """Reference-counted keyed ``asyncio.Lock`` table with scoped cleanup.

    A lock exists only while at least one caller owns a reference.  Waiting
    callers own references before attempting acquisition, so a completed owner
    can never delete a table entry another waiter still needs.
    """

    def __init__(self) -> None:
        self._entries: dict[Any, tuple[asyncio.Lock, int]] = {}

    def acquire_reference(self, key: Any) -> asyncio.Lock:
        entry = self._entries.get(key)
        if entry is None:
            lock = asyncio.Lock()
            self._entries[key] = (lock, 1)
            return lock
        lock, references = entry
        self._entries[key] = (lock, references + 1)
        return lock

    def release_reference(self, lock: asyncio.Lock) -> None:
        for key, entry in list(self._entries.items()):
            candidate, references = entry
            if candidate is not lock:
                continue
            if references > 1:
                self._entries[key] = (candidate, references - 1)
            else:
                del self._entries[key]
            return
        raise ValueError("lock reference is not owned by this table")

    @asynccontextmanager
    async def lock(self, key: Any) -> AsyncIterator[asyncio.Lock]:
        """Acquire one keyed lock and drop its reference on exit."""
        lock = self.acquire_reference(key)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield lock
        finally:
            if acquired:
                lock.release()
            self.release_reference(lock)

    def scoped(self, keys: list[Any]):
        """Acquire keys in deterministic order with cancellation-safe cleanup."""
        return _ScopedKeyedLocks(self, sorted(keys, key=repr))

    async def acquire_many(self, keys: list[Any]) -> list[asyncio.Lock]:
        """Acquire a deterministic key set for legacy dispatch callers."""
        return await self.scoped(keys).__aenter__()

    def release_many(self, locks: list[asyncio.Lock]) -> None:
        """Release locks returned by :meth:`acquire_many`."""
        for lock in reversed(locks):
            lock.release()
            self.release_reference(lock)

    async def acquire_many_keys(
        self, keys: list[Any], locks: list[asyncio.Lock]
    ) -> list[asyncio.Lock]:
        """Acquire pre-reserved locks while retaining cancellation cleanup."""
        acquired: list[asyncio.Lock] = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
        except BaseException:
            for lock in reversed(acquired):
                lock.release()
            for lock in locks:
                self.release_reference(lock)
            raise
        return acquired

    def keys(self):
        return self._entries.keys()

    def __len__(self) -> int:
        return len(self._entries)


class _ScopedKeyedLocks:
    """Async context manager owning references for an ordered lock set."""

    def __init__(self, table: ReferenceCountedKeyedLocks, keys: list[Any]) -> None:
        self._table = table
        self._keys = list(keys)
        self._reserved: list[asyncio.Lock] = []
        self._acquired: list[asyncio.Lock] = []

    async def __aenter__(self) -> list[asyncio.Lock]:
        reserved: list[asyncio.Lock] = []
        acquired: list[asyncio.Lock] = []
        try:
            for key in self._keys:
                reserved.append(self._table.acquire_reference(key))
            for lock in reserved:
                await lock.acquire()
                acquired.append(lock)
            self._reserved = reserved
            self._acquired = acquired
            return acquired
        except BaseException:
            for lock in reversed(acquired):
                lock.release()
            for lock in reserved:
                self._table.release_reference(lock)
            raise

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        for lock in reversed(self._acquired):
            lock.release()
        for lock in self._reserved:
            self._table.release_reference(lock)
        self._reserved = []
        self._acquired = []
