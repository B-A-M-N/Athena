"""Conservative model-request token bounds.

Planning estimates may be cheap and approximate. Hard admission needs a bound
that includes the complete request envelope, not only visible message text.
Profiles may set ``token_upper_bound_per_byte=None`` when no safe bound exists;
callers must then refuse finite hard input budgets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from athena.protocol.models import ModelRequest


@dataclass(frozen=True)
class ModelTokenEstimator:
    """Provide a safe upper bound for one canonical model request."""

    token_upper_bound_per_byte: int | None = 1
    message_overhead: int = 32
    capability_overhead: int = 64

    @classmethod
    def from_profile(cls, profile: Any = None) -> "ModelTokenEstimator":
        value = getattr(profile, "token_upper_bound_per_byte", 1)
        if isinstance(profile, dict):
            value = profile.get("token_upper_bound_per_byte", 1)
        if value is None:
            bound = None
        else:
            try:
                bound = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "token_upper_bound_per_byte must be a positive integer or null"
                ) from exc
            if bound < 1:
                raise ValueError("token_upper_bound_per_byte must be positive")
        return cls(token_upper_bound_per_byte=bound)

    def upper_bound(self, request: ModelRequest) -> int | None:
        """Return a bound for all request content, or ``None`` if unknown."""
        if self.token_upper_bound_per_byte is None:
            return None
        payload = [request.system or ""]
        payload.extend(message.text() or "" for message in request.messages)
        for capability in request.capabilities:
            to_dict = getattr(capability, "to_dict", None)
            value = to_dict() if callable(to_dict) else vars(capability)
            payload.append(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
        byte_count = sum(len(value.encode("utf-8")) for value in payload)
        overhead = self.message_overhead * max(1, len(request.messages))
        overhead += self.capability_overhead * len(request.capabilities)
        return max(1, byte_count * self.token_upper_bound_per_byte + overhead)


__all__ = ["ModelTokenEstimator"]
