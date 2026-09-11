"""Provenance records for untrusted remote pack metadata."""

from __future__ import annotations

from typing import Any

_TRUST_ANCHOR_SOURCES = frozenset({"operator", "signed_registry", "model", "unknown"})


def normalize_trust_anchor_source(source: str | None) -> str:
    value = str(source or "unknown").strip().casefold() or "unknown"
    if value not in _TRUST_ANCHOR_SOURCES:
        raise ValueError(
            "expected_sha256_source must be one of: model, operator, signed_registry, unknown"
        )
    return value


def archive_provenance(
    source_url: str,
    endpoint: Any,
    archive_sha256: str,
    *,
    expected_sha256: str | None = None,
    expected_sha256_source: str | None = None,
) -> dict[str, Any]:
    expected = str(expected_sha256 or "").lower() or None
    source = normalize_trust_anchor_source(expected_sha256_source) if expected else None
    matched = expected is not None and expected == archive_sha256
    return {
        "kind": "remote_pack_archive",
        "source_url": str(source_url),
        "archive_sha256": archive_sha256,
        "metadata_trust": "validated_archive_manifest",
        "digest_verification": {
            "algorithm": "sha256",
            "computed": archive_sha256,
            "expected": expected,
            "expected_source": source,
            "matched": matched,
            "source_authenticated": bool(matched and source in {"operator", "signed_registry"}),
        },
        "transport": endpoint.scheme,
        "endpoint_classification": endpoint.classification,
    }


def index_provenance(source_url: str, endpoint: Any) -> dict[str, Any]:
    return {
        "kind": "remote_pack_index_metadata",
        "index_url": str(source_url),
        "transport": endpoint.scheme,
        "endpoint_classification": endpoint.classification,
        "metadata_trust": "untrusted_until_archive_hash_and_manifest_validation",
    }


def remote_archive_receipts(
    source_url: str,
    endpoint: Any,
    archive_sha256: str,
    expected_sha256: str | None,
    expected_sha256_source: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return separate metadata and authenticity records for one archive."""
    expected = str(expected_sha256 or "").lower() or None
    source = normalize_trust_anchor_source(expected_sha256_source)
    matched = bool(expected and archive_sha256 == expected)
    provenance = archive_provenance(
        source_url,
        endpoint,
        archive_sha256,
        expected_sha256=expected,
        expected_sha256_source=source if expected else None,
    )
    authenticity = {
        "transport": endpoint.scheme,
        "endpoint_classification": endpoint.classification,
        "archive_sha256": archive_sha256,
        "expected_sha256": expected,
        "expected_sha256_source": source if expected else None,
        "digest_match": matched,
        "source_authenticated": bool(matched and source in {"operator", "signed_registry"}),
        "operator_expected_sha256": expected if expected and source == "operator" else None,
        "operator_approved": False,
    }
    return provenance, authenticity


__all__ = [
    "archive_provenance",
    "index_provenance",
    "normalize_trust_anchor_source",
    "remote_archive_receipts",
]
