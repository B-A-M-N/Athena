from __future__ import annotations

import sys
import threading
import types

import pytest

from athena.memory.embeddings import DEFAULT_FASTEMBED_MODEL, FastEmbedProvider


class _FakeTextEmbedding:
    init_kwargs: dict[str, object] | None = None

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs
        type(self).init_kwargs = kwargs

    def embed(self, documents, *, batch_size: int = 256):
        assert batch_size == 1
        for document in documents:
            yield (float(len(document)), 1.0)


@pytest.mark.asyncio
async def test_fastembed_provider_is_lazy_and_nonblocking(monkeypatch, tmp_path):
    fake = types.SimpleNamespace(TextEmbedding=_FakeTextEmbedding)
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    provider = FastEmbedProvider(cache_dir=str(tmp_path))
    assert provider.health() == {
        "configured": True,
        "available": True,
        "state": "ready",
        "model": DEFAULT_FASTEMBED_MODEL,
        "lazy": True,
    }

    vector = await provider.embed("brief responses")

    assert vector == (15.0, 1.0)
    assert _FakeTextEmbedding.init_kwargs == {
        "model_name": DEFAULT_FASTEMBED_MODEL,
        "cache_dir": str(tmp_path),
    }
    assert provider.health()["lazy"] is False


@pytest.mark.asyncio
async def test_fastembed_initialization_runs_off_event_loop(monkeypatch):
    started = threading.Event()

    class SlowTextEmbedding(_FakeTextEmbedding):
        def __init__(self, **kwargs: object) -> None:
            started.set()
            super().__init__(**kwargs)

    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        types.SimpleNamespace(TextEmbedding=SlowTextEmbedding),
    )
    provider = FastEmbedProvider()
    await provider.embed("hello")
    assert started.is_set()
