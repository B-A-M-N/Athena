"""Durable provider request/response receipts.

The provider boundary is inherently not part of SQLite's transaction.  This
store closes the useful crash window around it: request identity is recorded
before a provider call, and the normalized response is committed before the
kernel can compile the next prompt.  A restart can therefore replay a
completed response without calling the provider again.
"""

from __future__ import annotations

import json
import inspect
from dataclasses import asdict
from decimal import Decimal
from typing import Any, Callable, Mapping

from athena.protocol.models import ModelResponse, UsageInfo
from athena.protocol.messages import utcnow
from athena.protocol.ids import new_id
from athena.state.database import Database
from athena.state.model_response_reconciliation import (
    archive_attempt,
    ensure_attempt_row,
    reject_unresolved_legacy_receipts,
    validated_actual_cost,
)
from athena.state.sessions import _deserialize_block, _serialize_block


class ModelResponseStore:
    """SQLite-backed request journal and normalized response receipts."""

    def __init__(
        self,
        db: Database,
        *,
        fault_injector: Callable[[str], Any] | None = None,
    ) -> None:
        self._db = db
        self._fault_injector = fault_injector

    def set_fault_injector(self, injector: Callable[[str], Any] | None) -> None:
        """Install a test-only fault hook for paired projection writes."""
        self._fault_injector = injector

    async def _fault_point(self, name: str) -> None:
        if self._fault_injector is None:
            return
        result = self._fault_injector(name)
        if inspect.isawaitable(result):
            await result

    async def prepare(
        self,
        *,
        task_id: str,
        request_fingerprint: str,
        request_id: str,
        provider: str,
        model: str,
        reservation_amount: Decimal | str | None = None,
        idempotency_semantics: str = "none",
    ) -> dict[str, Any]:
        """Create or recover one task-scoped provider request identity."""
        async with self._db.transaction() as db:
            existing = await db.fetch_one_raw(
                "SELECT * FROM model_response_receipts "
                "WHERE task_id = ? AND request_fingerprint = ?",
                (task_id, request_fingerprint),
            )
            if existing is None:
                await reject_unresolved_legacy_receipts(
                    db,
                    task_id=task_id,
                    provider=provider,
                    model=model,
                    request_fingerprint=request_fingerprint,
                )
                attempt_id = new_id("inference")
                created_at = utcnow().isoformat()
                await db.execute_raw(
                    "INSERT OR IGNORE INTO model_response_receipts("
                    "task_id, request_fingerprint, request_id, provider, model, status, created_at, "
                    "attempt_id, reservation_amount, idempotency_key, provider_outcome_status"
                    ") VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, 'pending')",
                    (
                        task_id,
                        request_fingerprint,
                        request_id,
                        provider,
                        model,
                        created_at,
                        attempt_id,
                        str(reservation_amount) if reservation_amount is not None else None,
                        attempt_id,
                    ),
                )
                existing = await db.fetch_one_raw(
                    "SELECT * FROM model_response_receipts "
                    "WHERE task_id = ? AND request_fingerprint = ?",
                    (task_id, request_fingerprint),
                )
            if existing is None:
                raise RuntimeError("model response receipt could not be prepared")
            if (
                str(existing.get("provider") or "") != provider
                or str(existing.get("model") or "") != model
            ):
                raise ValueError("model response receipt identity changed for the same request")
            status = str(existing.get("status") or "PENDING")
            if status == "FAILED":
                current_attempt = await db.fetch_one_raw(
                    "SELECT status, provider_outcome_status FROM model_response_attempts "
                    "WHERE attempt_id = ?",
                    (existing.get("attempt_id"),),
                )
                current_attempt_status = str((current_attempt or {}).get("status") or "")
                current_outcome = str(
                    (current_attempt or {}).get("provider_outcome_status") or ""
                ).lower()
                if (
                    current_attempt_status == "ABANDONED"
                    or current_outcome == "confirmed_succeeded"
                ):
                    return dict(existing)
                if current_attempt_status == "UNKNOWN" and current_outcome != "retry_authorized":
                    return dict(existing)
                if current_attempt_status not in {"UNKNOWN", "ABANDONED"}:
                    await archive_attempt(db, existing)
                attempt_id = new_id("inference")
                created_at = utcnow().isoformat()
                await db.execute_raw(
                    "UPDATE model_response_receipts SET request_id = ?, status = 'PENDING', "
                    "response = NULL, completed_at = NULL, attempt_id = ?, "
                    "reservation_amount = ?, reservation_applied_at = NULL, "
                    "actual_input_tokens = NULL, actual_output_tokens = NULL, actual_cost = NULL, "
                    "provider_usage_id = NULL, response_committed_at = NULL, "
                    "accounting_applied_at = NULL, assistant_appended_at = NULL, "
                    "reservation_released_at = NULL, budget_accounted_at = NULL, "
                    "provider_usage_started_at = NULL, provider_usage_completed_at = NULL, "
                    "provider_outcome_status = 'pending', provider_response_id = NULL, "
                    "idempotency_key = ?, provider_outcome_unknown_at = NULL WHERE task_id = ? "
                    "AND request_fingerprint = ?",
                    (
                        request_id,
                        attempt_id,
                        str(reservation_amount) if reservation_amount is not None else None,
                        attempt_id,
                        task_id,
                        request_fingerprint,
                    ),
                )
                await db.execute_raw(
                    "INSERT INTO model_response_attempts("
                    "attempt_id, task_id, request_fingerprint, request_id, provider, model, status, "
                    "reservation_amount, idempotency_key, idempotency_semantics, "
                    "provider_outcome_status, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, 'pending', ?)",
                    (
                        attempt_id,
                        task_id,
                        request_fingerprint,
                        request_id,
                        provider,
                        model,
                        str(reservation_amount) if reservation_amount is not None else None,
                        attempt_id,
                        idempotency_semantics,
                        created_at,
                    ),
                )
                existing = await db.fetch_one_raw(
                    "SELECT * FROM model_response_receipts "
                    "WHERE task_id = ? AND request_fingerprint = ?",
                    (task_id, request_fingerprint),
                )
            elif not existing.get("attempt_id"):
                # Pre-accounting receipts already represented a completed provider
                # turn. Mark their legacy identity as accounted so replay cannot
                # charge them a second time.
                legacy_attempt_id = new_id("legacy-inference")
                await db.execute_raw(
                    "UPDATE model_response_receipts SET attempt_id = ?, "
                    "accounting_applied_at = COALESCE(accounting_applied_at, completed_at), "
                    "budget_accounted_at = COALESCE(budget_accounted_at, accounting_applied_at, completed_at) "
                    "WHERE task_id = ? AND request_fingerprint = ?",
                    (legacy_attempt_id, task_id, request_fingerprint),
                )
                await db.execute_raw(
                    "INSERT OR IGNORE INTO model_response_attempts("
                    "attempt_id, task_id, request_fingerprint, request_id, provider, model, status, "
                    "provider_outcome_status, idempotency_semantics, created_at, completed_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        legacy_attempt_id,
                        task_id,
                        request_fingerprint,
                        existing["request_id"],
                        provider,
                        model,
                        existing.get("status") or "COMPLETED",
                        existing.get("provider_outcome_status") or "known",
                        idempotency_semantics,
                        existing.get("created_at") or utcnow().isoformat(),
                        existing.get("completed_at"),
                    ),
                )
                existing = await db.fetch_one_raw(
                    "SELECT * FROM model_response_receipts "
                    "WHERE task_id = ? AND request_fingerprint = ?",
                    (task_id, request_fingerprint),
                )
            elif (
                status == "PENDING"
                and existing.get("reservation_amount") in (None, "")
                and reservation_amount is not None
            ):
                # Backfill legacy receipts and their current attempt together.
                await db.execute_raw(
                    "UPDATE model_response_receipts SET reservation_amount = ? "
                    "WHERE task_id = ? AND request_fingerprint = ? AND status = 'PENDING' "
                    "AND reservation_amount IS NULL",
                    (str(reservation_amount), task_id, request_fingerprint),
                )
                await db.execute_raw(
                    "UPDATE model_response_attempts SET reservation_amount = ?, "
                    "idempotency_semantics = ? WHERE attempt_id = ?",
                    (str(reservation_amount), idempotency_semantics, existing["attempt_id"]),
                )
                existing = await db.fetch_one_raw(
                    "SELECT * FROM model_response_receipts "
                    "WHERE task_id = ? AND request_fingerprint = ?",
                    (task_id, request_fingerprint),
                )
            await ensure_attempt_row(
                db,
                existing,
                idempotency_semantics=idempotency_semantics,
            )
            return dict(existing or {})

    async def mark_reservation_applied(self, *, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET reservation_applied_at = COALESCE(?, reservation_applied_at) "
                "WHERE attempt_id = ? AND reservation_applied_at IS NULL",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-reservation-applied")
            await db.execute_raw(
                "UPDATE model_response_receipts SET reservation_applied_at = COALESCE(?, reservation_applied_at) "
                "WHERE attempt_id = ? AND reservation_applied_at IS NULL",
                (timestamp, attempt_id),
            )
            return True

    async def set_provider_usage_id(self, *, attempt_id: str, provider_usage_id: str) -> None:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            await db.execute_raw(
                "UPDATE model_response_attempts SET provider_usage_id = ?, "
                "provider_usage_started_at = COALESCE(provider_usage_started_at, ?) "
                "WHERE attempt_id = ?",
                (provider_usage_id, timestamp, attempt_id),
            )
            await self._fault_point("attempt-provider-usage-id")
            await db.execute_raw(
                "UPDATE model_response_receipts SET provider_usage_id = ?, "
                "provider_usage_started_at = COALESCE(provider_usage_started_at, ?) "
                "WHERE attempt_id = ?",
                (provider_usage_id, timestamp, attempt_id),
            )

    async def set_actual_usage(
        self,
        *,
        attempt_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Decimal | str | None,
    ) -> None:
        """Checkpoint usage on the attempt authority and its atomic projection."""
        input_tokens = max(0, int(input_tokens))
        output_tokens = max(0, int(output_tokens))
        cost = str(cost_usd) if cost_usd is not None else None
        async with self._db.transaction() as db:
            await db.execute_raw(
                "UPDATE model_response_attempts SET actual_input_tokens = ?, "
                "actual_output_tokens = ?, actual_cost = COALESCE(?, actual_cost) "
                "WHERE attempt_id = ? AND status = 'COMPLETED'",
                (input_tokens, output_tokens, cost, attempt_id),
            )
            await self._fault_point("attempt-actual-usage")
            await db.execute_raw(
                "UPDATE model_response_receipts SET actual_input_tokens = ?, "
                "actual_output_tokens = ?, actual_cost = COALESCE(?, actual_cost) "
                "WHERE attempt_id = ? AND status = 'COMPLETED'",
                (input_tokens, output_tokens, cost, attempt_id),
            )

    async def complete(
        self,
        *,
        attempt_id: str,
        response: ModelResponse,
        provider_usage_id: str | None = None,
        provider_response_id: str | None = None,
    ) -> bool:
        """Persist one attempt response exactly once, addressed by attempt ID."""
        payload = json.dumps(_encode_response(response), sort_keys=True, default=str)
        committed_at = utcnow().isoformat()
        input_tokens = int(getattr(response.usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(response.usage, "output_tokens", 0) or 0)
        cost = (
            str(response.usage.cost_usd)
            if getattr(response.usage, "cost_usd", None) is not None
            else None
        )
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET status = 'COMPLETED', completed_at = ?, "
                "response_committed_at = ?, actual_input_tokens = ?, actual_output_tokens = ?, "
                "actual_cost = ?, provider_usage_id = COALESCE(?, provider_usage_id), "
                "provider_response_id = COALESCE(?, provider_response_id), "
                "provider_outcome_status = 'known' WHERE attempt_id = ? "
                "AND status IN ('PENDING', 'ACCOUNTED')",
                (
                    committed_at,
                    committed_at,
                    input_tokens,
                    output_tokens,
                    cost,
                    provider_usage_id,
                    provider_response_id,
                    attempt_id,
                ),
            )
            if cursor.rowcount == 1:
                await self._fault_point("attempt-response-complete")
                receipt_cursor = await db.execute_raw(
                    "UPDATE model_response_receipts SET status = 'COMPLETED', response = ?, "
                    "completed_at = ?, response_committed_at = ?, actual_input_tokens = ?, "
                    "actual_output_tokens = ?, actual_cost = ?, "
                    "provider_usage_id = COALESCE(?, provider_usage_id), "
                    "provider_response_id = COALESCE(?, provider_response_id), "
                    "provider_outcome_status = 'known' WHERE attempt_id = ? "
                    "AND status = 'PENDING'",
                    (
                        payload,
                        committed_at,
                        committed_at,
                        input_tokens,
                        output_tokens,
                        cost,
                        provider_usage_id,
                        provider_response_id,
                        attempt_id,
                    ),
                )
                if receipt_cursor.rowcount != 1:
                    raise RuntimeError(
                        f"model response receipt projection missing for attempt {attempt_id}"
                    )
                return True
            existing = await db.fetch_one_raw(
                "SELECT status FROM model_response_attempts WHERE attempt_id = ?",
                (attempt_id,),
            )
            if existing is not None and str(existing.get("status") or "") == "COMPLETED":
                return False
            raise ValueError("model response attempt was not prepared")

    async def mark_accounting_applied(self, *, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET accounting_applied_at = ? "
                "WHERE attempt_id = ? AND accounting_applied_at IS NULL",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-accounting-applied")
            await db.execute_raw(
                "UPDATE model_response_receipts SET accounting_applied_at = ? "
                "WHERE attempt_id = ? AND accounting_applied_at IS NULL",
                (timestamp, attempt_id),
            )
            return True

    async def mark_budget_accounted(self, *, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET budget_accounted_at = COALESCE(?, budget_accounted_at), "
                "status = CASE WHEN status = 'PENDING' THEN 'ACCOUNTED' ELSE status END "
                "WHERE attempt_id = ? AND budget_accounted_at IS NULL",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-budget-accounted")
            await db.execute_raw(
                "UPDATE model_response_receipts SET budget_accounted_at = COALESCE(?, budget_accounted_at) "
                "WHERE attempt_id = ? AND budget_accounted_at IS NULL",
                (timestamp, attempt_id),
            )
            return True

    async def mark_provider_usage_started(self, *, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET provider_usage_started_at = "
                "COALESCE(provider_usage_started_at, ?) WHERE attempt_id = ? "
                "AND provider_usage_started_at IS NULL",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-provider-usage-started")
            await db.execute_raw(
                "UPDATE model_response_receipts SET provider_usage_started_at = "
                "COALESCE(provider_usage_started_at, ?) WHERE attempt_id = ? "
                "AND provider_usage_started_at IS NULL",
                (timestamp, attempt_id),
            )
            return True

    async def mark_provider_usage_completed(self, *, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET provider_usage_completed_at = "
                "COALESCE(provider_usage_completed_at, ?) WHERE attempt_id = ? "
                "AND provider_usage_completed_at IS NULL",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-provider-usage-completed")
            await db.execute_raw(
                "UPDATE model_response_receipts SET provider_usage_completed_at = "
                "COALESCE(provider_usage_completed_at, ?) WHERE attempt_id = ? "
                "AND provider_usage_completed_at IS NULL",
                (timestamp, attempt_id),
            )
            return True

    async def mark_reservation_released(self, *, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET reservation_released_at = "
                "COALESCE(reservation_released_at, ?) WHERE attempt_id = ? "
                "AND reservation_released_at IS NULL",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-reservation-released")
            await db.execute_raw(
                "UPDATE model_response_receipts SET reservation_released_at = "
                "COALESCE(reservation_released_at, ?) WHERE attempt_id = ? "
                "AND reservation_released_at IS NULL",
                (timestamp, attempt_id),
            )
            return True

    async def list_unreleased_reservations(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM model_response_attempts WHERE task_id = ? "
            "AND status = 'FAILED' AND provider_outcome_status IN ('failed', 'confirmed_failed') "
            "AND reservation_applied_at IS NOT NULL AND reservation_released_at IS NULL",
            (task_id,),
        )
        return [dict(row) for row in rows]

    async def mark_attempt_reservation_released(self, attempt_id: str) -> bool:
        return await self.mark_reservation_released(attempt_id=attempt_id)

    async def mark_provider_outcome_unknown(self, *, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET status = 'UNKNOWN', "
                "provider_outcome_status = 'unknown', "
                "provider_outcome_unknown_at = COALESCE(provider_outcome_unknown_at, ?) "
                "WHERE attempt_id = ? AND (status IN ('PENDING', 'ACCOUNTED') "
                "OR (status = 'COMPLETED' AND accounting_applied_at IS NULL))",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-outcome-unknown")
            await db.execute_raw(
                "UPDATE model_response_receipts SET provider_outcome_status = 'unknown', "
                "provider_outcome_unknown_at = COALESCE(provider_outcome_unknown_at, ?) "
                "WHERE attempt_id = ? AND (status = 'PENDING' "
                "OR (status = 'COMPLETED' AND accounting_applied_at IS NULL))",
                (timestamp, attempt_id),
            )
            return True

    async def resolve_provider_outcome(
        self,
        *,
        attempt_id: str,
        resolution: str,
        note: str,
        provider_response_id: str | None = None,
        actual_cost: Decimal | str | None = None,
    ) -> dict[str, Any]:
        """Apply an operator/provider reconciliation decision exactly once."""
        resolution = str(resolution).strip().lower()
        allowed = {
            "confirmed_succeeded",
            "confirmed_failed",
            "retry_authorized",
            "abandoned_with_liability",
        }
        if resolution not in allowed:
            raise ValueError(f"unsupported provider outcome resolution: {resolution}")
        normalized_cost = validated_actual_cost(actual_cost)
        timestamp = utcnow().isoformat()
        terminal_status = {
            "confirmed_failed": "FAILED",
            "retry_authorized": "UNKNOWN",
            "abandoned_with_liability": "ABANDONED",
            "confirmed_succeeded": "COMPLETED",
        }[resolution]
        async with self._db.transaction() as db:
            current = await db.fetch_one_raw(
                "SELECT * FROM model_response_attempts WHERE attempt_id = ?",
                (attempt_id,),
            )
            if current is None:
                raise KeyError(f"unknown inference attempt: {attempt_id}")
            current_outcome = str(current.get("provider_outcome_status") or "").lower()
            if current_outcome != "unknown":
                raise ValueError(
                    f"attempt {attempt_id} is not awaiting reconciliation: {current_outcome or 'unset'}"
                )
            await db.execute_raw(
                "UPDATE model_response_attempts SET status = ?, provider_outcome_status = ?, "
                "provider_outcome_note = ?, provider_outcome_resolved_at = ?, "
                "provider_response_id = COALESCE(?, provider_response_id), "
                "actual_cost = COALESCE(?, actual_cost), completed_at = "
                "COALESCE(completed_at, ?) WHERE attempt_id = ?",
                (
                    terminal_status,
                    resolution,
                    note,
                    timestamp,
                    provider_response_id,
                    str(normalized_cost) if normalized_cost is not None else None,
                    timestamp,
                    attempt_id,
                ),
            )
            await self._fault_point("attempt-outcome-resolved")
            await db.execute_raw(
                "UPDATE model_response_receipts SET status = ?, provider_outcome_status = ?, "
                "provider_response_id = COALESCE(?, provider_response_id), "
                "actual_cost = COALESCE(?, actual_cost), "
                "provider_outcome_unknown_at = COALESCE(provider_outcome_unknown_at, ?), "
                "completed_at = COALESCE(completed_at, ?) WHERE attempt_id = ?",
                (
                    "FAILED",
                    resolution,
                    provider_response_id,
                    str(normalized_cost) if normalized_cost is not None else None,
                    timestamp,
                    timestamp,
                    attempt_id,
                ),
            )
            resolved = await db.fetch_one_raw(
                "SELECT * FROM model_response_attempts WHERE attempt_id = ?",
                (attempt_id,),
            )
            return dict(resolved or {})

    async def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM model_response_attempts WHERE attempt_id = ?",
            (attempt_id,),
        )
        return dict(row) if row is not None else None

    async def list_unresolved_attempts(self, task_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM model_response_attempts WHERE provider_outcome_status IN "
            "('unknown', 'abandoned_with_liability')"
        )
        params: tuple[Any, ...] = ()
        if task_id is not None:
            sql += " AND task_id = ?"
            params = (task_id,)
        sql += " ORDER BY created_at ASC"
        rows = await self._db.fetch_all(sql, params)
        return [dict(row) for row in rows]

    async def close_provider_liability(self, *, attempt_id: str, note: str) -> dict[str, Any]:
        """Record operator closeout of an abandoned provider liability."""
        note = str(note).strip()
        if not note:
            raise ValueError("provider liability closeout requires a non-empty note")
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            current = await db.fetch_one_raw(
                "SELECT * FROM model_response_attempts WHERE attempt_id = ?",
                (attempt_id,),
            )
            if current is None:
                raise KeyError(f"unknown inference attempt: {attempt_id}")
            if str(current.get("provider_outcome_status") or "").lower() != (
                "abandoned_with_liability"
            ):
                raise ValueError("only abandoned provider liabilities can be manually closed")
            close_note = f"liability closed at {timestamp}: {note}"
            await db.execute_raw(
                "UPDATE model_response_attempts SET reservation_released_at = "
                "COALESCE(reservation_released_at, ?), provider_outcome_note = "
                "TRIM(COALESCE(provider_outcome_note, '') || ' | ' || ?) WHERE attempt_id = ?",
                (timestamp, close_note, attempt_id),
            )
            await db.execute_raw(
                "UPDATE model_response_receipts SET reservation_released_at = "
                "COALESCE(reservation_released_at, ?) WHERE attempt_id = ?",
                (timestamp, attempt_id),
            )
            resolved = await db.fetch_one_raw(
                "SELECT * FROM model_response_attempts WHERE attempt_id = ?",
                (attempt_id,),
            )
            return dict(resolved or {})

    async def has_unresolved_liability(self, task_id: str) -> bool:
        row = await self._db.fetch_one(
            "SELECT 1 AS present FROM model_response_attempts WHERE task_id = ? "
            "AND reservation_applied_at IS NOT NULL AND reservation_released_at IS NULL "
            "AND provider_outcome_status IN ('unknown', 'abandoned_with_liability') LIMIT 1",
            (task_id,),
        )
        return row is not None

    async def list_resolved_attempts(self) -> list[dict[str, Any]]:
        """Return durable dispositions that still need task-side replay."""
        rows = await self._db.fetch_all(
            "SELECT * FROM model_response_attempts WHERE provider_outcome_status IN "
            "('confirmed_failed', 'retry_authorized', 'confirmed_succeeded', "
            "'abandoned_with_liability') ORDER BY provider_outcome_resolved_at ASC"
        )
        return [dict(row) for row in rows]

    async def mark_assistant_appended(self, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET assistant_appended_at = "
                "COALESCE(assistant_appended_at, ?) WHERE attempt_id = ? "
                "AND assistant_appended_at IS NULL",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-assistant-appended")
            await db.execute_raw(
                "UPDATE model_response_receipts SET assistant_appended_at = ? "
                "WHERE attempt_id = ? AND assistant_appended_at IS NULL",
                (timestamp, attempt_id),
            )
            return True

    async def fail(self, *, attempt_id: str) -> bool:
        timestamp = utcnow().isoformat()
        async with self._db.transaction() as db:
            cursor = await db.execute_raw(
                "UPDATE model_response_attempts SET status = 'FAILED', "
                "provider_outcome_status = 'failed', completed_at = COALESCE(completed_at, ?) "
                "WHERE attempt_id = ? AND status IN ('PENDING', 'ACCOUNTED')",
                (timestamp, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            await self._fault_point("attempt-failed")
            await db.execute_raw(
                "UPDATE model_response_receipts SET status = 'FAILED', "
                "provider_outcome_status = 'failed' WHERE attempt_id = ? AND status = 'PENDING'",
                (attempt_id,),
            )
            return True

    @staticmethod
    def response_from_row(row: Mapping[str, Any] | None) -> ModelResponse | None:
        if not row or str(row.get("status") or "") != "COMPLETED":
            return None
        raw = row.get("response")
        if not raw:
            return None
        try:
            return _decode_response(json.loads(str(raw)))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("model response receipt is corrupt") from exc

    async def list_completed(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM model_response_receipts WHERE task_id = ? "
            "AND status = 'COMPLETED' ORDER BY created_at ASC",
            (task_id,),
        )
        return [dict(row) for row in rows]

    async def get_receipt(self, *, task_id: str, request_fingerprint: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM model_response_receipts WHERE task_id = ? AND request_fingerprint = ?",
            (task_id, request_fingerprint),
        )
        return dict(row) if row is not None else None


def _encode_response(response: ModelResponse) -> dict[str, Any]:
    usage = response.usage or UsageInfo()
    return {
        "request_id": response.request_id,
        "model": response.model,
        "provider": response.provider,
        "blocks": [_serialize_block(block) for block in response.blocks],
        "finish_reason": response.finish_reason,
        "usage": asdict(usage),
        "metadata": dict(response.metadata or {}),
    }


def _decode_response(data: Mapping[str, Any]) -> ModelResponse:
    usage_data = dict(data.get("usage") or {})
    return ModelResponse(
        request_id=str(data.get("request_id") or ""),
        model=str(data.get("model") or ""),
        provider=str(data.get("provider") or ""),
        blocks=tuple(
            _deserialize_block(dict(block))
            for block in list(data.get("blocks") or [])
            if isinstance(block, Mapping)
        ),
        finish_reason=data.get("finish_reason"),
        usage=UsageInfo(
            input_tokens=int(usage_data.get("input_tokens") or 0),
            output_tokens=int(usage_data.get("output_tokens") or 0),
            reasoning_tokens=int(usage_data.get("reasoning_tokens") or 0),
            cost_usd=usage_data.get("cost_usd"),
            cache_read_tokens=int(usage_data.get("cache_read_tokens") or 0),
            cache_write_tokens=int(usage_data.get("cache_write_tokens") or 0),
            uncached_input_tokens=usage_data.get("uncached_input_tokens"),
            provider_metadata=dict(usage_data.get("provider_metadata") or {}),
        ),
        metadata=dict(data.get("metadata") or {}),
    )


__all__ = ["ModelResponseStore"]
