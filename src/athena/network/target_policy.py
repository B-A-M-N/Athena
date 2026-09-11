"""Shared outbound target validation for network-capable capabilities.

The validator is deliberately independent of HTTP/browser clients.  A caller
gets the host's resolved addresses so the eventual transport can pin the
connection to the addresses policy inspected.  Browser request interception
uses the same function for redirects and subresources.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from athena.network.transport import resolve_addresses


@dataclass(frozen=True)
class ValidatedTarget:
    target: str
    hostname: str
    addresses: tuple[str, ...] = ()


def validate_target(
    target: str,
    policy: str | object | None,
    *,
    resolver: Callable[[str, int], tuple[str, ...]] | None = None,
) -> tuple[ValidatedTarget | None, str | None]:
    """Validate a URL or hostname under an Athena network policy.

    ``ALLOW`` returns a target without DNS resolution because no restricted
    destination pinning is required. ``DENY`` rejects all outbound targets.
    ``RESTRICTED`` resolves every hostname and rejects local/private/link-local
    and other non-public destinations.
    """
    policy_name = str(getattr(policy, "value", policy) or "allow").casefold()
    raw = str(target or "").strip()
    if not raw:
        return None, "network target is required"
    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
            return None, "network target must use http or https"
        if parsed.username is not None or parsed.password is not None:
            return None, "network target must not contain URL userinfo"
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None, "network target contains an invalid port"
    except ValueError:
        return None, "network target has malformed URL authority"
    if not hostname:
        return None, f"network target has no hostname: {target}"
    if policy_name == "deny":
        return None, "network denied by workspace policy"
    if policy_name != "restricted":
        return ValidatedTarget(raw, hostname), None
    if hostname in {"localhost", "localhost.localdomain"}:
        return None, f"restricted network policy rejects local target: {hostname}"
    try:
        addresses: tuple[str, ...] = (str(ipaddress.ip_address(hostname)),)
    except ValueError:
        try:
            resolve = resolver or resolve_addresses
            addresses = tuple(str(address) for address in resolve(hostname, 0))
        except (OSError, socket.gaierror, ValueError):
            return None, f"unable to resolve host under restricted network policy: {hostname}"
    if not addresses:
        return None, f"unable to resolve host under restricted network policy: {hostname}"
    try:
        parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError:
        return None, f"resolver returned an invalid address for restricted target: {hostname}"
    if any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for address in parsed_addresses
    ):
        return None, f"restricted network policy rejects private/local host: {hostname}"
    return ValidatedTarget(raw, hostname, addresses), None


__all__ = ["ValidatedTarget", "validate_target"]
