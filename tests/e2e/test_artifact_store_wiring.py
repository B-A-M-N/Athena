"""E2E test: ArtifactStore is wired into the dispatcher so mutations capture
before-state and rollback restores it."""
from __future__ import annotations

import os
import tempfile

import pytest

from athena.artifacts.store import ArtifactStore
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.service.service import AthenaService
from athena.service.config import AthenaConfig, ProviderConfig


@pytest.mark.asyncio
async def test_artifact_store_injected_into_dispatcher():
    """The dispatcher constructed by AthenaService must hold the ArtifactStore
    (not None) so mutation before_ref can be captured durably."""
    tmp = tempfile.mkdtemp(prefix="athena-art-")
    cfg = AthenaConfig(
        db_path=":memory:",
        workspace_root=tmp,
        artifact_root=os.path.join(tmp, "artifacts"),
        providers=(ProviderConfig(kind="fake", name="fake"),),
    )
    svc = AthenaService(config=cfg)
    await svc.start()
    try:
        # Dispatcher.artifact_store must be the SAME instance constructed by
        # the service (identity check), not a fresh/None store.
        assert svc._dispatcher is not None
        assert svc._dispatcher._artifact_store is not None
        assert svc._dispatcher._artifact_store is svc._artifacts
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_artifact_store_persists_artifacts():
    """ArtifactStore must durably store and load content-addressed blobs."""
    tmp = tempfile.mkdtemp(prefix="athena-art-")
    store = ArtifactStore(root=os.path.join(tmp, "artifacts"))
    ref = await store.save(content=b"hello world", mime_type="text/plain")
    loaded = await store.load(ref)
    assert loaded == b"hello world"

    # list() should return our artifact
    items = await store.list()
    assert any(a.uri == ref.uri for a in items)


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_artifact_store_injected_into_dispatcher())
    asyncio.run(test_artifact_store_persists_artifacts())