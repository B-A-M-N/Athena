"""Network primitives shared by outbound capabilities."""

from athena.network.transport import (
    pinned_async_transport,
    pinned_sync_transport,
    resolve_addresses,
)
from athena.network.target_policy import ValidatedTarget, validate_target

__all__ = [
    "ValidatedTarget",
    "pinned_async_transport",
    "pinned_sync_transport",
    "resolve_addresses",
    "validate_target",
]
