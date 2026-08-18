import pytest

from athena.artifacts.store import ArtifactStore
from athena.artifacts import refs


@pytest.fixture
async def store(tmp_path):
    yield ArtifactStore(tmp_path / "artifacts")


async def test_save_returns_sha256_ref_and_load_round_trips(store):
    ref = await store.save(
        task_id="task-1",
        content="hello artifact content",
        mime_type="text/plain",
        producer="test",
    )
    assert ref.hash
    assert len(ref.hash) == 64
    assert ref.uri.startswith("artifact://sha256/")
    assert ref.uri.endswith(ref.hash)

    data = await store.load(ref)
    assert data.decode("utf-8") == "hello artifact content"


async def test_same_content_different_task_id_persists_both_provenances(store):
    content = "shared immutable payload"
    ref1 = await store.save(task_id="task-a", content=content, producer="test")
    ref2 = await store.save(task_id="task-b", content=content, producer="test")

    assert ref1.hash == ref2.hash
    listing = await store.list()
    task_ids = {r.task_id for r in listing if r.task_id is not None}
    assert task_ids == {"task-a", "task-b"}


async def test_large_content_above_threshold_is_artifactized(tmp_path):
    store = ArtifactStore(tmp_path / "big")
    big = "A" * 60_000

    result = await refs.maybe_artifactize(store, big, threshold=50_000)
    assert hasattr(result, "hash")

    small = await refs.maybe_artifactize(store, "small inline text", threshold=50_000)
    assert small == "small inline text"