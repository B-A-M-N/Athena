"""Content-addressed encoding and validation for portable capsules."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def with_id(body: Mapping[str, Any]) -> dict[str, Any]:
    canonical = json.dumps(dict(body), sort_keys=True, separators=(",", ":"))
    value = dict(body)
    value["capsule_id"] = "capsule_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return value


def decode(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError("capsule must be an object or JSON object string")
    capsule = dict(value)
    if capsule.get("format") != 1:
        raise ValueError("unsupported capsule format")
    supplied_id = str(capsule.pop("capsule_id", None) or "")
    if not supplied_id:
        raise ValueError("capsule_id is required")
    expected = with_id(capsule)["capsule_id"]
    if supplied_id != expected:
        raise ValueError("capsule content hash does not match capsule_id")
    capsule["capsule_id"] = supplied_id
    return capsule


__all__ = ["decode", "with_id"]
