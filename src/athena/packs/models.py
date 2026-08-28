"""Pack manifest and installed-state value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class PackManifest:
    id: str
    version: str
    publisher: str
    minimum_athena: str | None
    provides: Mapping[str, tuple[str, ...]]
    requested_effects: tuple[str, ...]
    declared_integrity: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self, *, computed_integrity: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "publisher": self.publisher,
            "minimum_athena": self.minimum_athena,
            "provides": {key: list(value) for key, value in self.provides.items()},
            "requested_effects": list(self.requested_effects),
            "declared_integrity": self.declared_integrity,
            "computed_integrity": computed_integrity,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PackState:
    manifest: PackManifest
    install_path: str
    enabled: bool
    installed_at: str
    source_integrity: str
    health: str = "unknown"

    @property
    def id(self) -> str:
        return self.manifest.id

    def to_record(self) -> dict[str, Any]:
        return {
            **self.manifest.to_record(computed_integrity=self.source_integrity),
            "install_path": self.install_path,
            "enabled": self.enabled,
            "installed_at": self.installed_at,
            "health": self.health,
        }


__all__ = ["PackManifest", "PackState"]
