from __future__ import annotations

import pytest

from athena.state.database import Database
from athena.state.failure_memory import FailureMemory


@pytest.mark.asyncio
async def test_failure_memory_is_scoped_and_deterministically_retrievable():
    db = Database(":memory:")
    memory = FailureMemory(db)
    await memory.record(
        signature_fingerprint="sig",
        capability_id="execute",
        environment_fingerprint="env",
        project_scope="repo",
        strategy={"kind": "known-repair"},
        remediation={"op": "fix"},
        evidence_ids=["e1"],
        success=True,
    )
    await memory.record(
        signature_fingerprint="sig",
        capability_id="execute",
        environment_fingerprint="other",
        project_scope="other",
        strategy={"kind": "portable"},
        success=False,
    )

    records = await memory.retrieve(
        signature_fingerprint="sig",
        capability_id="execute",
        environment_fingerprint="env",
        project_scope="repo",
    )
    assert records[0]["project_scope"] == "repo"
    assert records[0]["strategy"] == {"kind": "known-repair"}
    assert records[0]["evidence_ids"] == ["e1"]
    await db.close()
