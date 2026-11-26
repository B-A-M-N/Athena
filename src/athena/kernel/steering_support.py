"""Pending steering materialization for the kernel's safe reasoning boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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

__all__ = ["SteeringSupport"]


class SteeringSupport:
    """Persist queued operator/parent steering as canonical user turns."""

    def __init__(
        self,
        *,
        store: Any,
        messages: Any,
        emit: Callable[..., Awaitable[None]],
    ) -> None:
        self._store = store
        self._messages = messages
        self._emit = emit

    async def apply(self, task: Any) -> None:
        if self._store is None or not task.session_id:
            return
        for item in await self._store.list_pending(task.id):
            message_id = f"msg_steer_{item['id']}"
            source = str(item.get("source") or "").strip().lower()
            if not source:
                source = "parent_task" if item.get("source_task_id") else "operator"
            if source == "parent_task":
                source_type, trust, prefix = (
                    SourceType.TASK,
                    TrustClass.AGENT_CURATED,
                    "[Parent-task steering for the current task; consider it at this reasoning boundary]\n",
                )
            elif source == "system":
                source_type, trust, prefix = (
                    SourceType.SYSTEM,
                    TrustClass.AUTHORITY,
                    "[System steering for the current task]\n",
                )
            else:
                source_type, trust, prefix = (
                    SourceType.USER,
                    TrustClass.USER_CONTENT,
                    "[Operator steering for the current task; consider it at this reasoning boundary]\n",
                )
            now = utcnow()
            message = Message(
                id=message_id,
                role=Role.USER,
                blocks=(
                    TextBlock(
                        text=prefix + str(item["text"]),
                        provenance=Provenance(
                            source_type=source_type,
                            source_id=str(item["id"]),
                            trust=trust,
                            scope=f"task:{task.id}",
                            created_at=now,
                        ),
                    ),
                ),
                created_at=now,
                provenance=Provenance(
                    source_type=source_type,
                    source_id=str(item["id"]),
                    trust=trust,
                    scope=f"task:{task.id}",
                    created_at=now,
                ),
                metadata={
                    "session_id": task.session_id,
                    "task_id": task.id,
                    "steering_id": item["id"],
                    "source_task_id": item.get("source_task_id"),
                    "source": source,
                },
            )
            await self._messages.append_user_turn(task.session_id, message)
            await self._store.mark_consumed(item["id"])
            await self._emit(
                "TaskSteered",
                {
                    "steering_id": item["id"],
                    "principal_id": item["principal_id"],
                    "source_task_id": item.get("source_task_id"),
                },
                task,
            )
