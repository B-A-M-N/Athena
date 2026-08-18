from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Sequence

import aiosqlite


class Database:
    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._migrated = False
        self._lock = asyncio.Lock()
        self._txn_owner: asyncio.Task | None = None

    async def _ensure_ready(self) -> None:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
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

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
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
    ) -> aiosqlite.Cursor:
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
