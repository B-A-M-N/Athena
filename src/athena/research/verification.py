"""Evidence verification at the captured-artifact boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from athena.research.models import EvidenceObject, SourceRecord, classify_evidence_quality


async def verify_evidence(
    evidence: EvidenceObject,
    source: SourceRecord | None,
    artifacts: Any,
) -> dict[str, Any]:
    """Verify evidence against its immutable source snapshot and receipt."""
    quality = classify_evidence_quality(evidence, source)
    if source is None:
        return {
            "status": "unverified",
            "quality": quality,
            "reason": "source record not found",
        }
    if not source.artifact_uri or artifacts is None:
        return {
            "status": "unverified",
            "quality": quality,
            "reason": "source snapshot not captured",
        }
    content = await artifacts.load(source.artifact_uri)
    content_hash = hashlib.sha256(content).hexdigest()
    hash_matches = not source.content_hash or content_hash == source.content_hash
    found = hash_matches and evidence.exact_supporting_excerpt.encode("utf-8") in content
    result: dict[str, Any] = {
        "status": "verified" if found else "invalid",
        "quality": quality,
        "content_hash": content_hash,
        "hash_matches": hash_matches,
        "excerpt_hash": hashlib.sha256(
            evidence.exact_supporting_excerpt.encode("utf-8")
        ).hexdigest(),
    }
    recorded_excerpt_hash = evidence.metadata.get("excerpt_hash")
    if recorded_excerpt_hash and recorded_excerpt_hash != result["excerpt_hash"]:
        result["status"] = "invalid"
        result["excerpt_hash_matches"] = False
    else:
        result["excerpt_hash_matches"] = True
    if not found:
        return result

    # Structured evidence is still evidence only when its receipt is
    # internally coherent. The source hash/excerpt check above proves the
    # captured bytes; these checks prove that a measurement or execution
    # claim has the fields needed for replay/review.
    if evidence.evidence_type in {
        "execution",
        "measurement",
        "observation",
        "derivation",
    }:
        receipt = evidence.metadata.get("receipt")
        structured = _verify_receipt(evidence.evidence_type, receipt)
        result["receipt"] = structured
        if structured["status"] != "verified":
            result["status"] = "invalid"
    return result


def _verify_receipt(evidence_type: str, receipt: Any) -> dict[str, Any]:
    """Validate the minimum replay boundary for structured evidence."""
    if not isinstance(receipt, Mapping):
        return {"status": "unverified", "reason": "structured receipt is missing"}
    required: dict[str, tuple[str, ...]] = {
        "execution": ("capability_id", "input_hash", "environment_fingerprint"),
        "measurement": ("value", "unit", "observed_at", "environment_fingerprint"),
        "observation": ("observation", "observed_at", "environment_fingerprint"),
        "derivation": ("inputs", "derivation", "environment_fingerprint"),
    }
    missing = [
        field
        for field in required.get(evidence_type, ())
        if field not in receipt or receipt[field] in (None, "", [])
    ]
    if missing:
        return {"status": "invalid", "missing": missing}
    if evidence_type == "execution":
        exit_code = receipt.get("exit_code")
        ok = receipt.get("ok")
        status = str(receipt.get("status") or "").casefold()
        if exit_code not in (None, 0) or ok is False or status in {"failed", "error", "cancelled"}:
            return {
                "status": "invalid",
                "reason": "execution receipt does not show a successful exit",
            }
    return {
        "status": "verified",
        "evidence_type": evidence_type,
        "fields": sorted(str(key) for key in receipt),
    }


__all__ = ["verify_evidence"]
