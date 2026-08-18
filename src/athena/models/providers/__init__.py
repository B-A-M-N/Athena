"""Provider adapters for the ModelProvider protocol (BUILDSPEC 23-27).

Each adapter owns all provider-specific translation (INV-006); the router and
kernel consume only the canonical provider-neutral types. Keep new providers
here rather than branching in the router.
"""

from athena.models.providers.anthropic import AnthropicProvider
from athena.models.providers.fake import FakeModelProvider
from athena.models.providers.openai_compat import OpenAICompatProvider

__all__ = ["AnthropicProvider", "FakeModelProvider", "OpenAICompatProvider"]