"""Embedding providers for semantic and hybrid memory retrieval.

The default provider is deliberately lazy: importing Athena does not download a
model or start an inference runtime. The first indexing/query operation loads
FastEmbed on a worker thread, keeping the async service responsive while still
making semantic retrieval a real local CPU path when the ``semantic`` extra is
installed.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from athena.concurrency import run_blocking

DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"


@runtime_checkable
class MemoryEmbeddingProvider(Protocol):
    """Small provider boundary; implementations may be local or remote."""

    model: str
    version: str

    async def embed(self, text: str) -> Sequence[float]:
        """Return a finite, non-empty vector for ``text``."""


class SemanticRetrievalUnavailable(RuntimeError):
    """Raised when semantic/hybrid retrieval has no configured embeddings."""


@dataclass
class FastEmbedProvider:
    """Local CPU embedding provider backed by Qdrant's FastEmbed package.

    ``fastembed`` remains an optional dependency because it brings an ONNX
    runtime and downloads a model on first use. Athena configures this provider
    by default at the service composition boundary; if the extra is absent or
    model initialization fails, semantic retrieval reports an explicit
    ``SemanticRetrievalUnavailable`` error while lexical retrieval remains
    available.
    """

    model: str = DEFAULT_FASTEMBED_MODEL
    version: str = "fastembed"
    cache_dir: str | None = None
    # Ordinary memory writes must not synchronously trigger a model download.
    # Semantic queries call MemoryStore.ensure_embeddings() and backfill the
    # durable derived index at the point the feature is explicitly requested.
    eager_index: bool = False
    _model: object | None = field(default=None, init=False, repr=False)
    _model_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _load_model(self) -> object:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                fastembed = importlib.import_module("fastembed")
                text_embedding = fastembed.TextEmbedding
                kwargs = {"model_name": self.model}
                if self.cache_dir:
                    kwargs["cache_dir"] = self.cache_dir
                self._model = text_embedding(**kwargs)
            except Exception as exc:
                raise SemanticRetrievalUnavailable(
                    "semantic retrieval unavailable: FastEmbed could not initialize "
                    f"model {self.model!r}; install 'athena[semantic]' and verify model access"
                ) from exc
        return self._model

    async def embed(self, text: str) -> Sequence[float]:
        """Embed one string without blocking the service event loop."""
        if not isinstance(text, str) or not text.strip():
            raise SemanticRetrievalUnavailable(
                "semantic retrieval unavailable: cannot embed empty text"
            )

        def _embed_one() -> Sequence[float]:
            try:
                # Keep initialization and inference in one worker operation.
                # Besides avoiding a needless executor handoff, this ensures
                # callers never observe a half-initialized provider if the
                # event loop is cancelled between the two phases.
                model = self._load_model()
                vectors = getattr(model, "embed")([text], batch_size=1)
                vector = next(iter(vectors))
                return tuple(float(item) for item in vector)
            except StopIteration as exc:
                raise SemanticRetrievalUnavailable(
                    "semantic retrieval unavailable: FastEmbed returned no vector"
                ) from exc
            except Exception as exc:
                raise SemanticRetrievalUnavailable(
                    "semantic retrieval unavailable: FastEmbed failed to embed text"
                ) from exc

        return await run_blocking(_embed_one)

    def health(self) -> dict[str, object]:
        """Return availability without triggering model download/initialization."""
        try:
            importlib.import_module("fastembed")
        except Exception as exc:
            return {
                "configured": True,
                "available": False,
                "state": "unavailable",
                "model": self.model,
                "error": f"{type(exc).__name__}: install 'athena[semantic]'",
            }
        return {
            "configured": True,
            "available": True,
            "state": "ready",
            "model": self.model,
            "lazy": self._model is None,
        }


__all__ = [
    "DEFAULT_FASTEMBED_MODEL",
    "FastEmbedProvider",
    "MemoryEmbeddingProvider",
    "SemanticRetrievalUnavailable",
]
