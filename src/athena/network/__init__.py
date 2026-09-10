"""Network primitives shared by outbound capabilities."""

from athena.network.transport import (
    pinned_async_transport,
    pinned_sync_transport,
    resolve_addresses,
)
from athena.network.target_policy import ValidatedTarget, validate_target
from athena.network.browser_proxy import BrowserProxyConfig
from athena.network.endpoint_security import (
    EndpointIdentity,
    EndpointSecurityError,
    classify_endpoint,
    validate_endpoint,
)

__all__ = [
    "ValidatedTarget",
    "pinned_async_transport",
    "pinned_sync_transport",
    "resolve_addresses",
    "validate_target",
    "BrowserProxyConfig",
    "EndpointIdentity",
    "EndpointSecurityError",
    "classify_endpoint",
    "validate_endpoint",
]
