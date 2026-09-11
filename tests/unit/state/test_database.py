import asyncio
import hashlib
import sqlite3
import shutil
import time
from pathlib import Path

import pytest

import athena.state.database as database_module
from athena.state.database import Database, DatabaseRecoveryRequired


@pytest.fixture
async def db():
    db = Database(":memory:")
    yield db
    await db.close()


async def test_in_memory_connect_and_migrations_run(db):
    await db._ensure_ready()
    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert "sessions" in names
    assert "tasks" in names
    assert "messages" in names
    assert "schema_migrations" in names
    assert "workflow_step_item_runs" in names

    rows = await db.fetch_all("SELECT version, sql_sha256 FROM schema_migrations")
    assert rows
    assert all(len(str(row["sql_sha256"])) == 64 for row in rows)


async def test_migration_failure_rolls_back_all_statements_and_retries_cleanly(
    tmp_path, monkeypatch
):
    migration_root = tmp_path / "state"
    migration_dir = migration_root / "migrations"
    source_dir = Path(database_module.__file__).resolve().parent / "migrations"
    shutil.copytree(source_dir, migration_dir)
    failing = migration_dir / "999_fault_injection.sql"
    failing.write_text(
        "CREATE TABLE migration_fault_one (id INTEGER);\n"
        "CREATE TABLE migration_fault_two (id INTEGER);\n"
        "SELECT migration_fault_injection_failure();\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(database_module, "__file__", str(migration_root / "database.py"))
    path = tmp_path / "state.sqlite"

    first = Database(str(path))
    with pytest.raises(sqlite3.OperationalError):
        await first._ensure_ready()
    await first.close()

    failing.unlink()
    recovered = await _ready_database(path)
    assert (
        await recovered.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_fault_one'"
        )
        is None
    )
    assert (
        await recovered.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_fault_two'"
        )
        is None
    )
    await recovered.close()

    failing.write_text(
        "CREATE TABLE migration_fault_one (id INTEGER);\n"
        "CREATE TABLE migration_fault_two (id INTEGER);\n",
        encoding="utf-8",
    )
    recovered = await _ready_database(path)
    assert (
        await recovered.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_fault_one'"
        )
        is not None
    )
    assert (
        await recovered.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_fault_two'"
        )
        is not None
    )
    row = await recovered.fetch_one(
        "SELECT sql_sha256 FROM schema_migrations WHERE version = '999'"
    )
    assert row is not None
    assert row["sql_sha256"] == hashlib.sha256(failing.read_bytes()).hexdigest()
    await recovered.close()


async def test_legacy_two_column_migration_ledger_is_upgraded(tmp_path):
    path = tmp_path / "legacy.sqlite"
    source_dir = Path(database_module.__file__).resolve().parent / "migrations"
    initial_sql = (source_dir / "001_initial.sql").read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    connection.executescript(initial_sql)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        ("001", "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()
    connection.close()

    database = Database(str(path))
    await database._ensure_ready()
    row = await database.fetch_one("SELECT sql_sha256 FROM schema_migrations WHERE version = '001'")
    assert row is not None
    assert row["sql_sha256"] == hashlib.sha256(initial_sql.encode("utf-8")).hexdigest()
    assert await database.fetch_one(
        "SELECT name FROM sqlite_master WHERE name = 'model_response_receipts'"
    )
    await database.close()


async def test_database_startup_failure_cleans_state_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "retry.sqlite"
    original_start = database_module._AsyncSQLiteConnection.start
    calls = 0

    async def fail_once(connection):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("simulated startup failure")
        await original_start(connection)

    monkeypatch.setattr(database_module._AsyncSQLiteConnection, "start", fail_once)
    database = Database(str(path))
    with pytest.raises(DatabaseRecoveryRequired, match="cannot be opened safely"):
        await database._ensure_ready()
    assert database._conn is None
    assert database._migrated is False
    assert database._startup_diagnostics["status"] == "recovery_required"

    await database._ensure_ready()
    assert database._conn is not None
    assert database._migrated is True
    await database.close()


@pytest.mark.parametrize(
    "phase", ["open", "foreign_keys", "busy_timeout", "migration", "integrity", "lifecycle"]
)
async def test_each_pre_ready_failure_is_retryable(tmp_path, phase):
    path = tmp_path / f"retry-{phase}.sqlite"
    failures = {phase}

    def inject(current_phase: str) -> None:
        if current_phase in failures:
            failures.remove(current_phase)
            raise RuntimeError(f"fault at {current_phase}")

    database = Database(str(path), startup_fault_injector=inject)
    with pytest.raises(RuntimeError, match=f"fault at {phase}"):
        await database._ensure_ready()
    assert database._conn is None
    assert database._migrated is False
    assert database._closed is False

    await database._ensure_ready()
    assert database._migrated is True
    await database.close()


async def test_wal_startup_failure_is_retryable(tmp_path):
    path = tmp_path / "retry-wal.sqlite"
    failures = {"wal"}

    def inject(phase: str) -> None:
        if phase in failures:
            failures.remove(phase)
            raise RuntimeError("fault at wal")

    database = Database(str(path), startup_fault_injector=inject)
    with pytest.raises(RuntimeError, match="fault at wal"):
        await database._ensure_ready()
    assert database._conn is None
    assert database._closed is False
    await database._ensure_ready()
    await database.close()


async def test_migration_fault_hook_rolls_back_ddl_and_ledger(tmp_path):
    path = tmp_path / "fault-hook.sqlite"

    def inject(version: str, sql: str) -> str:
        if version == "001":
            return sql + "\nSELECT migration_fault_hook_failure();\n"
        return sql

    database = Database(str(path), migration_fault_injector=inject)
    with pytest.raises(sqlite3.OperationalError):
        await database._ensure_ready()
    # The first migration contains the core tables. Neither those tables nor
    # its ledger row may survive a failure after earlier statements ran, and
    # failed startup must leave the same wrapper retryable.
    assert database._conn is None
    assert database._closed is False
    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()
        is None
    )
    assert (
        connection.execute("SELECT version FROM schema_migrations WHERE version = '001'").fetchone()
        is None
    )
    connection.close()
    await database.close()

    recovered = Database(str(path))
    await recovered._ensure_ready()
    assert (
        await recovered.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        )
        is not None
    )
    await recovered.close()


async def test_changed_applied_migration_fails_startup(tmp_path, monkeypatch):
    migration_root = tmp_path / "state"
    migration_dir = migration_root / "migrations"
    source_dir = Path(database_module.__file__).resolve().parent / "migrations"
    shutil.copytree(source_dir, migration_dir)
    migration = migration_dir / "999_hash_guard.sql"
    migration.write_text("CREATE TABLE migration_hash_guard (id INTEGER);\n", encoding="utf-8")
    monkeypatch.setattr(database_module, "__file__", str(migration_root / "database.py"))
    path = tmp_path / "hash.sqlite"

    ready = await _ready_database(path)
    await ready.close()
    migration.write_text(
        "CREATE TABLE migration_hash_guard (id INTEGER, changed INTEGER);\n",
        encoding="utf-8",
    )
    changed = Database(str(path))
    with pytest.raises(RuntimeError, match="changed after it was applied"):
        await changed._ensure_ready()
    await changed.close()


async def test_missing_applied_migration_fails_startup(tmp_path, monkeypatch):
    migration_root = tmp_path / "state"
    migration_dir = migration_root / "migrations"
    source_dir = Path(database_module.__file__).resolve().parent / "migrations"
    shutil.copytree(source_dir, migration_dir)
    migration = migration_dir / "999_missing_guard.sql"
    migration.write_text("CREATE TABLE migration_missing_guard (id INTEGER);\n", encoding="utf-8")
    monkeypatch.setattr(database_module, "__file__", str(migration_root / "database.py"))
    path = tmp_path / "missing.sqlite"

    ready = await _ready_database(path)
    await ready.close()
    migration.unlink()
    missing = Database(str(path))
    with pytest.raises(RuntimeError, match="missing from the packaged migrations"):
        await missing._ensure_ready()
    await missing.close()


async def _ready_database(path):
    database = Database(str(path))
    await database._ensure_ready()
    return database


async def test_worker_completion_wakes_the_default_event_loop(db):
    await asyncio.wait_for(db._ensure_ready(), timeout=1.5)
    await asyncio.wait_for(db.close(), timeout=1.5)


async def test_transaction_commits_on_success(db):
    await db._ensure_ready()
    async with db.transaction():
        await db.execute_raw(
            "INSERT INTO sessions(id, parent_id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
            ("s_1", None, "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", "{}"),
        )
    row = await db.fetch_one("SELECT id FROM sessions WHERE id = 's_1'")
    assert row is not None


async def test_transaction_rolls_back_on_exception(db):
    await db._ensure_ready()
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute_raw(
                "INSERT INTO sessions(id, parent_id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
                ("s_2", None, "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", "{}"),
            )
            raise RuntimeError("boom")
    row = await db.fetch_one("SELECT id FROM sessions WHERE id = 's_2'")
    assert row is None


async def test_slow_sqlite_work_does_not_stall_asyncio_heartbeat(db):
    await db._ensure_ready()
    connection = db._conn
    assert connection is not None
    await connection._call(  # noqa: SLF001 - install a deterministic test-only SQLite function
        lambda: connection._require_connection().create_function("pause", 1, time.sleep)  # noqa: SLF001
    )

    heartbeat = 0

    async def beat() -> None:
        nonlocal heartbeat
        for _ in range(10):
            heartbeat += 1
            await asyncio.sleep(0.01)

    beat_task = asyncio.create_task(beat())
    await db.fetch_one("SELECT pause(?) AS paused", (0.08,))
    await beat_task

    assert heartbeat >= 5


async def test_cancelled_sqlite_wait_does_not_poison_the_connection(db):
    """A cancelled caller cannot turn a still-draining SQLite queue stale."""
    await db._ensure_ready()
    connection = db._conn
    assert connection is not None
    await connection._call(  # noqa: SLF001 - install a deterministic test-only SQLite function
        lambda: connection._require_connection().create_function("pause", 1, time.sleep)  # noqa: SLF001
    )

    pending = asyncio.create_task(db.fetch_one("SELECT pause(?)", (0.05,)))
    await asyncio.sleep(0.005)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    # The worker completes the cancelled operation and remains usable for the
    # next caller instead of leaking a late exception or closing the queue.
    row = await asyncio.wait_for(db.fetch_one("SELECT 1 AS value"), timeout=1.5)
    assert row is not None and row["value"] == 1


async def test_sqlite_disk_failure_is_not_reported_as_success(db, monkeypatch):
    """A storage failure propagates; callers cannot mistake it for a commit."""
    await db._ensure_ready()
    connection = db._conn
    assert connection is not None
    original_call = connection._call

    async def fail(_operation):
        raise OSError("simulated disk full")

    monkeypatch.setattr(connection, "_call", fail)
    with pytest.raises(OSError, match="disk full"):
        await db.execute("CREATE TABLE should_not_claim_success (value INTEGER)")
    monkeypatch.setattr(connection, "_call", original_call)
    assert (
        await db.fetch_one("SELECT name FROM sqlite_master WHERE name = 'should_not_claim_success'")
        is None
    )


async def test_file_database_reports_clean_shutdown_and_wal_diagnostics(tmp_path):
    path = tmp_path / "diagnostics.sqlite"
    first = Database(str(path))
    await first._ensure_ready()
    await first.close()

    second = Database(str(path))
    diagnostics = await second.diagnostics()
    assert diagnostics["status"] == "ok"
    assert diagnostics["path"] == str(path)
    assert diagnostics["migration"]["status"] == "ok"
    assert diagnostics["last_clean_shutdown"] == "1"
    await second.close()


async def test_truncated_sqlite_fails_closed_and_is_operator_diagnosable(tmp_path):
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(b"SQLite format 3\x00truncated")
    database = Database(str(path))
    with pytest.raises(DatabaseRecoveryRequired):
        await database._ensure_ready()
    diagnostics = await database.diagnostics()
    assert diagnostics["status"] == "recovery_required"
    assert "error" in diagnostics
    await database.close()
