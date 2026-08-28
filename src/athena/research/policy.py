"""Pre-acquisition source policy and canonical source identity."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class SourcePolicyError(ValueError):
    """A source URI is not allowed by the configured research policy."""


@dataclass(frozen=True)
class SourcePolicy:
    """Allowlist policy applied before an external source is acquired.

    An empty ``allowed_domains`` is intentional: no external HTTP(S) source
    is permitted until the operator configures an explicit domain allowlist.
    Artifact URIs are local immutable snapshots and remain usable without a
    network allowlist.
    """

    allowed_domains: tuple[str, ...] = ()
    denied_domains: tuple[str, ...] = ()
    allow_private_network: bool = False

    def check(self, uri: str) -> str:
        canonical = canonicalize_uri(uri)
        parsed = urlsplit(canonical)
        if parsed.scheme == "artifact":
            return canonical
        if parsed.scheme not in {"http", "https"}:
            raise SourcePolicyError(
                "research sources must use https/http or an artifact:// snapshot"
            )
        if parsed.username or parsed.password:
            raise SourcePolicyError("source URI credentials are not allowed")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise SourcePolicyError("source URI has no hostname")
        if _matches_domain(host, self.denied_domains):
            raise SourcePolicyError(f"source domain is denied: {host}")
        if not self.allowed_domains:
            raise SourcePolicyError("no research source domains are configured")
        if not _matches_domain(host, self.allowed_domains):
            raise SourcePolicyError(f"source domain is not allowlisted: {host}")
        if not self.allow_private_network and _is_private_host(host):
            raise SourcePolicyError(f"private/local source address is denied: {host}")
        return canonical

    def check_resolved(self, host: str, addresses: Iterable[str]) -> tuple[str, ...]:
        """Reject private addresses returned for an allowlisted hostname.

        URI allowlisting alone is not enough: a permitted hostname can resolve
        to loopback, link-local, metadata, or private address space. Fetchers
        call this after DNS resolution and before opening the HTTP connection.
        The resolved set is returned in stable order for provenance.
        """
        normalized = tuple(sorted({str(address).strip() for address in addresses if address}))
        if not normalized:
            raise SourcePolicyError(f"source hostname did not resolve: {host}")
        if not self.allow_private_network:
            private = [address for address in normalized if _is_private_host(address)]
            if private:
                raise SourcePolicyError(
                    f"source hostname resolves to private/local address: {host}"
                )
        return normalized


def canonicalize_uri(uri: str) -> str:
    """Normalize source identity without changing query semantics."""
    raw = str(uri or "").strip()
    if not raw:
        raise SourcePolicyError("source URI is required")
    parsed = urlsplit(raw)
    if parsed.username is not None or parsed.password is not None:
        raise SourcePolicyError("source URI credentials are not allowed")
    scheme = parsed.scheme.lower()
    if scheme == "artifact":
        if not parsed.netloc or not parsed.path or parsed.query or parsed.fragment:
            raise SourcePolicyError("invalid artifact source URI")
        return urlunsplit(("artifact", parsed.netloc.lower(), parsed.path, "", ""))
    if scheme not in {"http", "https"}:
        raise SourcePolicyError(f"unsupported source URI scheme: {scheme or '<none>'}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise SourcePolicyError("source URI has no hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourcePolicyError("invalid source URI port") from exc
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def classify_source(uri: str) -> str:
    """Conservative authority class used for ranking, never authorization."""
    host = (urlsplit(uri).hostname or "").lower().rstrip(".")
    primary = (
        ".gov",
        ".edu",
        "arxiv.org",
        "nature.com",
        "science.org",
        "aps.org",
        "nih.gov",
        "cern.ch",
        "iop.org",
        "acm.org",
        "ieee.org",
    )
    secondary = ("reuters.com", "bbc.com", "nytimes.com", "economist.com")
    if any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in primary):
        return "primary"
    if any(host == suffix or host.endswith("." + suffix) for suffix in secondary):
        return "secondary"
    return "tertiary"


def _matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    for value in domains:
        domain = str(value or "").lower().strip().rstrip(".").lstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


def _is_private_host(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        # Do not resolve DNS here. Fetchers must re-check resolved addresses
        # at connection time to close DNS-rebinding/SSRF races.
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


__all__ = [
    "SourcePolicy",
    "SourcePolicyError",
    "canonicalize_uri",
    "classify_source",
]
