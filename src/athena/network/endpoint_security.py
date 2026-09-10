"""Conservative security policy for credentialed HTTP endpoints.

This boundary is shared by model providers and MCP Streamable HTTP.  It is
intentionally based on the URL authority rather than a DNS lookup: a
credentialed request must not be sent over non-loopback HTTP even when a
hostname later resolves to a private address, and proxy environment variables
must never silently change the route selected by the operator.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit


class EndpointSecurityError(ValueError):
    """The configured endpoint violates Athena's outbound transport policy."""


@dataclass(frozen=True)
class EndpointIdentity:
    """Non-secret facts used for readiness and audit diagnostics."""

    url: str
    scheme: str
    hostname: str
    classification: str
    loopback: bool
    trust_env: bool = False


def classify_endpoint(url: str) -> str:
    """Classify a URL host as loopback, private, link-local, or public."""
    parsed = _split_endpoint(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        return "loopback"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Hostname classification cannot safely perform DNS here.  The
        # transport policy still treats it as non-loopback for HTTP rules.
        return "public"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_private:
        return "private"
    return "public"


def validate_endpoint(
    url: str,
    *,
    credentialed: bool,
    allow_insecure_remote: bool = False,
    trust_env: bool = False,
) -> EndpointIdentity:
    """Validate an HTTP endpoint before constructing a network client.

    Loopback HTTP is allowed for local development.  All other HTTP is
    rejected for credentialed clients and requires an explicit operator-only
    development override for unauthenticated clients.  ``trust_env`` is
    returned for auditability but remains false unless explicitly configured.
    """
    parsed = _split_endpoint(url)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise EndpointSecurityError("endpoint scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise EndpointSecurityError("endpoint URL must not contain userinfo")
    try:
        port = parsed.port
    except ValueError as exc:
        raise EndpointSecurityError("endpoint URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise EndpointSecurityError("endpoint URL contains an invalid port")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    classification = classify_endpoint(url)
    loopback = classification == "loopback"
    if scheme == "http" and not loopback:
        if credentialed:
            raise EndpointSecurityError(
                "credentialed non-loopback HTTP endpoints are not permitted"
            )
        if not allow_insecure_remote:
            raise EndpointSecurityError(
                "remote HTTP requires the explicit operator development override"
            )
    return EndpointIdentity(
        url=str(url),
        scheme=scheme,
        hostname=hostname,
        classification=classification,
        loopback=loopback,
        trust_env=bool(trust_env),
    )


def _split_endpoint(url: str):
    raw = str(url or "").strip()
    if not raw:
        raise EndpointSecurityError("endpoint URL is required")
    parsed = urlsplit(raw)
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise EndpointSecurityError("endpoint URL contains an invalid host") from exc
    if not parsed.netloc or not hostname:
        raise EndpointSecurityError("endpoint URL must include a hostname")
    return parsed


__all__ = [
    "EndpointIdentity",
    "EndpointSecurityError",
    "classify_endpoint",
    "validate_endpoint",
]
