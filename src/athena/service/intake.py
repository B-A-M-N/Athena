"""Helpers for reconstructing canonical task-intake messages."""

from __future__ import annotations

from typing import Any, Mapping

from athena.protocol.artifacts import ArtifactRef
from athena.protocol.messages import ArtifactRefBlock, FileRefBlock
from athena.protocol.tasks import ContextRef


def attachment_blocks(attachments: Any) -> list[Any]:
    """Convert request or durable context attachments into message blocks."""
    blocks: list[Any] = []
    for attachment in attachments or ():
        if isinstance(attachment, ArtifactRef) or (
            hasattr(attachment, "uri") and hasattr(attachment, "mime_type")
        ):
            blocks.append(ArtifactRefBlock(uri=str(attachment.uri), ref=attachment))
        elif isinstance(attachment, ContextRef):
            uri = str(attachment.ref or "")
            if not uri:
                continue
            if attachment.kind == "artifact":
                ref = ArtifactRef(
                    id=str(attachment.source_id or uri),
                    uri=uri,
                    hash=attachment.hash,
                    mime_type=attachment.mime_type,
                    size=attachment.size,
                    storage_path=attachment.storage_path,
                    producer=attachment.producer or attachment.summary,
                    metadata=dict(attachment.metadata),
                )
                blocks.append(ArtifactRefBlock(uri=uri, ref=ref))
            else:
                blocks.append(FileRefBlock(uri=uri, mime_type=attachment.mime_type))
        elif isinstance(attachment, Mapping):
            kind = str(attachment.get("kind") or "file")
            uri = str(attachment.get("uri") or attachment.get("ref") or "")
            if not uri:
                continue
            if kind == "artifact":
                ref = ArtifactRef(
                    id=str(attachment.get("source_id") or uri),
                    uri=uri,
                    hash=attachment.get("hash"),
                    mime_type=attachment.get("mime_type"),
                    size=attachment.get("size"),
                    storage_path=attachment.get("storage_path"),
                    producer=attachment.get("producer") or attachment.get("summary"),
                    metadata=dict(attachment.get("metadata") or {}),
                )
                blocks.append(ArtifactRefBlock(uri=uri, ref=ref))
            else:
                blocks.append(FileRefBlock(uri=uri, mime_type=attachment.get("mime_type")))
    return blocks
