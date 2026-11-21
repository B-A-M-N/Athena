"""Canonical durable user-turn persistence for task intake."""

from __future__ import annotations

from typing import Any

from athena.protocol.messages import (
    Message,
    Provenance,
    Role,
    SourceType,
    TextBlock,
    TrustClass,
    utcnow,
)

__all__ = ["UserTurnSupport"]


class UserTurnSupport:
    """Append one user-authored causal turn before a task is enqueued."""

    def __init__(self, message_store: Any) -> None:
        self._message_store = message_store

    def _store(self) -> Any:
        return self._message_store() if callable(self._message_store) else self._message_store

    async def record(self, request: Any, task: Any) -> None:
        message_store = self._store()
        if message_store is None or not task.session_id:
            raise RuntimeError(
                f"task {task.id!r} cannot enter the queue without a durable session/message store"
            )
        blocks: list[Any] = [
            TextBlock(
                text=str(getattr(request, "prompt", None) or getattr(request, "objective", "")),
                provenance=Provenance(
                    source_type=SourceType.USER,
                    source_id=task.id,
                    trust=TrustClass.USER_CONTENT,
                    scope="session",
                ),
            )
        ]
        # ``AgentRequest.attachments`` is ephemeral. CREATED-task recovery
        # receives only the durable TaskSpec, whose context_refs are the
        # canonical attachment representation. Prefer the request for the
        # initial write, but always fall back to those refs after a restart.
        from athena.service.intake import attachment_blocks

        attachments = getattr(request, "attachments", None)
        if not attachments:
            attachments = getattr(task, "context_refs", ())
        blocks.extend(attachment_blocks(attachments))
        message = Message(
            id=f"msg_user_{task.id}",
            role=Role.USER,
            blocks=tuple(blocks),
            created_at=utcnow(),
            provenance=Provenance(
                source_type=SourceType.USER,
                source_id=task.id,
                trust=TrustClass.USER_CONTENT,
                scope="session",
            ),
            metadata={
                "session_id": task.session_id,
                "task_id": task.id,
                "message_kind": "user_turn",
                "canonical_user_turn": True,
            },
        )
        append_user_turn = getattr(message_store, "append_user_turn", None)
        if append_user_turn is not None:
            await append_user_turn(task.session_id, message)
        else:
            await message_store.append_to_session(task.session_id, message)
