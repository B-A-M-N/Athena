"""Artifact retention and garbage collection.

``cleanup`` removes content-addressed artifacts older than ``max_age`` or which
push total storage past ``max_bytes``. Artifacts referenced by active sessions
or running tasks (BHV-067 provenance) are NEVER collected; callers pass a
``referenced`` predicate/set so the boundary is explicit.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from athena.artifacts.store import (
    ArtifactRef,
    ArtifactStore,
    _meta_to_ref,
    _read_meta_sync,
)
from athena.protocol.messages import utcnow


async def cleanup(
    store: ArtifactStore,
    *,
    max_age: timedelta | None = None,
    max_bytes: int | None = None,
    keep_referenced: bool = True,
    referenced: Callable[[ArtifactRef], bool] | set[str] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete artifacts over policy; never delete referenced ones.

    Returns ``{"deleted", "kept", "protected"}`` counts.
    """
    now = now or utcnow()
    stats = {"deleted": 0, "kept": 0, "protected": 0}

    candidates: list[ArtifactRef] = []
    for sidecar in sorted(store._meta.glob("*.json")):
        meta = _read_meta_sync(sidecar)
        if meta is None:
            continue
        ref = _meta_to_ref(meta)
        if keep_referenced and _is_referenced(ref, referenced):
            stats["protected"] += 1
            continue
        candidates.append(ref)

    candidates.sort(key=lambda r: (r.created_at or utcnow(), 0))

    budget = max_bytes
    for ref in candidates:
        too_old = max_age is not None and (
            ref.created_at is None or (now - ref.created_at) > max_age
        )
        too_large = budget is not None and _store_bytes(store) > budget
        if not (too_old or too_large):
            stats["kept"] += 1
            continue
        if await store.delete(ref):
            stats["deleted"] += 1

    return stats


def _is_referenced(
    ref: ArtifactRef,
    referenced: Callable[[ArtifactRef], bool] | set[str] | None,
) -> bool:
    if referenced is None:
        return False
    if callable(referenced):
        try:
            return bool(referenced(ref))
        except Exception:
            return False
    return (ref.hash in referenced) or (ref.uri in referenced)


def _store_bytes(store: ArtifactStore) -> int:
    total = 0
    for p in store._blobs.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total if hasattr(store, "_blobs") else 0


def _now() -> datetime:
    return utcnow()


__all__ = ["cleanup"]
