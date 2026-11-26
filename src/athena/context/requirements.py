"""Context-to-model requirement construction.

This module converts the bounded context compiler output into declarative
model requirements. It does not select or admit a provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from athena.models.admission import CAP_AUDIO_INPUT, CAP_TOOLS, CAP_VISION
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.messages import (
    ArtifactRefBlock,
    AudioBlock,
    CapabilityResultBlock,
    ImageBlock,
    Message,
)
from athena.protocol.models import ModelRequirements
from athena.protocol.tasks import TaskSpec
from athena.strategy import is_explicit_response_turn

__all__ = ["build_model_requirements"]


def build_model_requirements(
    task: TaskSpec,
    attachments: Sequence[Any],
    compiled_tokens: int,
    *,
    capabilities: Sequence[CapabilityDescriptor] = (),
    recent_messages: Sequence[Message] = (),
    reserve_output: int = 0,
    safety_margin: int = 0,
) -> ModelRequirements:
    """Build hard model requirements from one bounded context snapshot."""
    caps: set[str] = set()
    needs_tools = bool(capabilities) or bool(task.model_policy.require_tools)
    # A tool-eligible turn needs a tool-capable provider even when discovery
    # came back empty; a definite-response turn does not.
    needs_tools = needs_tools or not is_explicit_response_turn(task.objective)
    if needs_tools:
        caps.add(CAP_TOOLS)
    visual_inputs = tuple(attachments) + tuple(recent_messages)
    audio_inputs = tuple(attachments)
    visual = _has_visuals(visual_inputs)
    audio = _has_audio(audio_inputs)
    if visual:
        caps.add(CAP_VISION)
    if audio:
        caps.add(CAP_AUDIO_INPUT)
    return ModelRequirements(
        required_capabilities=frozenset(caps),
        estimated_input_tokens=compiled_tokens,
        minimum_context_window_tokens=compiled_tokens + reserve_output + safety_margin,
        requested_output_tokens=reserve_output or None,
    )


def _has_visuals(blocks: Sequence[Any]) -> bool:
    for value in blocks:
        if isinstance(value, Message):
            if _has_visuals(value.blocks):
                return True
            continue
        if isinstance(value, ImageBlock):
            return True
        if isinstance(value, ArtifactRefBlock):
            ref = value.ref
            if str(getattr(ref, "mime_type", "") or "").startswith("image/"):
                return True
            continue
        if isinstance(value, CapabilityResultBlock):
            metadata = value.metadata
            if str(metadata.get("mime_type", "") or "").startswith("image/"):
                return True
            artifact_ref = metadata.get("artifact_ref")
            if isinstance(artifact_ref, Mapping) and str(
                artifact_ref.get("mime_type", "") or ""
            ).startswith("image/"):
                return True
            continue
        if isinstance(value, dict) and str(value.get("mime_type", "")).startswith("image/"):
            return True
        if str(getattr(value, "mime_type", "") or "").startswith("image/"):
            return True
    return False


def _has_audio(blocks: Sequence[Any]) -> bool:
    return any(
        isinstance(block, AudioBlock)
        or (isinstance(block, dict) and str(block.get("mime_type", "")).startswith("audio/"))
        or (getattr(block, "mime_type", "") or "").startswith("audio/")
        for block in blocks
    )
