"""Local content-addressed immutable ArtifactStore (§22, §53-54).

Blobs are stored under ``<root>/blobs/<hash-prefix>/<hash>`` and are never
overwritten (BHV-066): a different payload yields a different content hash and
therefore a different artifact identity. Metadata is mirrored to sidecar JSON
records under ``<root>/_meta/<hash>.json`` so the filesystem, not a database,
is the source of truth for listing and retention.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from athena.protocol.artifacts import ArtifactRef, parse_artifact_uri
from athena.protocol.ids import new_id
from athena.protocol.messages import Provenance, utcnow

HASH_ALGO = "sha256"
HASH_PREFIX_BYTES = 2  # two hex chars => up to 256 digest-first dirs
BLOBS_DIR = "blobs"
META_DIR = "_meta"

# Local storage backend identifier (spec §53).
LOCAL_BACKEND = "local"
SHA256_ALGO = "sha256"


class ArtifactStore:
    """Content-addressed, immutable artifact store backed by a local directory."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root else _default_root()
        self._blobs = self._root / BLOBS_DIR
        self._meta = self._root / META_DIR
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._meta.mkdir(parents=True, exist_ok=True)
        # Per-digest lock for sidecar metadata updates (race-prone without lock).
        self._meta_locks: dict[str, asyncio.Lock] = {}
        self._meta_locks_lock = asyncio.Lock()

    async def _meta_lock(self, digest: str) -> asyncio.Lock:
        """Get or create a per-digest lock for sidecar updates."""
        async with self._meta_locks_lock:
            if digest not in self._meta_locks:
                self._meta_locks[digest] = asyncio.Lock()
            return self._meta_locks[digest]

    # -- writes -----------------------------------------------------------

    async def save(
        self,
        *,
        task_id: str | None = None,
        content: bytes | str,
        mime_type: str = "application/octet-stream",
        producer: Provenance | str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        """Content-address the blob, write it immutably, persist metadata."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(data).hexdigest()
        uri = f"artifact://{SHA256_ALGO}/{digest}"
        self._write_if_absent(digest, data)

        ref = ArtifactRef(
            id=uri,
            uri=uri,
            hash=digest,
            mime_type=mime_type,
            size=len(data),
            storage_path=str(self._blob_path(digest)),
            created_at=utcnow(),
            producer=_producer_str(producer),
            task_id=task_id,
            metadata=dict(metadata or {}),
        )
        await self._persist_meta(ref)
        return ref

    async def _persist_meta(self, ref: ArtifactRef) -> None:
        """Append this occurrence's provenance to the digest sidecar (BHV-067).

        The sidecar is keyed by digest only, so identical content dedups to one
        blob; provenance (task_id/producer) must be preserved per save, not
        silently replaced. Each call appends a provenance entry to the sidecar's
        ``provenances`` array while keeping a ``primary`` ref for the first
        occurrence.

        Uses a per-digest async lock to prevent concurrent identical-content
        saves from losing provenance entries (race condition fix).
        """
        sidecar = self._sidecar(ref)
        if sidecar is None:
            return
        occurrence = _ref_to_meta(ref, include_provenance=True)

        # Lock on digest to serialize concurrent sidecar updates
        lock = await self._meta_lock(ref.hash)
        async with lock:
            # Re-read inside lock to get latest state
            previous = _read_meta_sync(sidecar) if sidecar.exists() else None
            if isinstance(previous, dict) and isinstance(previous.get("provenances"), list):
                existing = previous["provenances"]
                # Remove duplicate entry (same task_id + created_at)
                for entry in existing:
                    if (
                        entry.get("task_id") == occurrence.get("task_id")
                        and entry.get("created_at") == occurrence.get("created_at")
                    ):
                        existing.remove(entry)
                        break
                existing.append(occurrence)
                previous["provenances"] = existing
            else:
                previous = {"digest": ref.hash or uri_digest(ref.uri), "provenances": [occurrence]}
            # Atomic write: temp file + os.replace (atomic on same filesystem)
            await _atomic_write_bytes(sidecar, json.dumps(previous).encode("utf-8"))

    # -- reads ------------------------------------------------------------

    async def load(self, ref: ArtifactRef | str) -> bytes:
        path = self._path_for(ref)
        if path is None or not path.exists():
            raise FileNotFoundError(f"artifact blob not found: {ref}")
        return await _to_thread(path.read_bytes)

    @asynccontextmanager
    async def open_stream(
        self, ref: ArtifactRef | str, chunk_size: int = 65536
    ) -> AsyncIterator[bytes]:
        path = self._path_for(ref)
        if path is None or not path.exists():
            raise FileNotFoundError(f"artifact blob not found: {ref}")
        loop = asyncio.get_running_loop()
        fh = await loop.run_in_executor(None, path.open, "rb")

        async def _gen() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await loop.run_in_executor(None, fh.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await loop.run_in_executor(None, fh.close)

        try:
            async for chunk in _gen():
                yield chunk
        finally:
            if not fh.closed:
                await loop.run_in_executor(None, fh.close)

    async def list(
        self,
        *,
        mime_type: str | None = None,
        task_id: str | None = None,
        producer: str | None = None,
        limit: int = 100,
    ) -> list[ArtifactRef]:
        refs: list[ArtifactRef] = []
        for sidecar in sorted(self._meta.glob("*.json")):
            meta = _read_meta_sync(sidecar)
            if meta is None:
                continue
            for ref in _occurrences(meta):
                if mime_type and ref.mime_type != mime_type:
                    continue
                if task_id and ref.task_id != task_id:
                    continue
                if producer and ref.producer != producer:
                    continue
                refs.append(ref)
                if len(refs) >= limit:
                    return refs
        return refs

    async def delete(self, ref: ArtifactRef | str) -> bool:
        """Delete an artifact occurrence.

        IMPORTANT: only removes the provenance entry from the sidecar, NOT
        the blob itself. The blob is shared across all occurrences with the
        same content hash (deduplication). The blob is only removed when
        delete_blob() is called explicitly (e.g., during garbage collection
        when no occurrences remain).
        """
        digest = _ref_digest(ref)
        if digest is None:
            return False
        removed = False
        sidecar = self._sidecar(ref)
        if sidecar is not None and sidecar.exists():
            lock = await self._meta_lock(digest)
            async with lock:
                # Remove only this occurrence's provenance
                meta = _read_meta_sync(sidecar)
                if isinstance(meta, dict):
                    provenances = meta.get("provenances", [])
                    occurrence_id = getattr(ref, "id", None)
                    new_provenances = [
                        p for p in provenances
                        if p.get("id") != occurrence_id and p.get("uri") != getattr(ref, "uri", None)
                    ]
                    if len(new_provenances) < len(provenances):
                        removed = True
                    if new_provenances:
                        meta["provenances"] = new_provenances
                        await _atomic_write_bytes(sidecar, json.dumps(meta).encode("utf-8"))
                    else:
                        # No occurrences left — remove entire sidecar
                        sidecar.unlink()
                        removed = True
        return removed

    async def delete_blob(self, ref: ArtifactRef | str) -> bool:
        """Delete the actual blob and its sidecard (garbage collection).

        This removes the shared blob file and the entire sidecar. Only call
        this when you're certain no other occurrences reference this blob.
        """
        digest = _ref_digest(ref)
        if digest is None:
            return False
        removed = False
        blob = self._blob_path(digest)
        if blob.exists():
            blob.unlink()
            _prune_empty(blob.parent)
            removed = True
        sidecar = self._sidecar(ref)
        if sidecar is not None and sidecar.exists():
            sidecar.unlink()
            removed = True
        return removed

    # -- accessors --------------------------------------------------------

    def _write_if_absent(self, digest: str, data: bytes) -> bool:
        """Write blob atomically. Uses temp file + rename for concurrency safety.

        Returns True if the blob was written, False if it already existed.
        """
        path = self._blob_path(digest)
        if path.exists():
            return False  # immutable: never overwrite an existing digest
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp file + os.replace
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".athena-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except FileExistsError:
            # Another writer created it between our check and write
            return False
        return True

    def _blob_path(self, digest: str) -> Path:
        return self._blobs / digest[:HASH_PREFIX_BYTES] / digest

    def _sidecar(self, ref: ArtifactRef | str) -> Path | None:
        digest = _ref_digest(ref)
        if digest is None:
            return None
        return self._meta / f"{digest}.json"

    def _path_for(self, ref: ArtifactRef | str) -> Path | None:
        digest = _ref_digest(ref)
        if digest is None:
            return None
        return self._blob_path(digest)


def _default_root() -> Path:
    import os

    base = os.environ.get("ATHENA_HOME") or str(Path.home() / ".athena")
    return Path(base) / "artifacts"


def _ref_digest(ref: ArtifactRef | str) -> str | None:
    if isinstance(ref, ArtifactRef):
        if ref.hash:
            return ref.hash
        return uri_digest(ref.uri)
    return uri_digest(ref)


def uri_digest(uri: str) -> str | None:
    """Extract the digest from an ``artifact://sha256/<digest>`` uri, if any."""
    parsed = parse_artifact_uri(uri)
    if parsed:
        return parsed[1] or None
    stripped = uri[len("artifact://") :] if uri.startswith("artifact://") else uri
    return None if not stripped else (stripped.rsplit("/", 1)[-1] or None)


def _producer_str(producer: Provenance | str | None) -> str | None:
    if producer is None or isinstance(producer, str):
        return producer
    st = producer.source_type
    return st.value if hasattr(st, "value") else str(st)


def _ref_to_meta(ref: ArtifactRef, include_provenance: bool = False) -> dict[str, Any]:
    meta = {
        "id": ref.id,
        "uri": ref.uri,
        "hash": ref.hash,
        "mime_type": ref.mime_type,
        "size": ref.size,
        "storage_path": ref.storage_path,
        "created_at": ref.created_at.isoformat() if ref.created_at else None,
        "producer": ref.producer,
        "task_id": ref.task_id,
        "metadata": dict(ref.metadata),
    }
    if include_provenance:
        meta["occurrence_id"] = new_id("art")
    return meta


def _meta_to_ref(meta: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        id=meta.get("id") or meta.get("uri") or "",
        uri=meta.get("uri", ""),
        hash=meta.get("hash"),
        mime_type=meta.get("mime_type"),
        size=meta.get("size"),
        storage_path=meta.get("storage_path"),
        created_at=_parse_dt(meta.get("created_at")),
        producer=meta.get("producer"),
        task_id=meta.get("task_id"),
        metadata=dict(meta.get("metadata") or {}),
    )


def _occurrences(meta: dict[str, Any]) -> list[ArtifactRef]:
    """Expand a sidecar into one ArtifactRef per persisted provenance (BHV-067)."""
    provenances = meta.get("provenances")
    if isinstance(provenances, list) and provenances:
        return [_meta_to_ref(p) for p in provenances if isinstance(p, dict)]
    primary = dict(meta)
    primary.pop("provenances", None)
    if "digest" in primary and not primary.get("uri"):
        primary["uri"] = primary["digest"]
        primary["id"] = primary["id"] or primary["uri"]
    return [_meta_to_ref(primary)]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


async def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write bytes to path using temp file + os.replace."""
    loop = asyncio.get_running_loop()

    def _write():
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".athena-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    await loop.run_in_executor(None, _write)


async def _to_thread(func: Any) -> Any:
    return await asyncio.to_thread(func)


def _read_meta_sync(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _prune_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


__all__ = [
    "ArtifactStore",
    "uri_digest",
    "SHA256_ALGO",
    "HASH_PREFIX_BYTES",
    "BLOBS_DIR",
    "META_DIR",
    "LOCAL_BACKEND",
]
