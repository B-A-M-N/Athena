"""Network primitives shared by outbound capabilities."""

from athena.network.transport import (
    pinned_async_transport,
    pinned_sync_transport,
    resolve_addresses,
)

__all__ = ["pinned_async_transport", "pinned_sync_transport", "resolve_addresses"]
