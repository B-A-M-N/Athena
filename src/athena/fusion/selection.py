"""Durable candidate-selection lifecycle for multi-candidate comparisons.

The comparison record carries the exact branch identities, verification
evidence, and selection state so a restart cannot silently promote the most
recently created candidate instead of the one the kernel explicitly selected.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow

__all__ = ["ComparisonRecord", "CandidateSelectionStore"]


@dataclass
class ComparisonRecord:
    """One bounded candidate comparison for one task.

    Only ``verified_branch_ids`` are selectable. Attempted, failed, and
    discarded identities are retained as evidence, never as selection targets.
    """

    comparison_id: str
    task_id: str
    attempted_branch_ids: list[str] = field(default_factory=list)
    verified_branch_ids: list[str] = field(default_factory=list)
    failed_branch_ids: list[str] = field(default_factory=list)
    discarded_branch_ids: list[str] = field(default_factory=list)
    candidate_branch_ids: list[str] = field(default_factory=list)
    verification_certificates: dict[str, dict] = field(default_factory=dict)
    selected_branch_id: str | None = None
    selected_branch_fingerprint: str | None = None
    selected_certificate_hash: str | None = None
    rejected_branch_ids: list[str] = field(default_factory=list)
    lifecycle: str = "COMPARING"  # COMPARING | SELECTED | DISCARDED
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    selected_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "ComparisonRecord":
        attempted = list(raw.get("attempted_branch_ids") or ())
        verified = list(raw.get("verified_branch_ids") or ())
        if not attempted:
            # Legacy records stored every ID-bearing branch as a candidate;
            # conservatively treat those identities as attempted, not verified.
            attempted = list(raw.get("candidate_branch_ids") or ())
        return cls(
            comparison_id=str(raw["comparison_id"]),
            task_id=str(raw["task_id"]),
            attempted_branch_ids=attempted,
            verified_branch_ids=verified,
            failed_branch_ids=list(raw.get("failed_branch_ids") or ()),
            discarded_branch_ids=list(raw.get("discarded_branch_ids") or ()),
            candidate_branch_ids=verified,
            verification_certificates=dict(raw.get("verification_certificates") or {}),
            selected_branch_id=raw.get("selected_branch_id"),
            selected_branch_fingerprint=raw.get("selected_branch_fingerprint"),
            selected_certificate_hash=raw.get("selected_certificate_hash"),
            rejected_branch_ids=list(raw.get("rejected_branch_ids") or ()),
            lifecycle=str(raw.get("lifecycle") or "COMPARING"),
            created_at=str(raw.get("created_at") or utcnow().isoformat()),
            selected_at=raw.get("selected_at"),
        )


class CandidateSelectionStore:
    """Durable comparison records, persisted atomically beside branch state."""

    def __init__(self, state_root: str | Path) -> None:
        self._state_root = Path(state_root)
        self._comparisons_path = self._state_root / "comparisons.json"
        self._records: dict[str, ComparisonRecord] = {}
        self._load()

    def create(
        self,
        *,
        task_id: str,
        candidate_branch_ids: list[str],
        verified_branch_ids: list[str] | None = None,
        verification_certificates: dict[str, dict] | None = None,
    ) -> ComparisonRecord:
        verified = list(verified_branch_ids or ())
        if not verified:
            # New callers must classify outcomes explicitly.
            verified = []
        record = ComparisonRecord(
            comparison_id=new_id("cmp"),
            task_id=task_id,
            attempted_branch_ids=list(candidate_branch_ids),
            verified_branch_ids=verified,
            failed_branch_ids=[
                branch_id for branch_id in candidate_branch_ids if branch_id not in verified
            ],
            candidate_branch_ids=verified,
            verification_certificates=dict(verification_certificates or {}),
        )
        self._records[record.comparison_id] = record
        self._persist()
        return record

    def get(self, comparison_id: str) -> ComparisonRecord | None:
        return self._records.get(comparison_id)

    def latest_for_task(self, task_id: str) -> ComparisonRecord | None:
        for record in reversed(list(self._records.values())):
            if record.task_id == task_id:
                return record
        return None

    def select(self, comparison_id: str, branch_id: str) -> ComparisonRecord:
        """Explicitly select one candidate and mark all others rejected."""
        record = self._records.get(comparison_id)
        if record is None:
            raise KeyError(f"comparison not found: {comparison_id}")
        if record.lifecycle not in {"COMPARING", "SELECTED"}:
            raise ValueError(f"comparison {comparison_id} is {record.lifecycle}; cannot select")
        if branch_id not in record.verified_branch_ids:
            raise ValueError(
                f"branch {branch_id} is not a verified candidate of comparison {comparison_id}"
            )
        if record.selected_branch_id is not None and record.selected_branch_id != branch_id:
            raise ValueError(
                f"comparison {comparison_id} already selected {record.selected_branch_id}; "
                f"cannot reselect {branch_id}"
            )
        certificate = dict(record.verification_certificates.get(branch_id) or {})
        record.selected_branch_id = branch_id
        record.selected_branch_fingerprint = str(certificate.get("candidate_fingerprint")) or None
        record.selected_certificate_hash = str(certificate.get("certificate_hash")) or None
        record.rejected_branch_ids = [bid for bid in record.verified_branch_ids if bid != branch_id]
        record.lifecycle = "SELECTED"
        record.selected_at = utcnow().isoformat()
        self._persist()
        return record

    def find_by_branch(self, branch_id: str) -> ComparisonRecord | None:
        for record in self._records.values():
            if branch_id in record.candidate_branch_ids:
                return record
        return None

    def latest_selected_for_task(self, task_id: str) -> ComparisonRecord | None:
        """Return the most recent record with an explicit selection."""
        for record in reversed(list(self._records.values())):
            if record.task_id == task_id and record.lifecycle == "SELECTED":
                return record
        return None

    def selected_branch_for_task(self, task_id: str) -> str | None:
        """Return the explicitly selected branch for a task, if any.

        Later comparisons that are still COMPARING do not erase an explicit
        selection; CandidateService blocks selection changes while they are
        open, but the existing exact choice remains the task's identity.
        """
        record = self.latest_selected_for_task(task_id)
        return record.selected_branch_id if record is not None else None

    def selected_identity_for_task(self, task_id: str) -> dict | None:
        """Return exact selected branch identity for integrity checks."""
        record = self.latest_selected_for_task(task_id)
        if record is None:
            return None
        return {
            "comparison_id": record.comparison_id,
            "branch_id": record.selected_branch_id,
            "candidate_fingerprint": record.selected_branch_fingerprint,
            "certificate_hash": record.selected_certificate_hash,
        }

    def _persist(self) -> None:
        self._state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        records = [r.to_dict() for r in self._records.values()]
        tmp = self._comparisons_path.with_suffix(".tmp")
        payload = json.dumps(records, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._comparisons_path)
        try:
            directory_fd = os.open(self._state_root, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load(self) -> None:
        try:
            records = json.loads(self._comparisons_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return
        if not isinstance(records, list):
            return
        for raw in records:
            if not isinstance(raw, dict) or not raw.get("comparison_id"):
                continue
            try:
                record = ComparisonRecord.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            self._records[record.comparison_id] = record
