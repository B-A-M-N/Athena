"""Neutral concurrency contracts shared outside capability boundaries."""

from __future__ import annotations

import asyncio
import threading

import pytest

from athena.concurrency import ReferenceCountedKeyedLocks, run_blocking


async def test_run_blocking_returns_work_result_without_polling() -> None:
    assert await run_blocking(lambda: "done") == "done"


async def test_run_blocking_propagates_worker_exception() -> None:
    def fail() -> None:
        raise ValueError("worker failed")

    with pytest.raises(ValueError, match="worker failed"):
        await run_blocking(fail)


@pytest.mark.asyncio
async def test_run_blocking_completes_when_event_loop_has_no_reader_support(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    original = loop.add_reader

    def unsupported(*_args, **_kwargs):
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_reader", unsupported)
    try:
        assert await asyncio.wait_for(run_blocking(lambda: "fallback"), timeout=1) == "fallback"
    finally:
        monkeypatch.setattr(loop, "add_reader", original)


@pytest.mark.asyncio
async def test_run_blocking_cancellation_does_not_publish_to_cancelled_future() -> None:
    def slow() -> str:
        import time

        time.sleep(0.05)
        return "late"

    task = asyncio.create_task(run_blocking(slow))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_run_blocking_late_worker_survives_loop_teardown() -> None:
    def slow() -> str:
        import time

        time.sleep(0.05)
        return "late"

    task = asyncio.create_task(run_blocking(slow))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("count", (100, 1000))
@pytest.mark.asyncio
async def test_run_blocking_cancellation_bounds_worker_admission(count: int) -> None:
    started = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0
    state_lock = threading.Lock()

    def blocked() -> None:
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
            started.set()
        try:
            release.wait(2)
        finally:
            with state_lock:
                active -= 1

    tasks = [asyncio.create_task(run_blocking(blocked)) for _ in range(count)]
    for _ in range(1000):
        if started.is_set():
            break
        await asyncio.sleep(0)
    assert started.is_set(), "a bounded worker should start within the admission probe"
    await asyncio.sleep(0.02)
    assert peak <= 16
    for task in tasks[16:]:
        task.cancel()
    release.set()
    done, pending = await asyncio.wait(tasks, timeout=5)
    assert not pending, "cancelled waiters and released workers must drain promptly"
    results = [
        task.exception() if not task.cancelled() else asyncio.CancelledError() for task in done
    ]
    assert all(isinstance(result, (asyncio.CancelledError, type(None))) for result in results)


@pytest.mark.asyncio
async def test_run_blocking_keeps_long_waiters_from_exhausting_short_capacity() -> None:
    started = threading.Event()
    release = threading.Event()
    active = 0
    state_lock = threading.Lock()

    def long_blocked() -> None:
        nonlocal active
        with state_lock:
            active += 1
            if active == 8:
                started.set()
        try:
            release.wait(2)
        finally:
            with state_lock:
                active -= 1

    long_tasks = [asyncio.create_task(run_blocking(long_blocked, _pool="long")) for _ in range(10)]
    for _ in range(1000):
        if started.is_set():
            break
        await asyncio.sleep(0)
    assert started.is_set(), "all long-pool workers should admit before the probe"

    assert await asyncio.wait_for(run_blocking(lambda: "short"), timeout=1) == "short"

    for task in long_tasks[8:]:
        task.cancel()
    release.set()
    results = await asyncio.gather(*long_tasks, return_exceptions=True)
    assert all(isinstance(result, (asyncio.CancelledError, type(None))) for result in results)


def test_run_blocking_completes_when_default_executor_wakeup_is_unobserved():
    class Loop(asyncio.SelectorEventLoop):
        def call_soon_threadsafe(self, callback, *args, context=None):
            return None

    async def child() -> None:
        assert await asyncio.wait_for(run_blocking(lambda: 321), timeout=2) == 321

    loop = Loop()
    try:
        loop.run_until_complete(child())
    finally:
        loop.close()


async def test_keyed_lock_context_removes_entries_after_completion():
    keyed = ReferenceCountedKeyedLocks()
    async with keyed.lock("task-1") as lock:
        assert lock.locked()
        assert len(keyed) == 1
    assert len(keyed) == 0


async def test_keyed_lock_waits_preserve_shared_entry_through_release_race():
    keyed = ReferenceCountedKeyedLocks()
    order = []

    async def owner():
        async with keyed.lock("shared"):
            order.append("owner")
            await asyncio.sleep(0)

    async def waiter():
        order.append("waiter-start")
        async with keyed.lock("shared"):
            order.append("waiter")

    await asyncio.gather(owner(), waiter())
    assert order == ["owner", "waiter-start", "waiter"]
    assert len(keyed) == 0


async def test_scoped_multi_key_locks_are_cancellation_safe():
    keyed = ReferenceCountedKeyedLocks()
    async with keyed.scoped(["b", "a"]) as locks:
        assert len(keyed) == 2
        assert [lock.locked() for lock in locks] == [True, True]
    assert len(keyed) == 0
    assert all(not lock.locked() for lock in locks)


async def test_scoped_multi_key_locks_release_when_wait_times_out():
    keyed = ReferenceCountedKeyedLocks()
    blocker_lock = keyed.acquire_reference("a")
    await blocker_lock.acquire()
    scope = keyed.scoped(["a", "b"])
    waiter = asyncio.create_task(scope.__aenter__())
    for _ in range(20):
        if waiter.done():
            break
        await asyncio.sleep(0)
    assert not waiter.done(), "scoped lock acquisition should block on held key"
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert len(keyed) == 1
    keyed.release_reference(blocker_lock)
    assert len(keyed) == 0
