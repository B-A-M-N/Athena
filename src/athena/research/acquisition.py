"""Pinned network acquisition for research sources.

Extracted from ``research/service.py`` so the domain service owns policy and
persistence while this module owns the actual network acquisition primitive
(DNS pinning + bounded HTTP stream). The service contract text no longer
needs to claim "never fetches": acquisition is explicit, named, and
policy-gated (review item 21).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.network import pinned_async_transport
from athena.research.discovery import resolve_with_timeout
from athena.research.policy import SourcePolicy, SourcePolicyError


@dataclass(frozen=True)
class AcquisitionResult:
    """Bounded result of one pinned acquisition attempt."""

    content: bytes
    media_type: str
    status_code: int
    resolved_addresses: tuple[str, ...]


async def fetch_pinned(
    *,
    source_policy: SourcePolicy,
    host_resolver: Any,
    canonical: str,
    host: str,
    port: int,
    timeout: float,
    max_bytes: int,
) -> AcquisitionResult:
    """Fetch one allowlisted source with DNS pinning and bounded body."""
    try:
        infos = await resolve_with_timeout(
            host_resolver,
            host,
            port,
            timeout=timeout,
        )
        addresses = [str(info[4][0]) for info in infos]
        resolved_addresses = source_policy.check_resolved(host, addresses)
    except (OSError, SourcePolicyError) as exc:
        raise SourceAcquisitionError(f"source DNS check failed: {exc}") from exc

    import httpx

    try:
        transport = pinned_async_transport(host, resolved_addresses)
        async with (
            httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "Athena-Research/1"},
                transport=transport,
            ) as client,
            client.stream("GET", canonical) as response,
        ):
            if response.status_code >= 300:
                location = response.headers.get("location")
                suffix = f" location={location}" if location else ""
                raise HttpStatusError(
                    f"source fetch returned HTTP {response.status_code}{suffix}",
                    status_code=response.status_code,
                )
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise MaxBytesExceededError(
                        f"source exceeds max_bytes={max_bytes}",
                        max_bytes=max_bytes,
                    )
                chunks.append(chunk)
            media_type = (
                response.headers.get("content-type", "application/octet-stream")
                .split(";", 1)[0]
                .strip()
                or "application/octet-stream"
            )
            status_code = response.status_code
    except httpx.HTTPError as exc:
        raise SourceAcquisitionError(f"source fetch failed: {exc}") from exc

    return AcquisitionResult(
        content=b"".join(chunks),
        media_type=media_type,
        status_code=status_code,
        resolved_addresses=tuple(resolved_addresses),
    )


class SourceAcquisitionError(Exception):
    """DNS or transport failure during pinned acquisition."""


class HttpStatusError(SourceAcquisitionError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class MaxBytesExceededError(SourceAcquisitionError):
    def __init__(self, message: str, *, max_bytes: int) -> None:
        super().__init__(message)
        self.max_bytes = max_bytes
