"""Canonical durable identity for one logical provider attempt."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping as ABCMapping
from enum import Enum
from typing import Any

from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.models import ModelRequest
from athena.protocol.tasks import TaskSpec

_REQUEST_FINGERPRINT_VERSION = 2


def _canonical_fingerprint_value(value: Any, *, path: str = "$") -> Any:
    """Convert fingerprint input to a strict, process-independent JSON value."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Enum):
        return _canonical_fingerprint_value(value.value, path=path)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"unsupported non-finite fingerprint value at {path}")
        return value
    if isinstance(value, ABCMapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"fingerprint mapping key at {path} must be a string")
            normalized[key] = _canonical_fingerprint_value(item, path=f"{path}.{key}")
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [
            _canonical_fingerprint_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        normalized_set = [
            _canonical_fingerprint_value(item, path=f"{path}{{item}}") for item in value
        ]
        return sorted(
            normalized_set,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
        )
    raise TypeError(f"unsupported fingerprint value at {path}: {type(value).__name__}")


def _capability_fingerprint_record(capability: CapabilityDescriptor) -> dict[str, Any]:
    """Return the stable contract surface used in a provider request identity."""
    if not isinstance(capability, CapabilityDescriptor):
        raise TypeError("model request capabilities must contain CapabilityDescriptor values")
    record = dict(capability.to_record())
    record.update(
        {
            "resources": (
                sorted(resource.value for resource in capability.resources)
                if capability.resources is not None
                else None
            ),
            "retry_policy": (
                capability.retry_policy.value if capability.retry_policy is not None else None
            ),
            "dynamic_resource_key": capability.resource_key_resolver is not None,
            "source_schema": capability.source_schema,
        }
    )
    return record


def _request_fingerprint(
    task: TaskSpec,
    request: ModelRequest,
    *,
    inference_kind: str | None,
    attempt: int,
) -> str:
    """Hash the exact logical provider prompt, excluding random request IDs."""
    from athena.state.sessions import serialize_block

    payload = {
        "fingerprint_version": _REQUEST_FINGERPRINT_VERSION,
        "task_id": task.id,
        "kind": inference_kind or "primary",
        "attempt": attempt,
        "provider": request.provider,
        "model": request.model,
        "system": request.system,
        "max_tokens": request.max_tokens,
        "stop": list(request.stop),
        "messages": [
            {
                "role": message.role.value,
                "blocks": [serialize_block(block) for block in message.blocks],
                "metadata": message.metadata or {},
            }
            for message in request.messages
        ],
        "metadata": request.metadata or {},
        "capabilities": [
            _capability_fingerprint_record(capability) for capability in request.capabilities
        ],
    }
    encoded = json.dumps(
        _canonical_fingerprint_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["_request_fingerprint"]
