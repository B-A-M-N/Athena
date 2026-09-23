"""Neutral async bridges shared by persistence, interfaces, and runtimes."""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import weakref
from concurrent.futures import Future
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Callable

from athena.concurrency.blocking_pool import BoundedDaemonExecutor


_BLOCKING_POOL_LIMITS = {"short": 16, "long": 8}
_LOOP_STATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, "_BlockingLoopState"] = (
    weakref.WeakKeyDictionary()
)
_LOOP_STATES_LOCK = threading.Lock()


class _BlockingLoopState:
    """Bounded blocking admission for one event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop_ref = weakref.ref(loop)
        self.slots = {
            name: asyncio.Semaphore(limit) for name, limit in _BLOCKING_POOL_LIMITS.items()
        }
        self.executors = {
            name: BoundedDaemonExecutor(
                max_workers=limit,
                thread_name_prefix=f"athena-blocking-{name}",
            )
            for name, limit in _BLOCKING_POOL_LIMITS.items()
        }
        self.active = {name: 0 for name in _BLOCKING_POOL_LIMITS}
        self.drained = asyncio.Event()
        self.drained.set()
        self.closed = False
        self.shutdown_complete = False
        self.shutdown_lock = asyncio.Lock()
        self._completion_queue: queue.SimpleQueue[tuple[Future, asyncio.Future[Any], str]] = (
            queue.SimpleQueue()
        )
        self._completion_read_fd, self._completion_write_fd = os.pipe()
        os.set_blocking(self._completion_read_fd, False)
        os.set_blocking(self._completion_write_fd, False)
        self._reader_installed = False
        try:
            loop.add_reader(self._completion_read_fd, self._drain_completions)
        except (NotImplementedError, OSError):
            # Proactor/embedded loops use the thread-safe callback fallback.
            pass
        else:
            self._reader_installed = True

    def reserve(self, pool: str) -> None:
        self.active[pool] += 1
        self.drained.clear()

    def release(self, pool: str) -> None:
        self.active[pool] -= 1
        self.slots[pool].release()
        if not any(self.active.values()):
            self.drained.set()

    def publish_completion(
        self,
        worker_future: Future,
        result_future: asyncio.Future[Any],
        pool: str,
    ) -> None:
        """Wake the loop once for a worker completion.

        The pipe belongs to the loop state, not an individual call.  It keeps
        selector loops compatible with runtimes whose ``call_soon_threadsafe``
        wakeup is unavailable, without recreating a file descriptor per call.
        """
        self._completion_queue.put((worker_future, result_future, pool))
        if self._reader_installed:
            try:
                os.write(self._completion_write_fd, b"x")
            except (BlockingIOError, OSError):
                # A byte is already pending, or the loop is closing.  The
                # reader will drain every queued completion in one pass.
                pass
            return
        try:
            loop = self._loop_ref()
            if loop is not None:
                loop.call_soon_threadsafe(self._drain_completions)
        except RuntimeError:
            return

    def _drain_completions(self) -> None:
        try:
            while os.read(self._completion_read_fd, 4096):
                pass
        except (BlockingIOError, OSError):
            pass
        while True:
            try:
                worker_future, result_future, pool = self._completion_queue.get_nowait()
            except queue.Empty:
                break
            self.release(pool)
            if result_future.done():
                continue
            if worker_future.cancelled():
                result_future.cancel()
                continue
            error = worker_future.exception()
            if error is not None:
                result_future.set_exception(error)
            else:
                result_future.set_result(worker_future.result())

    def close_completion_notifier(self) -> None:
        if self._reader_installed:
            try:
                loop = self._loop_ref()
                if loop is not None:
                    loop.remove_reader(self._completion_read_fd)
            except (OSError, RuntimeError):
                pass
        for descriptor in (self._completion_read_fd, self._completion_write_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _loop_state(loop: asyncio.AbstractEventLoop) -> _BlockingLoopState:
    with _LOOP_STATES_LOCK:
        state = _LOOP_STATES.get(loop)
        if state is None or state.shutdown_complete:
            state = _BlockingLoopState(loop)
            _LOOP_STATES[loop] = state
        return state


async def run_blocking(
    function: Callable[..., Any],
    *args: Any,
    _pool: str = "short",
    **kwargs: Any,
) -> Any:
    """Run one blocking call on a bounded, shared worker pool.

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
    if state.closed:
        raise RuntimeError("blocking worker pools are shut down")
    await state.slots[_pool].acquire()
    if state.closed:
        state.slots[_pool].release()
        raise RuntimeError("blocking worker pools are shut down")
    state.reserve(_pool)
    result: asyncio.Future[Any] = loop.create_future()
    try:
        worker_future = state.executors[_pool].submit(function, *args, **kwargs)
    except BaseException:
        state.release(_pool)
        raise

    worker_future.add_done_callback(
        lambda completed: state.publish_completion(completed, result, _pool)
    )
    try:
        return await result
    except asyncio.CancelledError:
        # This cancels queued work.  A running blocking function cannot be
        # forcefully stopped; its admission slot remains held by the done
        # callback until the function returns.
        worker_future.cancel()
        raise


async def shutdown_blocking(*, timeout: float | None = None) -> None:
    """Drain and close blocking workers owned by the current event loop."""
    state = _loop_state(asyncio.get_running_loop())
    async with state.shutdown_lock:
        if state.shutdown_complete:
            return
        state.closed = True
        waiter = state.drained.wait()
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout=max(0.0, float(timeout)))
        _shutdown_executors(tuple(state.executors.values()))
        state.close_completion_notifier()
        state.shutdown_complete = True


def _shutdown_executors(executors: tuple[BoundedDaemonExecutor, ...]) -> None:
    """Join shared workers outside the event loop."""
    for executor in executors:
        executor.shutdown(wait=True, cancel_futures=True)


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
