"""Protocol-neutral external delegate models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.capabilities import EffectClass


class DelegateProtocol(str, enum.Enum):
    ACP = "acp"
    A2A = "a2a"
    OPENAI = "openai"
    JSON_LINES = "json_lines"


@dataclass(frozen=True)
class DelegateSpec:
    id: str
    protocol: DelegateProtocol | str
    command: tuple[str, ...] = ()
    endpoint: str | None = None
    capability_ceiling: tuple[str, ...] = ()
    effect_ceiling: tuple[str, ...] = ()
    allowed_workspace: str | None = None
    timeout_seconds: float = 120.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 128:
            raise ValueError("delegate id must be between 1 and 128 characters")
        if not self.command and not self.endpoint:
            raise ValueError("delegate requires a command or endpoint")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 3600:
            raise ValueError("delegate timeout must be between 0 and 3600 seconds")
        for effect in self.effect_ceiling:
            try:
                EffectClass(effect)
            except ValueError as exc:
                raise ValueError(
                    f"delegate effect ceiling contains unknown effect: {effect}"
                ) from exc

    @property
    def protocol_value(self) -> str:
        return getattr(self.protocol, "value", self.protocol)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "protocol": self.protocol_value,
            "command": list(self.command),
            "endpoint": self.endpoint,
            "capability_ceiling": list(self.capability_ceiling),
            "effect_ceiling": list(self.effect_ceiling),
            "allowed_workspace": self.allowed_workspace,
            "timeout_seconds": self.timeout_seconds,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DelegateSession:
    id: str
    delegate_id: str
    task_id: str
    session_id: str | None
    remote_session_id: str | None
    workspace_root: str
    state: str = "active"
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    launch_signature: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "delegate_id": self.delegate_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "remote_session_id": self.remote_session_id,
            "workspace_root": self.workspace_root,
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "launch_signature": self.launch_signature,
            "metadata": dict(self.metadata),
        }


__all__ = ["DelegateProtocol", "DelegateSession", "DelegateSpec"]
