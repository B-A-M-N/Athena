"""Small pure parsing helpers shared by the facade and its mechanisms.

Extracted from ``athena.service.service`` during the P1-10 structural
decomposition so that the self-host mechanism (``athena.service.self_host``)
and the reviewer path in the facade can share one definition without a
runtime import cycle. Leaf module: imports nothing from the service package.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

__all__ = [
    "bounded_strings",
    "json_hash",
    "parse_review_json",
    "parse_self_host_completion_verdict",
    "successful_usage",
]


def bounded_strings(value: Any, *, limit: int = 16, item_limit: int = 512) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    return [str(item)[:item_limit] for item in values[:limit] if str(item).strip()]


def parse_self_host_completion_verdict(value: str | None) -> dict[str, Any] | None:
    """Parse the separate completion verifier's strictly bounded response."""
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```"))
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("complete"), bool):
        return None
    return {
        "complete": parsed["complete"],
        "reason": str(parsed.get("reason") or "").strip()[:1000],
        "missing_obligations": bounded_strings(parsed.get("missing_obligations")),
    }


def json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def successful_usage(row: Mapping[str, Any]) -> bool:
    """Accept only a durable completed provider attempt as identity evidence."""
    metadata = row.get("metadata")
    return isinstance(metadata, Mapping) and str(metadata.get("state") or "") == "success"


def parse_review_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.strip().startswith("```"))
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except (TypeError, ValueError):
            return None
    return dict(parsed) if isinstance(parsed, dict) else None
