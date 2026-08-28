"""Service-owned project-index lifecycle and invalidation boundary."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from typing import Any

from athena.project.index.builder import ProjectIndexBuilder
from athena.project.index.models import ProjectIndex
from athena.project.index.store import ProjectIndexStore


class ProjectIndexCoordinator:
    """Provide one current index per workspace to runtime consumers."""

    def __init__(
        self,
        store: ProjectIndexStore | None,
        builder: ProjectIndexBuilder | None = None,
    ) -> None:
        self._store = store
        self._builder = builder or ProjectIndexBuilder()
        self._cache: dict[str, ProjectIndex] = {}
        self._stale: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    async def current(
        self,
        root: str,
        *,
        refresh: bool = False,
        freshness: str = "cached",
    ) -> ProjectIndex:
        """Return an index at the requested freshness level.

        Ordinary callers use the cheap in-memory cache. Completion planning
        uses ``source_verified`` so an external editor cannot leave a stale
        dependency graph in the acceptance-proof path.
        """
        if freshness not in {"cached", "source_verified"}:
            raise ValueError("freshness must be cached or source_verified")
        canonical = _canonical_root(root)
        cached = self._cache.get(canonical)
        if (
            cached is not None
            and not refresh
            and canonical not in self._stale
            and (
                freshness == "cached"
                or (
                    bool(cached.source_revision)
                    and await self._matches_source_revision(canonical, cached.source_revision)
                )
            )
        ):
            return cached
        lock = self._locks.setdefault(canonical, asyncio.Lock())
        async with lock:
            cached = self._cache.get(canonical)
            if (
                cached is not None
                and not refresh
                and canonical not in self._stale
                and (
                    freshness == "cached"
                    or (
                        bool(cached.source_revision)
                        and await self._matches_source_revision(canonical, cached.source_revision)
                    )
                )
            ):
                return cached
            if not refresh and canonical not in self._stale and self._store is not None:
                persisted = await self._store.get(canonical)
                if (
                    persisted is not None
                    and persisted.source_revision
                    and await self._matches_source_revision(
                        canonical,
                        persisted.source_revision,
                    )
                ):
                    self._cache[canonical] = persisted
                    return persisted
            loop = asyncio.get_running_loop()
            index = await loop.run_in_executor(None, self._builder.build, canonical)
            if self._store is not None:
                await self._store.save(index)
            self._cache[canonical] = index
            self._stale.discard(canonical)
            return index

    async def _matches_source_revision(
        self,
        root: str,
        expected: str,
    ) -> bool:
        revision = getattr(self._builder, "source_revision", None)
        if revision is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            current = await loop.run_in_executor(None, revision, root)
        except (OSError, RuntimeError, ValueError):
            return False
        return str(current) == expected

    async def refresh(self, root: str) -> ProjectIndex:
        return await self.current(root, refresh=True)

    def mark_stale(self, root: str) -> None:
        self._stale.add(_canonical_root(root))

    def mark_stale_for_paths(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Invalidate cached roots containing one of the changed resources."""
        changed = tuple(_canonical_root(path) for path in paths if path)
        invalidated: list[str] = []
        for root in tuple(self._cache):
            if any(_inside(root, path) for path in changed):
                self._stale.add(root)
                invalidated.append(root)
        return tuple(sorted(invalidated))

    def status(self, root: str) -> dict[str, Any]:
        canonical = _canonical_root(root)
        index = self._cache.get(canonical)
        return {
            "root": canonical,
            "cached": index is not None,
            "stale": canonical in self._stale,
            "index_revision": index.index_revision if index is not None else None,
            "complete": index.complete if index is not None else None,
        }


def _canonical_root(root: str) -> str:
    return os.path.realpath(os.path.abspath(root))


def _inside(root: str, path: str) -> bool:
    try:
        return path == root or os.path.commonpath((root, path)) == root
    except ValueError:
        return False


__all__ = ["ProjectIndexCoordinator"]
