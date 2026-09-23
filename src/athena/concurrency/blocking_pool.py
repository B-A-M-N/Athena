"""Bounded daemon-worker executor used by the neutral async bridge."""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable


class BoundedDaemonExecutor:
    """A small bounded executor whose running calls remain cancellable at admission.

    The worker functions themselves are ordinary synchronous calls and cannot
    be forcefully interrupted.  Futures that have not started can be
    cancelled, while running calls keep their worker slot until completion.
    Daemon workers prevent an abandoned event loop from keeping the process
    alive; explicit ``shutdown(wait=True)`` remains the normal lifecycle path.
    """

    def __init__(self, *, max_workers: int, thread_name_prefix: str) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._work: queue.Queue[Any] = queue.Queue()
        self._threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._shutdown = False
        self._sentinel = object()

    def submit(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("bounded executor is shut down")
            self._work.put((future, function, args, kwargs))
            self._start_workers_locked()
        return future

    def _start_workers_locked(self) -> None:
        while len(self._threads) < self._max_workers:
            worker = threading.Thread(
                target=self._worker,
                name=f"{self._thread_name_prefix}-{len(self._threads) + 1}",
                daemon=True,
            )
            self._threads.add(worker)
            worker.start()

    def _worker(self) -> None:
        current = threading.current_thread()
        while True:
            item = self._work.get()
            try:
                if item is self._sentinel:
                    return
                future, function, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(function(*args, **kwargs))
                except BaseException as exc:  # preserve the blocking call's outcome
                    future.set_exception(exc)
            finally:
                self._work.task_done()
                if item is self._sentinel:
                    with self._lock:
                        self._threads.discard(current)
                    return

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                if cancel_futures:
                    self._cancel_queued_locked()
                for _ in self._threads:
                    self._work.put(self._sentinel)
            threads = tuple(self._threads)
        if wait:
            for worker in threads:
                worker.join()

    def _cancel_queued_locked(self) -> None:
        retained: list[Any] = []
        while True:
            try:
                item = self._work.get_nowait()
            except queue.Empty:
                break
            if item is self._sentinel:
                retained.append(item)
            else:
                item[0].cancel()
            self._work.task_done()
        for item in retained:
            self._work.put(item)


__all__ = ["BoundedDaemonExecutor"]
