"""Durable receipts for governed external-effect transactions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.capabilities import ExternalEffectPhase
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow


class ExternalEffectRecoveryRequired(RuntimeError):
    """The outcome of an external request is not safe to replay."""


class ExternalEffectStore:
    """Persist one receipt per external transaction identity.

    A transaction is write-ahead before ``apply``. If the process dies while
    an external request is in flight, a retry returns recovery-required rather
    than issuing a second request with unknown remote outcome.
    """

    def __init__(self, db=None) -> None:
        self._db = db
        self._ready = False
        self._lock = asyncio.Lock()
        self._memory: dict[str, dict[str, Any]] = {}

    async def _ensure(self) -> None:
        # The migration runner owns the durable schema.  Keeping DDL here
        # created two authorities that could silently drift across upgrades.
        self._ready = True

    async def get(self, transaction_id: str) -> dict[str, Any] | None:
        async with self._lock:
            await self._ensure()
            return await self._get_unlocked(transaction_id)

    async def prepare(
        self,
        *,
        transaction_id: str,
        task_id: str | None,
        capability_id: str,
        external_identity: str,
        request_digest: str,
        idempotency_key: str | None,
        phase: ExternalEffectPhase,
    ) -> dict[str, Any]:
        if phase not in {ExternalEffectPhase.PREPARE, ExternalEffectPhase.DRY_RUN}:
            raise ValueError("external preparation requires prepare or dry_run phase")
        async with self._lock:
            await self._ensure()
            existing = await self._get_unlocked(transaction_id)
            if existing is not None:
                self._assert_same(
                    existing,
                    task_id,
                    capability_id,
                    external_identity,
                    request_digest,
                    idempotency_key,
                )
                return existing
            if idempotency_key is not None:
                bound = await self._get_by_idempotency_unlocked(
                    capability_id,
                    external_identity,
                    idempotency_key,
                )
                if bound is not None:
                    raise ValueError(
                        "idempotency_key is already bound to another external transaction"
                    )
            now = utcnow().isoformat()
            receipt: dict[str, Any] = {
                "receipt_id": new_id("external-receipt"),
                "transaction_id": transaction_id,
                "task_id": task_id,
                "capability_id": capability_id,
                "phase": phase.value,
                "status": "PREPARED" if phase is ExternalEffectPhase.PREPARE else "DRY_RUN",
                "external_identity": external_identity,
                "request_digest": request_digest,
                "idempotency_key": idempotency_key,
                "response": {},
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            await self._save_unlocked(receipt)
            return receipt

    async def begin_apply(
        self,
        *,
        transaction_id: str,
        task_id: str | None,
        capability_id: str,
        external_identity: str,
        request_digest: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        if not idempotency_key:
            raise ValueError("external apply requires an idempotency_key")
        async with self._lock:
            await self._ensure()
            receipt = await self._get_unlocked(transaction_id)
            if receipt is None:
                raise ValueError("external apply requires a prepared transaction")
            self._assert_same(
                receipt, task_id, capability_id, external_identity, request_digest, idempotency_key
            )
            status = str(receipt.get("status") or "")
            if status == "COMPLETED":
                return receipt, True
            if status in {"APPLYING", "RECOVERY_REQUIRED"}:
                # APPLYING is a write-ahead marker, not a replay grant.  The
                # process may have died after the remote side effect was
                # issued and before its receipt became terminal.  Reissuing
                # here would turn an unknown outcome into a duplicate side
                # effect.  Recovery must be an explicit operator/verifier
                # action instead.
                raise ExternalEffectRecoveryRequired(
                    "external transaction outcome is unknown; recovery is required"
                )
            if status not in {"PREPARED", "DRY_RUN"}:
                raise ExternalEffectRecoveryRequired(
                    f"external transaction is not applicable from {status or 'unknown'}"
                )
            receipt = {
                **receipt,
                "phase": ExternalEffectPhase.APPLY.value,
                "status": "APPLYING",
                "error": None,
                "updated_at": utcnow().isoformat(),
            }
            await self._save_unlocked(receipt)
            return receipt, False

    async def finish(
        self,
        transaction_id: str,
        *,
        status: str,
        response: Mapping[str, Any] | None = None,
        error: str | None = None,
        phase: ExternalEffectPhase | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._ensure()
            receipt = await self._get_unlocked(transaction_id)
            if receipt is None:
                raise KeyError(f"unknown external transaction: {transaction_id}")
            previous_status = str(receipt.get("status") or "")
            response_value = dict(response or receipt.get("response") or {})
            updated = {
                **receipt,
                "status": status,
                "response": response_value,
                "error": error,
                "updated_at": utcnow().isoformat(),
            }
            if phase is not None:
                updated["phase"] = phase.value
            if status == "RECOVERY_REQUIRED":
                origin_phase = str(
                    receipt.get("recovery_origin_phase")
                    or updated.get("phase")
                    or response_value.get("phase")
                    or ""
                )
                origin_status = str(receipt.get("recovery_origin_status") or previous_status or "")
                updated["recovery_origin_phase"] = origin_phase
                updated["recovery_origin_status"] = origin_status
                updated["verification_target"] = str(
                    receipt.get("verification_target")
                    or (
                        "COMPENSATION_PRESTATE"
                        if origin_phase == ExternalEffectPhase.COMPENSATE.value
                        or origin_status.startswith("COMPENSAT")
                        else "APPLY_POSTSTATE"
                    )
                )
            await self._save_unlocked(updated)
            return updated

    async def reconcile_startup(self) -> list[dict[str, Any]]:
        """Fail closed for external work interrupted by process shutdown.

        External systems are outside Athena's transaction boundary.  A
        receipt left in an in-flight phase cannot be safely replayed merely
        because Athena restarted, so startup converts it to an explicit
        recovery state and returns redacted receipt metadata for evidence.
        PREPARED and DRY_RUN remain actionable because no external apply was
        authorized from those states.
        """
        in_flight = {"APPLYING", "VERIFYING", "COMPENSATING"}
        reconciled: list[dict[str, Any]] = []
        async with self._lock:
            await self._ensure()
            records = await self._list_unlocked()
            for record in records:
                previous_status = str(record.get("status") or "")
                if previous_status not in in_flight:
                    continue
                reason = (
                    "external transaction was interrupted while "
                    f"{previous_status.lower()}; remote outcome is unknown"
                )
                updated = {
                    **record,
                    "status": "RECOVERY_REQUIRED",
                    "recovery_origin_phase": str(record.get("phase") or ""),
                    "recovery_origin_status": previous_status,
                    "verification_target": (
                        "COMPENSATION_PRESTATE"
                        if previous_status == "COMPENSATING"
                        or str(record.get("phase") or "") == ExternalEffectPhase.COMPENSATE.value
                        else "APPLY_POSTSTATE"
                    ),
                    "error": reason,
                    "response": {
                        **dict(record.get("response") or {}),
                        "recovery": {
                            "transaction_id": record.get("transaction_id"),
                            "task_id": record.get("task_id"),
                            "capability_id": record.get("capability_id"),
                            "external_identity": record.get("external_identity"),
                            "phase": record.get("phase"),
                            "previous_status": previous_status,
                            "idempotency_key_present": bool(record.get("idempotency_key")),
                            "idempotency_key_fingerprint": (
                                hashlib.sha256(
                                    str(record.get("idempotency_key")).encode()
                                ).hexdigest()
                                if record.get("idempotency_key")
                                else None
                            ),
                            "reason": reason,
                        },
                        "previous_status": previous_status,
                    },
                    "updated_at": utcnow().isoformat(),
                }
                await self._save_unlocked(updated)
                reconciled.append(updated)
        return reconciled

    async def begin_followup(
        self,
        transaction_id: str,
        *,
        task_id: str | None,
        capability_id: str,
        request_digest: str | None,
        phase: ExternalEffectPhase,
    ) -> dict[str, Any]:
        if phase not in {ExternalEffectPhase.VERIFY, ExternalEffectPhase.COMPENSATE}:
            raise ValueError("unsupported external follow-up phase")
        async with self._lock:
            await self._ensure()
            receipt = await self._get_unlocked(transaction_id)
            if receipt is None:
                raise ValueError("external follow-up requires a prepared transaction")
            if receipt.get("task_id") != task_id or receipt.get("capability_id") != capability_id:
                raise ValueError("external transaction ownership does not match")
            if (
                phase is ExternalEffectPhase.VERIFY
                and receipt.get("request_digest") != request_digest
            ):
                raise ValueError("external transaction request does not match")
            status = str(receipt.get("status") or "")
            if phase is ExternalEffectPhase.VERIFY and status in {
                "VERIFIED",
                "COMPENSATION_VERIFIED",
            }:
                return receipt
            if phase is ExternalEffectPhase.COMPENSATE and status in {
                "COMPENSATED",
                "COMPENSATION_SENT",
            }:
                return receipt
            verify_recovery_states = {
                "RECOVERY_REQUIRED",
                "APPLY_FAILED",
                "APPLY_REJECTED",
                "VERIFY_FAILED",
                "COMPENSATION_SENT",
                "COMPENSATION_FAILED",
                "COMPENSATION_REJECTED",
                "COMPENSATION_VERIFY_FAILED",
            }
            allowed = {
                "COMPLETED",
                "VERIFIED",
                "VERIFY_FAILED",
                "COMPENSATION_VERIFY_FAILED",
            }
            if phase is ExternalEffectPhase.VERIFY:
                allowed |= verify_recovery_states
            if status not in allowed:
                raise ExternalEffectRecoveryRequired(
                    f"external transaction cannot enter {phase.value} from {status or 'unknown'}"
                )
            updated = {
                **receipt,
                "phase": phase.value,
                "previous_status": status,
                "status": phase.name + "ING",
                "error": None,
                "updated_at": utcnow().isoformat(),
            }
            if phase is ExternalEffectPhase.VERIFY:
                target = str(receipt.get("verification_target") or "")
                if not target:
                    target = (
                        "COMPENSATION_PRESTATE"
                        if status
                        in {
                            "COMPENSATION_SENT",
                            "COMPENSATION_VERIFY_FAILED",
                        }
                        or str(receipt.get("recovery_origin_phase") or "")
                        == ExternalEffectPhase.COMPENSATE.value
                        else "APPLY_POSTSTATE"
                    )
                updated["verification_target"] = target
            await self._save_unlocked(updated)
            return updated

    async def _get_unlocked(self, transaction_id: str) -> dict[str, Any] | None:
        if self._db is None:
            value = self._memory.get(transaction_id)
            return dict(value) if value is not None else None
        row = await self._db.fetch_one(
            "SELECT * FROM external_effect_receipts WHERE transaction_id = ?",
            (transaction_id,),
        )
        return _decode(row) if row is not None else None

    async def _get_by_idempotency_unlocked(
        self,
        capability_id: str,
        external_identity: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        if self._db is None:
            for value in self._memory.values():
                if (
                    value.get("capability_id") == capability_id
                    and value.get("external_identity") == external_identity
                    and value.get("idempotency_key") == idempotency_key
                ):
                    return dict(value)
            return None
        row = await self._db.fetch_one(
            "SELECT * FROM external_effect_receipts "
            "WHERE capability_id = ? AND external_identity = ? "
            "AND idempotency_key = ?",
            (capability_id, external_identity, idempotency_key),
        )
        return _decode(row) if row is not None else None

    async def _list_unlocked(self) -> list[dict[str, Any]]:
        if self._db is None:
            return [dict(value) for value in self._memory.values()]
        rows = await self._db.fetch_all(
            "SELECT * FROM external_effect_receipts ORDER BY created_at, transaction_id"
        )
        return [_decode(row) for row in rows]

    async def _save_unlocked(self, value: Mapping[str, Any]) -> None:
        record = dict(value)
        if self._db is None:
            self._memory[str(record["transaction_id"])] = record
            return
        await self._db.execute(
            "INSERT INTO external_effect_receipts("
            "transaction_id, receipt_id, task_id, capability_id, phase, status, "
            "external_identity, request_digest, idempotency_key, response, error, "
            "created_at, updated_at, recovery_origin_phase, recovery_origin_status, "
            "verification_target) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(transaction_id) DO UPDATE SET receipt_id=excluded.receipt_id, "
            "task_id=excluded.task_id, capability_id=excluded.capability_id, "
            "phase=excluded.phase, status=excluded.status, "
            "external_identity=excluded.external_identity, request_digest=excluded.request_digest, "
            "idempotency_key=excluded.idempotency_key, response=excluded.response, "
            "error=excluded.error, created_at=excluded.created_at, updated_at=excluded.updated_at, "
            "recovery_origin_phase=excluded.recovery_origin_phase, "
            "recovery_origin_status=excluded.recovery_origin_status, "
            "verification_target=excluded.verification_target",
            (
                record["transaction_id"],
                record["receipt_id"],
                record.get("task_id"),
                record["capability_id"],
                record["phase"],
                record["status"],
                record["external_identity"],
                record["request_digest"],
                record.get("idempotency_key"),
                json.dumps(record.get("response") or {}),
                record.get("error"),
                record["created_at"],
                record["updated_at"],
                record.get("recovery_origin_phase"),
                record.get("recovery_origin_status"),
                record.get("verification_target"),
            ),
        )

    @staticmethod
    def _assert_same(
        receipt: Mapping[str, Any],
        task_id: str | None,
        capability_id: str,
        external_identity: str,
        request_digest: str,
        idempotency_key: str | None,
    ) -> None:
        if (
            receipt.get("task_id") != task_id
            or receipt.get("capability_id") != capability_id
            or receipt.get("external_identity") != external_identity
            or receipt.get("request_digest") != request_digest
            or receipt.get("idempotency_key") != idempotency_key
        ):
            raise ValueError("external transaction identity does not match")


def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    try:
        result["response"] = json.loads(result.get("response") or "{}")
    except (TypeError, ValueError):
        result["response"] = {}
    return result


__all__ = ["ExternalEffectRecoveryRequired", "ExternalEffectStore"]
