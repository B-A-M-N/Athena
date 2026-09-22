"""Durable message-construction helpers for kernel run finalization."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.messages import (
    CapabilityCallBlock,
    Message,
    Provenance,
    Role,
    SourceType,
    TrustClass,
)
from athena.protocol.models import ModelResponse
from athena.protocol.tasks import TaskSpec

_logger = logging.getLogger("athena.kernel")


def assistant_message(task: TaskSpec, response: ModelResponse) -> Message:
    """Build the durable assistant message preserving ALL blocks."""
    blocks = tuple(response.blocks or ())
    metadata: dict[str, Any] = {"task_id": task.id}
    if task.session_id:
        metadata["session_id"] = task.session_id
    try:
        from athena.models.compat.caching import InferenceReceipt

        receipt = InferenceReceipt(
            call_id=response.request_id,
            provider_profile_id=str(
                response.metadata.get("provider_profile_id", response.provider)
            ),
            model_id=response.model,
            response_id=response.metadata.get("response_id"),
            tool_ids=tuple(
                b.call_id
                for b in response.blocks
                if isinstance(b, CapabilityCallBlock) and b.call_id
            ),
            provider_metadata=dict(response.metadata),
            usage=(dict(vars(response.usage)) if response.usage is not None else {}),
        )
        metadata["inference_receipt"] = receipt.to_dict()
    except Exception as exc:
        _logger.warning("could not build inference receipt: %s", exc)
    if response.request_id:
        identity = hashlib.sha256(
            f"assistant-response\0{task.id}\0{response.request_id}".encode("utf-8")
        ).hexdigest()[:32]
        message_id = f"msg_assistant_{identity}"
        metadata["response_identity"] = f"{task.id}:{response.request_id}"
    else:
        message_id = new_id("msg")
    return Message(
        id=message_id,
        role=Role.ASSISTANT,
        blocks=blocks,
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.GENERATED, trust=TrustClass.AGENT_CURATED),
        metadata=metadata,
    )


def results_message(task: TaskSpec, blocks) -> Message:
    return Message(
        id=new_id("msg"),
        role=Role.CAPABILITY,
        blocks=tuple(blocks),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.CAPABILITY),
        metadata={
            "task_id": task.id,
            **({"session_id": task.session_id} if task.session_id else {}),
        },
    )
