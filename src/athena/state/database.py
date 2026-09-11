from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Sequence

from athena.execution.async_call import run_blocking


class DatabaseRecoveryRequired(RuntimeError):
    """The database cannot be trusted for normal service startup."""


def _sql_literal(value: str) -> str:
    """Quote an internal migration value for an executescript wrapper."""
    return "'" + str(value).replace("'", "''") + "'"


def _load_migration_files(migrations_dir: str) -> tuple[tuple[str, str, str], ...]:
    """Read and hash packaged migrations off the event loop."""
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for filename in sorted(name for name in os.listdir(migrations_dir) if name.endswith(".sql")):
        version = filename.split("_", 1)[0]
        if version in seen:
            raise RuntimeError(f"duplicate packaged migration version: {version}")
        seen.add(version)
        path = os.path.join(migrations_dir, filename)
        with open(path, "r", encoding="utf-8") as handle:
            sql = handle.read()
        entries.append((filename, sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()))
    return tuple(entries)


def _load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


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

        # A caller can be cancelled while the SQLite worker is still
        # finishing the queued operation.  The worker must still publish its
        # result so the queue drains, but no task may be left holding an
        # exception-only Future that the event loop later reports as
        # ``Future exception was never retrieved``.  This callback observes
        # late exceptions without changing normal await/raise semantics.
        def observe_late_exception(done: asyncio.Future[Any]) -> None:
            if done.cancelled():
                return
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                return

        future.add_done_callback(observe_late_exception)
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

    async def close(self, *, notify_owner: bool = True) -> None:
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
                if notify_owner and self._on_close is not None:
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
    def __init__(
        self,
        path: str = ":memory:",
        *,
        sqlite_poll_fallback: bool = False,
        migration_fault_injector: Callable[[str, str], str] | None = None,
        startup_fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        """Create a database wrapper.

        ``migration_fault_injector`` is intentionally a narrow test hook. It
        may return a modified migration body (for example one containing a
        failing SQL statement) so crash/rollback tests can exercise the
        boundary between migration DDL and its ledger row. Production callers
        leave it unset.
        """
        self._path = path
        self._sqlite_poll_fallback = bool(sqlite_poll_fallback)
        self._migration_fault_injector = migration_fault_injector
        self._startup_fault_injector = startup_fault_injector
        self._conn: _AsyncSQLiteConnection | None = None
        self._closed = False
        self._migrated = False
        self._ensure_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._txn_owner: asyncio.Task | None = None
        self._last_clean_shutdown: str | None = None
        self._startup_diagnostics: dict[str, Any] = {}

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
            connection = _AsyncSQLiteConnection(
                self._path,
                on_close=self._mark_closed,
                poll_fallback=self._sqlite_poll_fallback,
            )
            self._conn = connection
            startup_phase = "open"
            try:
                await self._conn.start()
                self._inject_startup_fault("open")
                if self._path != ":memory:":
                    startup_phase = "wal"
                    self._inject_startup_fault("wal")
                    cursor = await self._conn.execute("PRAGMA journal_mode=WAL")
                    journal = await cursor.fetchone()
                    await cursor.close()
                    if journal is None or str(journal[0]).lower() != "wal":
                        raise DatabaseRecoveryRequired(
                            f"SQLite WAL mode could not be established for {self._path}"
                        )
                startup_phase = "foreign_keys"
                self._inject_startup_fault("foreign_keys")
                await self._conn.execute("PRAGMA foreign_keys=ON")
                startup_phase = "busy_timeout"
                self._inject_startup_fault("busy_timeout")
                # File-backed services may have a short-lived second reader
                # during restart reconciliation (for example an operator
                # status probe).  Give SQLite enough time to serialize that
                # reader with a durable writer instead of surfacing a false
                # task failure under normal contention.
                await self._conn.execute("PRAGMA busy_timeout=30000")
                startup_phase = "migration"
                self._inject_startup_fault("migration")
                await self._run_migrations()
                if self._path != ":memory:":
                    startup_phase = "integrity"
                    self._inject_startup_fault("integrity")
                    # In-memory databases cannot retain an interrupted WAL or
                    # a truncated file; the explicit ``integrity_check`` API
                    # still covers them when an operator/test requests it.
                    await self._check_integrity()
                startup_phase = "lifecycle"
                self._inject_startup_fault("lifecycle")
                await self._mark_started()
                self._migrated = True
            except BaseException as exc:
                self._startup_diagnostics = {
                    "status": "recovery_required",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                try:
                    # Startup cleanup is recoverable.  The public close path
                    # marks the owning Database terminally closed, but a
                    # failed open/migration/integrity/lifecycle step must
                    # leave the same Database instance retryable.
                    await connection.close(notify_owner=False)
                except BaseException:
                    pass
                self._conn = None
                self._migrated = False
                self._txn_owner = None
                if isinstance(exc, DatabaseRecoveryRequired):
                    raise
                if isinstance(exc, sqlite3.DatabaseError) and startup_phase in {
                    "open",
                    "wal",
                    "foreign_keys",
                    "busy_timeout",
                }:
                    raise DatabaseRecoveryRequired(
                        f"SQLite database cannot be opened safely: {type(exc).__name__}: {exc}"
                    ) from exc
                raise

    def _inject_startup_fault(self, phase: str) -> None:
        if self._startup_fault_injector is not None:
            self._startup_fault_injector(phase)

    async def _run_migrations(self) -> None:
        assert self._conn is not None
        migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
        if not os.path.isdir(migrations_dir):
            return
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, "
            "sql_sha256 TEXT NOT NULL)"
        )
        await self._conn.commit()

        # Databases created before migration hashes existed have the original
        # two-column ledger. Upgrade that ledger before reading it. This is a
        # metadata-only compatibility change and is itself atomic.
        columns = await self._conn.execute("PRAGMA table_info(schema_migrations)")
        column_rows = await columns.fetchall()
        await columns.close()
        column_names = {str(row["name"]) for row in column_rows}
        if "sql_sha256" not in column_names:
            try:
                await self._conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    "ALTER TABLE schema_migrations ADD COLUMN sql_sha256 TEXT;\n"
                    "COMMIT;\n"
                )
            except BaseException:
                await self._conn.rollback()
                raise

        cur = await self._conn.execute("SELECT version, sql_sha256 FROM schema_migrations")
        rows = await cur.fetchall()
        await cur.close()
        packaged = await run_blocking(_load_migration_files, migrations_dir)
        files = [filename for filename, _sql, _digest in packaged]
        migration_sql = {
            filename.split("_", 1)[0]: (sql, digest) for filename, sql, digest in packaged
        }

        known_versions = set(migration_sql)
        for row in rows:
            version = str(row["version"])
            if version not in known_versions:
                raise RuntimeError(
                    f"applied migration {version!r} is missing from the packaged migrations"
                )
            recorded = row["sql_sha256"]
            if recorded and str(recorded) != migration_sql[version][1]:
                raise RuntimeError(
                    f"migration {version} changed after it was applied: "
                    f"recorded {recorded}, packaged {migration_sql[version][1]}"
                )

        # Backfill hashes for databases created before the hash column existed.
        # Historical SQL is immutable: the checked-in digest manifest must
        # agree with the packaged bytes before legacy rows are upgraded.
        manifest_path = os.path.join(migrations_dir, "migration-digests.json")
        try:
            baseline = await run_blocking(_load_json_file, manifest_path)
            if not isinstance(baseline, dict):
                raise ValueError("migration digest manifest must be an object")
            for filename, _sql, digest in packaged:
                expected = baseline.get(filename)
                if expected is not None and str(expected) != digest:
                    raise RuntimeError(
                        f"historical migration digest mismatch for {filename}: "
                        f"manifest {expected}, packaged {digest}"
                    )
        except FileNotFoundError:
            raise RuntimeError("migration digest manifest is missing") from None

        missing_hashes = [
            (migration_sql[str(row["version"])][1], str(row["version"]))
            for row in rows
            if not row["sql_sha256"]
        ]
        if missing_hashes:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                await self._conn.executemany(
                    "UPDATE schema_migrations SET sql_sha256 = ? WHERE version = ?",
                    missing_hashes,
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

        applied = {str(row["version"]) for row in rows}
        for fname in files:
            version = fname.split("_", 1)[0]
            if version in applied:
                continue
            sql, digest = migration_sql[version]
            # sqlite3.executescript() commits any transaction that was already
            # open before it starts. Put the transaction *inside* the script,
            # and explicitly roll it back if any statement fails. This keeps a
            # multi-statement migration and its ledger row indivisible.
            applied_at = datetime.now(timezone.utc).isoformat()
            migration_body = sql
            if self._migration_fault_injector is not None:
                migration_body = self._migration_fault_injector(version, migration_body)
            migration_script = (
                "BEGIN IMMEDIATE;\n"
                f"{migration_body}\n"
                "INSERT INTO schema_migrations(version, applied_at, sql_sha256) VALUES ("
                f"{_sql_literal(version)}, {_sql_literal(applied_at)}, {_sql_literal(digest)});\n"
                "COMMIT;\n"
            )
            try:
                await self._conn.executescript(migration_script)
            except BaseException:
                await self._conn.rollback()
                raise

    async def _check_integrity(self) -> None:
        assert self._conn is not None
        try:
            check = await self._conn.execute("PRAGMA integrity_check")
            rows = await check.fetchall()
            await check.close()
            errors = [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]
            foreign = await self._conn.execute("PRAGMA foreign_key_check")
            foreign_rows = await foreign.fetchall()
            await foreign.close()
        except (sqlite3.DatabaseError, OSError) as exc:
            raise DatabaseRecoveryRequired(
                f"SQLite integrity check could not complete: {type(exc).__name__}: {exc}"
            ) from exc
        if errors or foreign_rows:
            raise DatabaseRecoveryRequired(
                "SQLite integrity check failed; operator recovery is required: "
                + "; ".join(errors[:8] or ["foreign-key violations"])
            )

    async def _mark_started(self) -> None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT value FROM database_lifecycle WHERE key = 'last_clean_shutdown'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        self._last_clean_shutdown = str(row["value"]) if row else None
        await self._conn.execute(
            "INSERT INTO database_lifecycle(key, value, updated_at) VALUES "
            "('last_clean_shutdown', '0', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.execute(
                    "INSERT INTO database_lifecycle(key, value, updated_at) VALUES "
                    "('last_clean_shutdown', '1', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                await self._conn.commit()
            except (sqlite3.DatabaseError, OSError):
                # Closing must still release the connection; diagnostics will
                # report that the clean-shutdown marker could not be written.
                pass
            await self._conn.close()
            self._conn = None
            self._migrated = False
            self._txn_owner = None

    async def integrity_check(self) -> dict[str, Any]:
        """Return non-mutating SQLite integrity diagnostics."""
        await self._ensure_ready()
        await self._check_integrity()
        return {"status": "ok"}

    async def diagnostics(self) -> dict[str, Any]:
        """Return operator-safe DB/WAL/migration health information."""
        result: dict[str, Any] = {
            "path": self._path,
            "status": "unknown",
            "wal": {"present": False, "size": 0},
            "shm": {"present": False, "size": 0},
            "last_clean_shutdown": self._last_clean_shutdown,
            "migration": {"status": "unknown"},
        }
        if self._path != ":memory:":
            for suffix, key in (("-wal", "wal"), ("-shm", "shm")):
                sidecar = self._path + suffix
                try:
                    result[key] = {
                        "present": os.path.exists(sidecar),
                        "size": os.path.getsize(sidecar) if os.path.exists(sidecar) else 0,
                    }
                except OSError as exc:
                    result[key] = {"present": False, "size": 0, "error": type(exc).__name__}
        try:
            await self._ensure_ready()
            assert self._conn is not None
            cursor = await self._conn.execute(
                "SELECT COUNT(*) AS count, MAX(version) AS latest FROM schema_migrations"
            )
            migration_row = await cursor.fetchone()
            await cursor.close()
            migration = (
                {"count": migration_row["count"], "latest": migration_row["latest"]}
                if migration_row is not None
                else None
            )
            result["migration"] = {
                "status": "ok",
                "applied_count": int((migration or {}).get("count") or 0),
                "latest": (migration or {}).get("latest"),
            }
            result["status"] = "ok"
            result["last_clean_shutdown"] = self._last_clean_shutdown
        except (sqlite3.DatabaseError, OSError, DatabaseRecoveryRequired, RuntimeError) as exc:
            result["status"] = "recovery_required"
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["migration"] = {"status": "unavailable"}
        return result

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


__all__ = ["Database", "DatabaseRecoveryRequired"]
