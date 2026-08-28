from __future__ import annotations

import pytest

from athena.state.database import Database


@pytest.fixture
async def db(tmp_path):
    """Fresh in-memory Database with migrations applied."""
    database = Database()
    try:
        await database._ensure_ready()
        yield database
    finally:
        await database.close()
