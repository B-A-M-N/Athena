"""Bounded, MIME-aware artifact derivations."""

from __future__ import annotations

import io
import mimetypes
from dataclasses import dataclass
from typing import Any

from athena.protocol.artifacts import ArtifactRef

EXTRACTOR_VERSION = "1"


@dataclass(frozen=True)
class ArtifactDescription:
    artifact_id: str
    content_hash: str
    mime_type: str
    size: int
    metadata: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "content_hash": self.content_hash,
            "mime_type": self.mime_type,
            "size": self.size,
            "metadata": dict(self.metadata),
        }


class ArtifactExtractionService:
    """Perform bounded derivations through an existing ArtifactStore."""

    def __init__(self, store) -> None:
        self._store = store

    async def describe(self, ref: ArtifactRef | str) -> ArtifactDescription:
        artifact = await self._resolve(ref)
        return ArtifactDescription(
            artifact_id=artifact.uri,
            content_hash=str(artifact.hash or ""),
            mime_type=str(artifact.mime_type or "application/octet-stream"),
            size=int(artifact.size or 0),
            metadata=dict(artifact.metadata),
        )

    async def extract_text(
        self,
        ref: ArtifactRef | str,
        *,
        task_id: str | None = None,
        max_bytes: int = 10_000_000,
        max_chars: int = 200_000,
    ) -> ArtifactRef:
        artifact = await self._resolve(ref)
        data = await self._bounded_load(artifact, max_bytes)
        mime = str(artifact.mime_type or "").split(";", 1)[0].casefold()
        if mime == "application/pdf" or artifact.uri.casefold().endswith(".pdf"):
            text = self._pdf_text(data)
        elif mime.startswith("text/") or mime in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-yaml",
        }:
            text = data.decode("utf-8", errors="replace")
        else:
            raise ValueError(f"artifact MIME type is not text-extractable: {mime or 'unknown'}")
        text = text[: max(1, int(max_chars))]
        return await self._save_derived(
            artifact,
            text.encode("utf-8"),
            mime_type="text/plain; charset=utf-8",
            task_id=task_id,
            kind="text",
            metadata={"characters": len(text)},
        )

    async def pages(
        self,
        ref: ArtifactRef | str,
        *,
        task_id: str | None = None,
        max_bytes: int = 25_000_000,
        max_pages: int = 200,
    ) -> list[ArtifactRef]:
        artifact = await self._resolve(ref)
        data = await self._bounded_load(artifact, max_bytes)
        mime = str(artifact.mime_type or "").split(";", 1)[0].casefold()
        if mime != "application/pdf":
            raise ValueError("artifact pages extraction requires application/pdf")
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "PDF page extraction requires the optional pypdf dependency"
            ) from exc
        reader = PdfReader(io.BytesIO(data))
        derived: list[ArtifactRef] = []
        for index, page in enumerate(reader.pages[: max(1, int(max_pages))], start=1):
            text = page.extract_text() or ""
            derived.append(
                await self._save_derived(
                    artifact,
                    text.encode("utf-8"),
                    mime_type="text/plain; charset=utf-8",
                    task_id=task_id,
                    kind="page",
                    metadata={"page": index},
                )
            )
        return derived

    async def media_info(self, ref: ArtifactRef | str) -> dict[str, Any]:
        artifact = await self._resolve(ref)
        mime = str(artifact.mime_type or "")
        guessed = mimetypes.guess_type(artifact.uri)[0]
        return {
            "artifact_id": artifact.uri,
            "content_hash": artifact.hash,
            "mime_type": mime or guessed or "application/octet-stream",
            "size": int(artifact.size or 0),
            "media": mime.split("/", 1)[0] if "/" in mime else "binary",
            "metadata": dict(artifact.metadata),
        }

    async def _resolve(self, ref: ArtifactRef | str) -> ArtifactRef:
        if isinstance(ref, ArtifactRef):
            return ref
        matches = await self._store.list(limit=2000)
        for candidate in matches:
            if candidate.uri == ref or candidate.id == ref:
                return candidate
        raise FileNotFoundError(f"artifact metadata not found: {ref}")

    async def _bounded_load(self, artifact: ArtifactRef, max_bytes: int) -> bytes:
        if int(artifact.size or 0) > max(1, int(max_bytes)):
            raise ValueError(f"artifact exceeds extraction limit of {max_bytes} bytes")
        return await self._store.load(artifact)

    async def _save_derived(
        self,
        source: ArtifactRef,
        content: bytes,
        *,
        mime_type: str,
        task_id: str | None,
        kind: str,
        metadata: dict[str, Any],
    ) -> ArtifactRef:
        return await self._store.save(
            task_id=task_id,
            content=content,
            mime_type=mime_type,
            producer="artifact.extractor",
            metadata={
                "derived": True,
                "derivation_kind": kind,
                "source_artifact_id": source.uri,
                "source_hash": source.hash,
                "extractor": "athena.artifacts.extractors",
                "extractor_version": EXTRACTOR_VERSION,
                **metadata,
            },
        )

    @staticmethod
    def _pdf_text(data: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "PDF text extraction requires the optional pypdf dependency"
            ) from exc
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)


__all__ = ["ArtifactDescription", "ArtifactExtractionService", "EXTRACTOR_VERSION"]
