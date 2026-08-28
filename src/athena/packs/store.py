"""Durable installed-pack registry."""

from __future__ import annotations

import json
from typing import Any, List, Mapping

from athena.packs.models import PackManifest, PackState
from athena.state.database import Database


class PackStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, state: PackState) -> None:
        await self._db.execute(
            "INSERT INTO capability_packs (id, version, manifest, install_path, "
            "enabled, source_integrity, installed_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version, "
            "manifest=excluded.manifest, install_path=excluded.install_path, "
            "enabled=excluded.enabled, source_integrity=excluded.source_integrity, "
            "installed_at=excluded.installed_at",
            (
                state.id,
                state.manifest.version,
                json.dumps(
                    state.manifest.to_record(computed_integrity=state.source_integrity),
                    sort_keys=True,
                ),
                state.install_path,
                1 if state.enabled else 0,
                state.source_integrity,
                state.installed_at,
            ),
        )

    async def get(self, pack_id: str) -> PackState | None:
        row = await self._db.fetch_one("SELECT * FROM capability_packs WHERE id = ?", (pack_id,))
        return _from_row(row) if row else None

    async def list(self) -> list[PackState]:
        rows = await self._db.fetch_all("SELECT * FROM capability_packs ORDER BY id")
        return [_from_row(row) for row in rows]

    async def set_enabled(self, pack_id: str, enabled: bool) -> PackState | None:
        state = await self.get(pack_id)
        if state is None:
            return None
        updated = PackState(
            manifest=state.manifest,
            install_path=state.install_path,
            enabled=bool(enabled),
            installed_at=state.installed_at,
            source_integrity=state.source_integrity,
            health=state.health,
        )
        await self.save(updated)
        return updated

    async def delete(self, pack_id: str) -> bool:
        state = await self.get(pack_id)
        if state is None:
            return False
        await self._db.execute("DELETE FROM capability_packs WHERE id = ?", (pack_id,))
        return True

    async def save_contribution(
        self,
        pack_id: str,
        kind: str,
        contribution_id: str,
    ) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO capability_pack_contributions"
            "(pack_id, kind, contribution_id) VALUES (?, ?, ?)",
            (pack_id, kind, contribution_id),
        )

    async def contributions(self, pack_id: str) -> List[dict[str, str]]:
        rows = await self._db.fetch_all(
            "SELECT kind, contribution_id FROM capability_pack_contributions"
            " WHERE pack_id = ? ORDER BY kind, contribution_id",
            (pack_id,),
        )
        return [
            {"kind": str(row["kind"]), "contribution_id": str(row["contribution_id"])}
            for row in rows
        ]

    async def delete_contributions(self, pack_id: str) -> None:
        await self._db.execute(
            "DELETE FROM capability_pack_contributions WHERE pack_id = ?",
            (pack_id,),
        )


def _from_row(row: Mapping[str, Any]) -> PackState:
    manifest_data = json.loads(str(row.get("manifest") or "{}"))
    provides = {
        str(key): tuple(str(item) for item in value or ())
        for key, value in (manifest_data.get("provides") or {}).items()
    }
    manifest = PackManifest(
        id=str(manifest_data.get("id") or row["id"]),
        version=str(manifest_data.get("version") or row.get("version") or ""),
        publisher=str(manifest_data.get("publisher") or ""),
        minimum_athena=manifest_data.get("minimum_athena"),
        provides=provides,
        requested_effects=tuple(str(v) for v in manifest_data.get("requested_effects") or ()),
        declared_integrity=manifest_data.get("declared_integrity"),
        metadata=dict(manifest_data.get("metadata") or {}),
    )
    return PackState(
        manifest=manifest,
        install_path=str(row.get("install_path") or ""),
        enabled=bool(row.get("enabled")),
        installed_at=str(row.get("installed_at") or ""),
        source_integrity=str(row.get("source_integrity") or ""),
        health="unknown",
    )


__all__ = ["PackStore"]
