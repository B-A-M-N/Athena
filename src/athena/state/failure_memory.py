"""Durable, advisory memory for recurring diagnostic failures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class FailureMemory:
    """Remember repair evidence without turning memory into authority.

    Records are scoped by diagnostic signature, environment, and project.
    Retrieval is deterministic and exact-scope records win over portable
    records. Callers still have to validate and authorize any remediation.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        signature_fingerprint: str,
        capability_id: str,
        environment_fingerprint: str | None = None,
        project_scope: str | None = None,
        strategy: Mapping[str, Any] | str = "",
        remediation: Mapping[str, Any] | str | None = None,
        evidence_ids: tuple[str, ...] | list[str] = (),
        success: bool = False,
        expires_at: str | None = None,
    ) -> str:
        signature = _bounded(signature_fingerprint, 128, "signature_fingerprint")
        capability = _bounded(capability_id, 128, "capability_id")
        environment = _bounded(environment_fingerprint or "", 256, "environment")
        project = _bounded(project_scope or "", 256, "project_scope")
        strategy_json = _json(strategy)
        remediation_json = _json(remediation) if remediation is not None else None
        evidence_json = json.dumps(
            sorted({_bounded(value, 256, "evidence_id") for value in evidence_ids}),
            sort_keys=True,
        )
        now = utcnow().isoformat()
        existing = await self._db.fetch_one(
            "SELECT id, success_count, failure_count FROM failure_memory "
            "WHERE signature_fingerprint = ? AND capability_id = ? "
            "AND environment_fingerprint = ? AND project_scope = ? AND strategy = ?",
            (signature, capability, environment, project, strategy_json),
        )
        if existing is None:
            record_id = new_id("failure")
            await self._db.execute(
                "INSERT INTO failure_memory ("
                "id, signature_fingerprint, capability_id, environment_fingerprint, "
                "project_scope, strategy, remediation, evidence_ids, success_count, "
                "failure_count, last_success, last_failure, created_at, updated_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    signature,
                    capability,
                    environment,
                    project,
                    strategy_json,
                    remediation_json,
                    evidence_json,
                    1 if success else 0,
                    0 if success else 1,
                    now if success else None,
                    None if success else now,
                    now,
                    now,
                    expires_at,
                ),
            )
            return record_id
        record_id = str(existing["id"])
        await self._db.execute(
            "UPDATE failure_memory SET success_count = success_count + ?, "
            "failure_count = failure_count + ?, last_success = ?, last_failure = ?, "
            "remediation = COALESCE(?, remediation), evidence_ids = ?, "
            "updated_at = ?, expires_at = COALESCE(?, expires_at) WHERE id = ?",
            (
                1 if success else 0,
                0 if success else 1,
                now if success else None,
                None if success else now,
                remediation_json,
                evidence_json,
                now,
                expires_at,
                record_id,
            ),
        )
        return record_id

    async def retrieve(
        self,
        *,
        signature_fingerprint: str | None = None,
        capability_id: str | None = None,
        environment_fingerprint: str | None = None,
        project_scope: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return currently usable suggestions in stable relevance order."""
        clauses = ["(expires_at IS NULL OR expires_at > ?)"]
        params: list[Any] = [utcnow().isoformat()]
        if signature_fingerprint:
            clauses.append("signature_fingerprint = ?")
            params.append(signature_fingerprint)
        if capability_id:
            clauses.append("capability_id = ?")
            params.append(capability_id)
        rows = await self._db.fetch_all(
            "SELECT * FROM failure_memory WHERE " + " AND ".join(clauses),
            tuple(params),
        )
        environment = environment_fingerprint or ""
        project = project_scope or ""
        rows.sort(
            key=lambda row: (
                0 if row.get("project_scope") == project and project else 1,
                0 if row.get("environment_fingerprint") == environment and environment else 1,
                0 if int(row.get("success_count") or 0) > 0 else 1,
                -int(row.get("success_count") or 0),
                -int(row.get("failure_count") or 0),
                str(row.get("updated_at") or ""),
                str(row.get("id") or ""),
            )
        )
        return [_decode(row) for row in rows[: max(1, min(limit, 100))]]

    async def expire(self, *, now: str | None = None) -> int:
        cursor = await self._db.execute(
            "DELETE FROM failure_memory WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now or utcnow().isoformat(),),
        )
        return int(cursor.rowcount or 0)


def _bounded(value: str, maximum: int, field: str) -> str:
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _json(value: Mapping[str, Any] | str) -> str:
    if isinstance(value, str):
        return json.dumps({"text": value}, sort_keys=True)
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("strategy", "remediation", "evidence_ids"):
        value = row.get(key)
        if value is None:
            continue
        try:
            row[key] = json.loads(value)
        except (TypeError, ValueError):
            pass
    return row


__all__ = ["FailureMemory"]
