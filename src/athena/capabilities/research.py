"""Model-facing adapter for the research domain service."""

from __future__ import annotations

from athena.protocol.capabilities import CapabilityRequest, CapabilityResult
from athena.research.discovery import (
    BraveSearchProvider,
    HttpDiscoveryProvider,
    ResearchDiscoveryProvider,
    TavilySearchProvider,
)
from athena.research.service import ResearchService
from athena.capabilities.research_contract import RESEARCH_DESCRIPTOR


class ResearchCapability:
    """Thin model-facing adapter around the research domain service."""

    descriptor = RESEARCH_DESCRIPTOR

    def __init__(self, *args, **kwargs) -> None:
        self._service = ResearchService(*args, **kwargs)

    @property
    def service(self) -> ResearchService:
        return self._service

    def _conformance_failure_probe(self) -> dict:
        return self._service._conformance_failure_probe()

    async def invoke(self, request: CapabilityRequest, **kwargs) -> CapabilityResult:
        return await self._service.execute(request, **kwargs)


__all__ = [
    "BraveSearchProvider",
    "HttpDiscoveryProvider",
    "ResearchDiscoveryProvider",
    "ResearchService",
    "ResearchCapability",
    "TavilySearchProvider",
]
