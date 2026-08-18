"""Canonical provider-neutral message model.

All providers normalize into this representation (IMPLEMENTATIONSPEC section
11). Provider schemas MUST be translated at adapter boundaries. The persistent
message schema MUST NOT assume OpenAI tool calls, Anthropic blocks, or any
provider-specific shape.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from athena.protocol.artifacts import ArtifactRef



class Role(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    CAPABILITY = "capability"
    COMPRESSION = "compression"


class SourceType(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    SESSION = "session"
    TASK = "task"
    MEMORY = "memory"
    SKILL = "skill"
    PROJECT_INSTRUCTION = "project_instruction"
    FILE = "file"
    ARTIFACT = "artifact"
    MCP = "MCP"
    WEB = "web"
    RUNTIME = "runtime"
    CAPABILITY = "capability"
    GENERATED = "generated"


class TrustClass(str, enum.Enum):
    AUTHORITY = "authority"
    CONFIGURED_INSTRUCTION = "configured_instruction"
    USER_CONTENT = "user_content"
    AGENT_CURATED = "agent_curated"
    EXTERNAL_CONTENT = "external_content"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class Provenance:
    source_type: SourceType
    source_id: str | None = None
    trust: TrustClass = TrustClass.AGENT_CURATED
    scope: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ContentBlock:
    type: str


@dataclass(frozen=True)
class TextBlock(ContentBlock):
    type: str = "text"
    text: str = ""
    provenance: Provenance | None = None


@dataclass(frozen=True)
class ReasoningBlock(ContentBlock):
    text: str = ""
    type: str = "reasoning"


@dataclass(frozen=True)
class ImageBlock(ContentBlock):
    data_path: str | None = None
    mime_type: str | None = None
    type: str = "image"


@dataclass(frozen=True)
class AudioBlock(ContentBlock):
    data_path: str | None = None
    mime_type: str | None = None
    type: str = "audio"


@dataclass(frozen=True)
class FileRefBlock(ContentBlock):
    uri: str = ""
    mime_type: str | None = None
    type: str = "file_ref"


@dataclass(frozen=True)
class ArtifactRefBlock(ContentBlock):
    uri: str = ""
    ref: "ArtifactRef | None" = None
    type: str = "artifact_ref"


@dataclass(frozen=True)
class CapabilityCallBlock(ContentBlock):
    type: str = "capability_call"
    call_id: str = ""
    capability_id: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityResultBlock(ContentBlock):
    type: str = "capability_result"
    call_id: str = ""
    capability_id: str = ""
    ok: bool = True
    output: str = ""
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    ref_uri: str | None = None


@dataclass(frozen=True)
class Message:
    id: str
    role: Role
    blocks: tuple[ContentBlock, ...]
    created_at: datetime
    provenance: Provenance
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        parts: list[str] = []
        for b in self.blocks:
            if isinstance(b, (TextBlock, ReasoningBlock)) and getattr(b, "text", None):
                parts.append(b.text)
            elif isinstance(b, CapabilityResultBlock) and getattr(b, "output", None):
                parts.append(b.output)
            elif isinstance(b, CapabilityCallBlock):
                if getattr(b, "call_id", None):
                    parts.append(f"capability_call:{b.call_id}")
        return "\n".join(parts)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "Role",
    "SourceType",
    "TrustClass",
    "Provenance",
    "ContentBlock",
    "TextBlock",
    "ReasoningBlock",
    "ImageBlock",
    "AudioBlock",
    "FileRefBlock",
    "ArtifactRefBlock",
    "CapabilityCallBlock",
    "CapabilityResultBlock",
    "Message",
    "utcnow",
]