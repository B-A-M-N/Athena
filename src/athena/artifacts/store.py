"""Local content-addressed immutable ArtifactStore (§22, §53-54).

Blobs are stored under ``<root>/blobs/<hash-prefix>/<hash>`` and are never
overwritten (BHV-066): a different payload yields a different content hash and
therefore a different artifact identity. Metadata is mirrored to sidecar JSON
records under ``<root>/_meta/<hash>.json`` so the filesystem, not a database,
is the source of truth for listing and retention.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from athena.execution.async_call import run_blocking
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

    def __init__(self, root: str | Path | None = None, budget_tracker=None) -> None:
        self._root = Path(root) if root else _default_root()
        self._blobs = self._root / BLOBS_DIR
        self._meta = self._root / META_DIR
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._meta.mkdir(parents=True, exist_ok=True)
        # Per-digest lock for sidecar metadata updates (race-prone without lock).
        self._meta_locks: dict[str, asyncio.Lock] = {}
        self._meta_locks_lock = asyncio.Lock()
        self._io_slots = asyncio.Semaphore(8)
        self._budget_tracker = budget_tracker

    def set_budget_tracker(self, budget_tracker) -> None:
        self._budget_tracker = budget_tracker

    async def _io(self, function, *args, **kwargs):
        """Run bounded blob/sidecar filesystem work off the event loop."""
        async with self._io_slots:
            return await run_blocking(function, *args, **kwargs)

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
        """Content-address the blob, write it immutably, persist metadata.

        The artifact budget charges the logical task-owned occurrence size,
        even when the immutable blob is physically deduplicated with another
        occurrence. This keeps task accounting from being bypassed by
        repeated identical outputs.
        """
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(data).hexdigest()
        uri = f"artifact://{SHA256_ALGO}/{digest}"
        reserved = False
        committed = False
        if task_id and self._budget_tracker is not None:
            await self._budget_tracker.reserve_artifact(task_id, len(data))
            reserved = True
        try:
            await self._io(self._write_if_absent, digest, data)

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
            if reserved:
                await self._budget_tracker.commit_artifact(task_id, len(data))
                committed = True
            return ref
        except BaseException:
            if reserved and not committed:
                await self._budget_tracker.release_artifact(task_id, len(data))
            raise

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
        if not ref.hash:
            return
        occurrence = _ref_to_meta(ref, include_provenance=True)

        # Lock on digest to serialize concurrent sidecar updates
        lock = await self._meta_lock(ref.hash)
        async with lock:
            # Re-read inside lock to get latest state
            sidecar_exists = await self._io(sidecar.exists)
            previous = await self._io(_read_meta_sync, sidecar) if sidecar_exists else None
            if isinstance(previous, dict) and isinstance(previous.get("provenances"), list):
                existing = previous["provenances"]
                # Remove duplicate entry (same task_id + created_at)
                for entry in existing:
                    if entry.get("task_id") == occurrence.get("task_id") and entry.get(
                        "created_at"
                    ) == occurrence.get("created_at"):
                        existing.remove(entry)
                        break
                existing.append(occurrence)
                previous["provenances"] = existing
            else:
                previous = {"digest": ref.hash or uri_digest(ref.uri), "provenances": [occurrence]}
            # Atomic write: temp file + os.replace (atomic on same filesystem)
            await self._io(_atomic_write_bytes, sidecar, json.dumps(previous).encode("utf-8"))

    # -- reads ------------------------------------------------------------

    async def load(self, ref: ArtifactRef | str) -> bytes:
        path = self._path_for(ref)
        if path is None:
            raise FileNotFoundError(f"artifact blob not found: {ref}")
        return await self._io(_load_blob_sync, path, ref)

    async def read_range(self, ref: ArtifactRef | str, offset: int, limit: int) -> bytes:
        """Read a bounded byte range without materialising the complete blob."""
        if offset < 0 or limit < 0:
            raise ValueError("offset and limit must be non-negative")
        path = self._path_for(ref)
        if path is None:
            raise FileNotFoundError(f"artifact blob not found: {ref}")
        return await self._io(_read_range_checked, path, ref, offset, limit)

    @asynccontextmanager
    async def open_stream(
        self, ref: ArtifactRef | str, chunk_size: int = 65536
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        path = self._path_for(ref)
        if path is None:
            raise FileNotFoundError(f"artifact blob not found: {ref}")
        fh = await self._io(_open_blob_checked, path, ref)

        async def _gen() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await self._io(fh.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await self._io(fh.close)

        try:
            # The context manager yields a single async iterator. Consumers
            # can then iterate bounded chunks without confusing a chunk with
            # the context-manager's one required yield point.
            yield _gen()
        finally:
            if not fh.closed:
                await self._io(fh.close)

    async def list(
        self,
        *,
        mime_type: str | None = None,
        task_id: str | None = None,
        producer: str | None = None,
        limit: int | None = 100,
    ) -> list[ArtifactRef]:
        return await self._io(
            self._list_sync,
            mime_type=mime_type,
            task_id=task_id,
            producer=producer,
            limit=limit,
        )

    async def find_occurrence(self, *, task_id: str, uri: str) -> ArtifactRef | None:
        """Look up one task-owned occurrence by digest-sidecar index.

        Authorization must not depend on scanning an arbitrary first page of
        the task's artifacts. The content-addressed URI identifies exactly
        one sidecar, whose provenance list is the ownership index.
        """
        digest = _ref_digest(uri)
        if not task_id or digest is None:
            return None
        sidecar = self._meta / f"{digest}.json"
        return await self._io(
            _find_occurrence_sync,
            sidecar,
            task_id,
            uri,
        )

    async def is_visible(self, *, task_id: str, uri: str) -> bool:
        return await self.find_occurrence(task_id=task_id, uri=uri) is not None

    def _list_sync(
        self,
        *,
        mime_type: str | None,
        task_id: str | None,
        producer: str | None,
        limit: int | None,
    ) -> builtins.list[ArtifactRef]:
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
                if limit is not None and len(refs) >= limit:
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
        if sidecar is not None and await self._io(sidecar.exists):
            lock = await self._meta_lock(digest)
            async with lock:
                removed = await self._io(_remove_occurrence_sync, sidecar, ref)
        return removed

    async def delete_blob(self, ref: ArtifactRef | str) -> bool:
        """Delete the actual blob and its sidecard (garbage collection).

        This removes the shared blob file and the entire sidecar. Only call
        this when you're certain no other occurrences reference this blob.
        """
        digest = _ref_digest(ref)
        if digest is None:
            return False
        return await self._io(_delete_blob_sync, self._blobs, self._meta, digest)

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
    if parsed and parsed[0] == SHA256_ALGO and re.fullmatch(r"[0-9a-f]{64}", parsed[1]):
        return parsed[1]
    stripped = uri[len("artifact://") :] if uri.startswith("artifact://") else uri
    digest = None if not stripped else stripped.rsplit("/", 1)[-1]
    return digest if digest and re.fullmatch(r"[0-9a-f]{64}", digest) else None


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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write bytes to path using temp file + os.replace."""

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

    _write()


def _read_range_sync(path: Path, offset: int, limit: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(limit)


def _load_blob_sync(path: Path, ref: ArtifactRef | str) -> bytes:
    if not path.exists():
        raise FileNotFoundError(f"artifact blob not found: {ref}")
    return path.read_bytes()


def _read_range_checked(path: Path, ref: ArtifactRef | str, offset: int, limit: int) -> bytes:
    if not path.exists():
        raise FileNotFoundError(f"artifact blob not found: {ref}")
    return _read_range_sync(path, offset, limit)


def _open_blob_checked(path: Path, ref: ArtifactRef | str):
    if not path.exists():
        raise FileNotFoundError(f"artifact blob not found: {ref}")
    return path.open("rb")


def _find_occurrence_sync(path: Path, task_id: str, uri: str) -> ArtifactRef | None:
    if not path.exists():
        return None
    meta = _read_meta_sync(path)
    if meta is None:
        return None
    return next(
        (ref for ref in _occurrences(meta) if ref.task_id == task_id and ref.uri == uri),
        None,
    )


def _remove_occurrence_sync(path: Path, ref: ArtifactRef | str) -> bool:
    """Remove one provenance entry without blocking the event loop."""
    meta = _read_meta_sync(path)
    if not isinstance(meta, dict):
        return False
    provenances = meta.get("provenances", [])
    occurrence_id = getattr(ref, "id", None)
    uri = getattr(ref, "uri", ref if isinstance(ref, str) else None)
    new_provenances = [
        p for p in provenances if p.get("id") != occurrence_id and p.get("uri") != uri
    ]
    if len(new_provenances) == len(provenances):
        return False
    if new_provenances:
        meta["provenances"] = new_provenances
        _atomic_write_bytes(path, json.dumps(meta).encode("utf-8"))
    else:
        path.unlink()
    return True


def _delete_blob_sync(blobs: Path, meta_root: Path, digest: str) -> bool:
    removed = False
    blob = blobs / digest[:HASH_PREFIX_BYTES] / digest
    if blob.exists():
        blob.unlink()
        _prune_empty(blob.parent)
        removed = True
    sidecar = meta_root / f"{digest}.json"
    if sidecar.exists():
        sidecar.unlink()
        removed = True
    return removed


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
