"""MODELS layer — model routing, provider registry, and provider adapters.

Router and registry expose the canonical ``athena.protocol.models.ModelProvider``
protocol (BUILDSPEC 23). Each provider adapter owns its own wire translation
(INV-006); this package imports them as modules so ``athena.models.router``,
``athena.models.registry``, and ``athena.models.providers`` are directly
importable names.
"""

from athena.models.registry import ProviderRegistry
from athena.models.router import (
    ModelRequirements,
    ModelRouter,
    ModelSelection,
)

__all__ = [
    "ProviderRegistry",
    "ModelRouter",
    "ModelRequirements",
    "ModelSelection",
]
