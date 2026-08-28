"""Data model for a bounded, restartable project index."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ProjectIndex:
    """Lexical project graph plus the profile/environment at its revision."""

    root: str
    profile: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, Any] = field(default_factory=dict)
    files: tuple[Mapping[str, Any], ...] = ()
    imports: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    symbols: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    generated_files: tuple[str, ...] = ()
    test_associations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    configs: tuple[str, ...] = ()
    dependency_edges: tuple[Mapping[str, str], ...] = ()
    semantic: Mapping[str, Any] = field(default_factory=dict)
    complete: bool = True
    truncated: bool = False
    truncation_reason: str | None = None
    index_revision: str = ""
    source_revision: str = ""
    built_at: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "profile": dict(self.profile),
            "environment": dict(self.environment),
            "files": [dict(item) for item in self.files],
            "imports": {key: list(value) for key, value in self.imports.items()},
            "symbols": {key: list(value) for key, value in self.symbols.items()},
            "references": {key: list(value) for key, value in self.references.items()},
            "generated_files": list(self.generated_files),
            "test_associations": {
                key: list(value) for key, value in self.test_associations.items()
            },
            "configs": list(self.configs),
            "dependency_edges": [dict(item) for item in self.dependency_edges],
            "semantic": dict(self.semantic),
            "complete": self.complete,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "index_revision": self.index_revision,
            "source_revision": self.source_revision,
            "built_at": self.built_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ProjectIndex":
        return cls(
            root=str(record.get("root") or ""),
            profile=dict(record.get("profile") or {}),
            environment=dict(record.get("environment") or {}),
            files=tuple(dict(item) for item in record.get("files") or ()),
            imports={
                str(key): tuple(str(value) for value in values or ())
                for key, values in (record.get("imports") or {}).items()
            },
            symbols={
                str(key): tuple(str(value) for value in values or ())
                for key, values in (record.get("symbols") or {}).items()
            },
            references={
                str(key): tuple(str(value) for value in values or ())
                for key, values in (record.get("references") or {}).items()
            },
            generated_files=tuple(str(value) for value in record.get("generated_files") or ()),
            test_associations={
                str(key): tuple(str(value) for value in values or ())
                for key, values in (record.get("test_associations") or {}).items()
            },
            configs=tuple(str(value) for value in record.get("configs") or ()),
            dependency_edges=tuple(dict(item) for item in record.get("dependency_edges") or ()),
            semantic=dict(record.get("semantic") or {}),
            complete=bool(record.get("complete", not record.get("truncated", False))),
            truncated=bool(record.get("truncated", False)),
            truncation_reason=(
                str(record["truncation_reason"])
                if record.get("truncation_reason") is not None
                else None
            ),
            index_revision=str(record.get("index_revision") or ""),
            source_revision=str(record.get("source_revision") or ""),
            built_at=str(record.get("built_at") or ""),
        )

    def impact(self, changed_paths: list[str]) -> dict[str, Any]:
        """Return direct/transitive dependents from this immutable revision."""
        known = {str(item.get("path")) for item in self.files}
        changed = sorted({str(path).replace("\\", "/") for path in changed_paths})
        changed_known = [path for path in changed if path in known]
        reverse: dict[str, set[str]] = {}
        for edge in self.dependency_edges:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source and target:
                reverse.setdefault(target, set()).add(source)

        direct: set[str] = set()
        queue: deque[str] = deque()
        for path in changed_known:
            for dependent in sorted(reverse.get(path, ())):
                direct.add(dependent)
                queue.append(dependent)
        transitive: set[str] = set(direct)
        while queue:
            current = queue.popleft()
            for dependent in sorted(reverse.get(current, ())):
                if dependent not in transitive:
                    transitive.add(dependent)
                    queue.append(dependent)

        associated_tests = {
            test
            for source in changed_known + sorted(transitive)
            for test in self.test_associations.get(source, ())
        }
        affected = sorted(transitive | associated_tests)
        file_count = max(1, len(known))
        confidence = "high" if direct else "medium" if changed_known else "low"
        if self.truncated and confidence == "high":
            confidence = "medium"
        critical = sorted(
            path
            for path in affected
            if any(
                part in {"main.py", "__main__.py", "main.go", "main.rs", "app.py"}
                for part in (path.rsplit("/", 1)[-1],)
            )
        )
        impacted = [
            {
                "path": path,
                "direct": path in direct,
                "transitive": path in transitive and path not in direct,
                "test": path in associated_tests or _is_test(path),
                "confidence": "high" if path in direct else "medium",
            }
            for path in affected
        ]
        return {
            # Legacy keys remain available to existing callers.
            "changed": changed,
            "impacted": impacted,
            "method": "persistent lexical dependency index",
            "changed_resources": changed,
            "direct_dependents": sorted(direct),
            "transitive_dependents": sorted(transitive - direct),
            "affected_tests": sorted(
                path for path in affected if path in associated_tests or _is_test(path)
            ),
            "blast_radius": round(len(affected) / file_count, 6),
            "confidence": confidence,
            "critical_paths": critical,
            "index_revision": self.index_revision,
            "complete": self.complete,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
        }


def _is_test(path: str) -> bool:
    parts = set(path.split("/"))
    name = path.rsplit("/", 1)[-1].casefold()
    return bool(parts & {"test", "tests", "spec", "specs", "__tests__"}) or name.startswith(
        ("test_", "test.")
    )


__all__ = ["ProjectIndex"]
