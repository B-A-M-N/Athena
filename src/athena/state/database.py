from __future__ import annotations

import asyncio
import os
import queue
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Sequence

import sqlite3


class _AsyncSQLiteConnection:
    """Awaitable facade over one serialized SQLite connection.

    SQLite owns a connection and cursor from the thread that created it.
    A dedicated daemon worker therefore owns both and services a small queue;
    the asyncio thread only enqueues work and awaits a loop-bound Future. This
    keeps disk access, lock waits, and cursor operations off Athena's event
    loop while preserving one-connection transaction affinity.
    """

    def __init__(
        self,
        path: str,
        *,
        on_close: Callable[[], None] | None = None,
        poll_fallback: bool = False,
    ) -> None:
        self._path = path
        self._on_close = on_close
        self._connection: sqlite3.Connection | None = None
        self._queue: queue.Queue[tuple[Callable[[], Any], asyncio.Future[Any]] | None] = (
            queue.Queue()
        )
        self._closed = False
        self._poll_fallback = poll_fallback
        self._started = False
        self._wake_read: int | None = None
        self._wake_write: int | None = None
        self._wake_loop: asyncio.AbstractEventLoop | None = None
        try:
            self._wake_read, self._wake_write = os.pipe()
            os.set_blocking(self._wake_read, False)
            os.set_blocking(self._wake_write, False)
        except OSError:
            self._close_wakeup_pipe()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="athena-sqlite",
            daemon=True,
        )
        self._thread.start()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._call(self._open)

    def _open(self) -> None:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        self._connection = connection

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLite connection is not open")
        return self._connection

    async def _call(self, operation: Callable[[], Any]) -> Any:
        if self._closed:
            raise RuntimeError("SQLite connection is closed")
        loop = asyncio.get_running_loop()
        self._install_wakeup(loop)
        future: asyncio.Future[Any] = loop.create_future()
        self._queue.put((operation, future))
        # ``call_soon_threadsafe`` completes the loop-owned Future. The pipe
        # is a second wakeup path because embedded/sandboxed event loops may
        # deny writes to asyncio's private socketpair.
        if self._poll_fallback or self._wake_loop is None:
            # Explicit compatibility mode for embedded adapters that violate
            # the asyncio thread-safe wakeup contract, or platforms without a
            # selector reader API. The ordinary path has no timer polling.
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=0.001)
                except TimeoutError:
                    continue
                return future.result()
        return await future

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            operation, future = item
            try:
                value = operation()
            except BaseException as exc:  # propagate SQLite and callback failures
                self._publish_future(future, error=exc)
            else:
                self._publish_future(future, value=value)

    def _install_wakeup(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._wake_loop is loop or self._wake_read is None:
            return
        if self._wake_loop is not None:
            try:
                self._wake_loop.remove_reader(self._wake_read)
            except (OSError, RuntimeError):
                pass
        if not hasattr(loop, "add_reader"):
            return
        try:
            loop.add_reader(self._wake_read, self._drain_wakeup)
        except (NotImplementedError, OSError):
            return
        self._wake_loop = loop

    def _drain_wakeup(self) -> None:
        if self._wake_read is None:
            return
        try:
            os.read(self._wake_read, 4096)
        except (BlockingIOError, OSError):
            pass

    def _publish_future(
        self,
        future: asyncio.Future[Any],
        *,
        value: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """Complete a loop-owned Future from the SQLite worker thread."""
        loop = future.get_loop()

        def complete() -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

        try:
            loop.call_soon_threadsafe(complete)
        except RuntimeError:
            # The owning loop is already gone; no consumer can observe this
            # result and the daemon worker must still be allowed to exit.
            return
        if self._wake_write is not None:
            try:
                os.write(self._wake_write, b"\\0")
            except (BlockingIOError, OSError):
                pass

    def _close_wakeup_pipe(self) -> None:
        if self._wake_loop is not None and self._wake_read is not None:
            try:
                self._wake_loop.remove_reader(self._wake_read)
            except (OSError, RuntimeError):
                pass
        for fd in (self._wake_read, self._wake_write):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_read = None
        self._wake_write = None
        self._wake_loop = None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> "_AsyncSQLiteCursor":
        def operation() -> tuple[sqlite3.Cursor, int]:
            cursor = self._require_connection().execute(sql, params)
            return cursor, cursor.rowcount

        cursor, rowcount = await self._call(operation)
        return _AsyncSQLiteCursor(self, cursor, rowcount)

    async def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> "_AsyncSQLiteCursor":
        def operation() -> tuple[sqlite3.Cursor, int]:
            cursor = self._require_connection().executemany(sql, params)
            return cursor, cursor.rowcount

        cursor, rowcount = await self._call(operation)
        return _AsyncSQLiteCursor(self, cursor, rowcount)

    async def executescript(self, sql: str) -> "_AsyncSQLiteCursor":
        def operation() -> tuple[sqlite3.Cursor, int]:
            cursor = self._require_connection().executescript(sql)
            return cursor, cursor.rowcount

        cursor, rowcount = await self._call(operation)
        return _AsyncSQLiteCursor(self, cursor, rowcount)

    async def commit(self) -> None:
        await self._call(lambda: self._require_connection().commit())

    async def rollback(self) -> None:
        await self._call(lambda: self._require_connection().rollback())

    async def close(self) -> None:
        if self._closed:
            return

        def operation() -> None:
            connection = self._connection
            if connection is None:
                return
            try:
                connection.close()
            finally:
                self._connection = None
                if self._on_close is not None:
                    self._on_close()

        try:
            await self._call(operation)
        finally:
            self._closed = True
            self._queue.put(None)
            self._close_wakeup_pipe()


class _AsyncSQLiteCursor:
    def __init__(
        self,
        connection: _AsyncSQLiteConnection,
        cursor: sqlite3.Cursor,
        rowcount: int,
    ) -> None:
        self._connection = connection
        self._cursor = cursor
        self._rowcount = rowcount

    @property
    def rowcount(self) -> int:
        return self._rowcount

    async def fetchone(self) -> sqlite3.Row | None:
        return await self._connection._call(self._cursor.fetchone)  # noqa: SLF001

    async def fetchall(self) -> list[sqlite3.Row]:
        return await self._connection._call(self._cursor.fetchall)  # noqa: SLF001

    async def close(self) -> None:
        await self._connection._call(self._cursor.close)  # noqa: SLF001


class Database:
    def __init__(self, path: str = ":memory:", *, sqlite_poll_fallback: bool = False) -> None:
        self._path = path
        self._sqlite_poll_fallback = bool(sqlite_poll_fallback)
        self._conn: _AsyncSQLiteConnection | None = None
        self._closed = False
        self._migrated = False
        self._ensure_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._txn_owner: asyncio.Task | None = None

    async def _ensure_ready(self) -> None:
        if self._closed:
            raise RuntimeError("database is closed")
        # P0: concurrency-safe initialization. Multiple concurrent callers
        # (e.g. background world-state writes) must not race to run
        # migrations twice — that causes "table sessions already exists".
        async with self._ensure_lock:
            await self._ensure_ready_unlocked()

    async def _ensure_ready_unlocked(self) -> None:
        if self._conn is None:
            self._conn = _AsyncSQLiteConnection(
                self._path,
                on_close=self._mark_closed,
                poll_fallback=self._sqlite_poll_fallback,
            )
            await self._conn.start()
            if self._path != ":memory:":
                await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.execute("PRAGMA busy_timeout=5000")
        if not self._migrated:
            await self._run_migrations()
            self._migrated = True

    async def _run_migrations(self) -> None:
        assert self._conn is not None
        migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
        if not os.path.isdir(migrations_dir):
            return
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await self._conn.commit()
        cur = await self._conn.execute("SELECT version FROM schema_migrations")
        rows = await cur.fetchall()
        await cur.close()
        applied = {row["version"] for row in rows}
        files = sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))
        for fname in files:
            version = fname.split("_", 1)[0]
            if version in applied:
                continue
            with open(os.path.join(migrations_dir, fname), "r", encoding="utf-8") as f:
                sql = f.read()
            await self._conn.executescript(sql)
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
            await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self._migrated = False
            self._txn_owner = None

    def _mark_closed(self) -> None:
        self._closed = True

    async def _acquire(self) -> bool:
        """Acquire the serialization lock unless the current task already owns
        the open transaction. Returns True when the caller must release."""
        task = asyncio.current_task()
        if task is not None and task is self._txn_owner:
            return False
        await self._lock.acquire()
        return True

    def _release(self) -> None:
        self._lock.release()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> _AsyncSQLiteCursor:
        await self._ensure_ready()
        assert self._conn is not None
        if await self._acquire():
            try:
                cur = await self._conn.execute(sql, params)
                await self._conn.commit()
                return cur
            finally:
                self._release()
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur

    async def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        await self._ensure_ready()
        assert self._conn is not None
        if await self._acquire():
            try:
                await self._conn.executemany(sql, seq)
                await self._conn.commit()
            finally:
                self._release()
            return
        await self._conn.executemany(sql, seq)
        await self._conn.commit()

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        await self._ensure_ready()
        assert self._conn is not None
        if await self._acquire():
            try:
                return await self._fetch_one_locked(sql, params)
            finally:
                self._release()
        return await self._fetch_one_locked(sql, params)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        await self._ensure_ready()
        assert self._conn is not None
        if await self._acquire():
            try:
                return await self._fetch_all_locked(sql, params)
            finally:
                self._release()
        return await self._fetch_all_locked(sql, params)

    async def _fetch_one_locked(self, sql: str, params: Sequence[Any]) -> dict | None:
        assert self._conn is not None
        cur = await self._conn.execute(sql, params)
        try:
            row = await cur.fetchone()
            if row is None:
                return None
            return {key: row[key] for key in row.keys()}
        finally:
            await cur.close()

    async def _fetch_all_locked(self, sql: str, params: Sequence[Any]) -> list[dict]:
        assert self._conn is not None
        cur = await self._conn.execute(sql, params)
        try:
            rows = await cur.fetchall()
            return [{key: row[key] for key in row.keys()} for row in rows]
        finally:
            await cur.close()

    async def commit(self) -> None:
        if self._conn is not None:
            await self._conn.commit()

    async def execute_raw(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> _AsyncSQLiteCursor:
        """Execute without auto-commit; caller owns the transaction.

        BEGIN/COMMIT/<BIC_SWIFT placeholder> and other transactional control
        statements hold the serialization lock until the transaction ends so an
        interleaved coroutine cannot start a transaction within a transaction.
        """
        await self._ensure_ready()
        assert self._conn is not None
        stripped = sql.strip().upper()
        task = asyncio.current_task()
        if stripped.startswith(("BEGIN", "COMMIT", "ROLLBACK", "END")):
            if stripped.startswith("BEGIN"):
                if await self._acquire():
                    self._txn_owner = task
                cur = await self._conn.execute(sql, params)
            else:
                cur = await self._conn.execute(sql, params)
                if task is not None and task is self._txn_owner:
                    self._txn_owner = None
                    self._release()
            return cur
        if task is not None and task is self._txn_owner:
            return await self._conn.execute(sql, params)
        async with self._lock:
            return await self._conn.execute(sql, params)

    async def executemany_raw(
        self,
        sql: str,
        params: Sequence[Sequence[Any]],
    ) -> None:
        """Execute many rows without auto-commit inside the caller's transaction."""
        await self._ensure_ready()
        assert self._conn is not None
        task = asyncio.current_task()
        if task is not None and task is self._txn_owner:
            await self._conn.executemany(sql, params)
            return
        async with self._lock:
            await self._conn.executemany(sql, params)

    async def fetch_one_raw(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> dict | None:
        """Fetch one row without auto-commit; caller owns the transaction."""
        await self._ensure_ready()
        if await self._acquire():
            try:
                return await self._fetch_one_locked(sql, params)
            finally:
                self._release()
        return await self._fetch_one_locked(sql, params)

    async def fetch_all_raw(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[dict]:
        """Fetch many rows without auto-commit; caller owns the transaction."""
        await self._ensure_ready()
        if await self._acquire():
            try:
                return await self._fetch_all_locked(sql, params)
            finally:
                self._release()
        return await self._fetch_all_locked(sql, params)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["Database"]:
        await self._ensure_ready()
        assert self._conn is not None
        task = asyncio.current_task()
        await self._lock.acquire()
        self._txn_owner = task
        try:
            try:
                await self._conn.execute("BEGIN")
                yield self
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        finally:
            self._txn_owner = None
            self._lock.release()


__all__ = ["Database"]
