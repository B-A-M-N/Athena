import pytest

from athena.artifacts.extractors import ArtifactExtractionService
from athena.artifacts.store import ArtifactStore


@pytest.mark.asyncio
async def test_text_extraction_records_immutable_source_provenance(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = await store.save(content="hello\nworld", mime_type="text/plain")
    extractor = ArtifactExtractionService(store)

    derived = await extractor.extract_text(source, task_id="task-a")
    assert await store.load(derived) == b"hello\nworld"
    assert derived.metadata["source_artifact_id"] == source.uri
    assert derived.metadata["source_hash"] == source.hash
    assert derived.metadata["extractor_version"] == "1"


@pytest.mark.asyncio
async def test_binary_extraction_fails_closed_and_limits_input(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = await store.save(content=b"\x00\x01", mime_type="application/octet-stream")
    extractor = ArtifactExtractionService(store)

    with pytest.raises(ValueError, match="not text-extractable"):
        await extractor.extract_text(source)

    text = await store.save(content="0123456789", mime_type="text/plain")
    with pytest.raises(ValueError, match="extraction limit"):
        await extractor.extract_text(text, max_bytes=3)


@pytest.mark.asyncio
async def test_media_info_is_bounded_metadata_only(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = await store.save(content=b"image", mime_type="image/png")
    info = await ArtifactExtractionService(store).media_info(source)
    assert info["media"] == "image"
    assert info["content_hash"] == source.hash
