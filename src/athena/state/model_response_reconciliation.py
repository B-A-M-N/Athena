"""Legacy-receipt migration and provider-outcome normalization."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from athena.protocol.errors import ProviderOutcomeUnknown
from athena.protocol.messages import utcnow


async def reject_unresolved_legacy_receipts(
    db,
    *,
    task_id: str,
    provider: str,
    model: str,
    request_fingerprint: str,
) -> None:
    """Prevent a new fingerprint from silently bypassing an old liability."""
    rows = await db.fetch_all_raw(
        "SELECT r.*, a.status AS attempt_status, "
        "a.provider_outcome_status AS attempt_outcome_status "
        "FROM model_response_receipts AS r "
        "LEFT JOIN model_response_attempts AS a ON a.attempt_id = r.attempt_id "
        "WHERE r.task_id = ? AND r.provider = ? AND r.model = ? "
        "AND r.request_fingerprint != ?",
        (task_id, provider, model, request_fingerprint),
    )
    unresolved = [row for row in rows if _requires_reconciliation(row)]
    if unresolved:
        attempt_ids = [str(row.get("attempt_id") or "unknown") for row in unresolved]
        raise ProviderOutcomeUnknown(
            "an unresolved inference receipt uses a prior fingerprint format; "
            "reconcile it before starting another provider attempt",
            receipt_attempt_ids=attempt_ids,
        )


async def ensure_attempt_row(
    db,
    row: Mapping[str, Any] | None,
    *,
    idempotency_semantics: str = "none",
) -> None:
    """Backfill the attempt projection for a legacy response receipt."""
    if not row or not row.get("attempt_id"):
        return
    await db.execute_raw(
        "INSERT OR IGNORE INTO model_response_attempts("
        "attempt_id, task_id, request_fingerprint, request_id, provider, model, status, "
        "reservation_amount, reservation_applied_at, provider_usage_id, "
        "provider_outcome_status, created_at, completed_at, actual_input_tokens, "
        "actual_output_tokens, actual_cost, response_committed_at, accounting_applied_at, idempotency_key, "
        "idempotency_semantics, provider_outcome_note, provider_outcome_resolved_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["attempt_id"],
            row["task_id"],
            row["request_fingerprint"],
            row["request_id"],
            row["provider"],
            row["model"],
            row.get("status") or "PENDING",
            row.get("reservation_amount"),
            row.get("reservation_applied_at"),
            row.get("provider_usage_id"),
            row.get("provider_outcome_status"),
            row.get("created_at") or utcnow().isoformat(),
            row.get("completed_at"),
            row.get("actual_input_tokens"),
            row.get("actual_output_tokens"),
            row.get("actual_cost"),
            row.get("response_committed_at"),
            row.get("accounting_applied_at"),
            row.get("idempotency_key") or row.get("attempt_id"),
            row.get("idempotency_semantics") or idempotency_semantics,
            row.get("provider_outcome_note"),
            row.get("provider_outcome_resolved_at"),
        ),
    )


async def archive_attempt(db, row: Mapping[str, Any]) -> None:
    await ensure_attempt_row(db, row)
    if row.get("attempt_id"):
        await db.execute_raw(
            "UPDATE model_response_attempts SET status = 'FAILED', "
            "provider_outcome_status = 'failed', "
            "completed_at = COALESCE(completed_at, ?) WHERE attempt_id = ?",
            (row.get("completed_at") or utcnow().isoformat(), row["attempt_id"]),
        )


def validated_actual_cost(value: Decimal | str | None) -> Decimal | None:
    """Normalize operator-supplied cost without allowing non-finite values."""
    if value is None:
        return None
    try:
        cost = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("actual_cost must be a finite, non-negative Decimal") from exc
    if not cost.is_finite() or cost < 0:
        raise ValueError("actual_cost must be a finite, non-negative Decimal")
    return cost


def _requires_reconciliation(row: Mapping[str, Any]) -> bool:
    receipt_status = str(row.get("status") or "").upper()
    attempt_status = str(row.get("attempt_status") or "").upper()
    receipt_outcome = str(row.get("provider_outcome_status") or "").lower()
    attempt_outcome = str(row.get("attempt_outcome_status") or "").lower()
    if receipt_status == "PENDING":
        return True
    if receipt_outcome in {"pending", "unknown"} or attempt_outcome in {"pending", "unknown"}:
        return True
    if attempt_status in {"PENDING", "ACCOUNTED", "UNKNOWN"}:
        return True
    if receipt_status == "COMPLETED":
        return not row.get("accounting_applied_at") or bool(
            row.get("provider_usage_id") and not row.get("provider_usage_completed_at")
        )
    return False


__all__ = [
    "archive_attempt",
    "ensure_attempt_row",
    "reject_unresolved_legacy_receipts",
    "validated_actual_cost",
]
