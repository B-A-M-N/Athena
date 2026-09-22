"""Bounded in-memory cache for project indexes."""

from __future__ import annotations

from collections import OrderedDict

from athena.project.index.models import ProjectIndex


class ProjectIndexCache(OrderedDict[str, ProjectIndex]):
    """Keep a bounded LRU window while durable storage remains authoritative."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = max(1, int(limit))

    def store(self, root: str, index: ProjectIndex) -> str | None:
        self[root] = index
        self.move_to_end(root)
        evicted: str | None = None
        while len(self) > self._limit:
            evicted, _ = self.popitem(last=False)
        return evicted

    def release(self, root: str) -> None:
        self.pop(root, None)


__all__ = ["ProjectIndexCache"]
