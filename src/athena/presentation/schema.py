"""Versioned presentation DTO facts shared by hosted and native surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from athena.protocol.failure import FailureInfo

# Keep this value aligned with the Rust read-only decoder in
# ``native/src/main.rs``.  The bridge is a transport seam, not a second event
# schema: unsupported versions must fail visibly at that seam.
NATIVE_BRIDGE_SCHEMA_VERSION = 5
LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ProjectionFailure:
    """Structured failure envelope carried by a projection frame."""

    reason: str = ""
    stage: str = ""
    kind: str = ""
    code: str = ""
    fatal: bool = False

    def as_dict(self) -> ProjectionFailurePayload:
        return {
            "reason": self.reason,
            "stage": self.stage,
            "kind": self.kind,
            "code": self.code,
            "fatal": self.fatal,
        }


class ProjectionFailurePayload(TypedDict):
    """JSON shape of the structured failure envelope in a bridge frame."""

    reason: str
    stage: str
    kind: str
    code: str
    fatal: bool


class ProjectionFrame(TypedDict, total=False):
    """Versioned Python-side DTO emitted to the native deserializer."""

    schema_version: int
    title: str
    conversation: list[dict[str, Any]]
    status: str
    status_message: str
    failure: ProjectionFailurePayload
    runtime_state_lost: bool
    runtime_recovery: dict[str, Any]
    self_host_phase: str
    visual_mode: str
    semantic_state: str
    system_status: str
    network_status: str
    activity_status: str
    buddy: dict[str, Any]
    active_operation: dict[str, Any] | None
    current_action: dict[str, Any]
    code_view: dict[str, Any] | None
    diagnostics: list[dict[str, Any]]
    attention_items: list[dict[str, Any]]
    instruments: list[dict[str, Any]]
    verification: dict[str, Any]
    progress: dict[str, Any]
    model_request: dict[str, Any]
    workspace_tree: list[dict[str, Any]]
    runtime_tree: list[dict[str, Any]]
    trace: list[str]
    stream_tail: list[str]
    view: dict[str, Any]
    workspace_entities: list[dict[str, Any]]
    runtime_entities: list[dict[str, Any]]
    oi: list[str]
    entities: list[dict[str, Any]]
    alerts: list[str]
    layout: dict[str, Any]
    navigation: dict[str, Any]


def failure_envelope(
    reason: object = "",
    stage: object = "",
    *,
    kind: object = "",
    code: object = "",
    fatal: bool = False,
) -> ProjectionFailure:
    """Serialize explicit failure facts without classifying message text."""
    info = FailureInfo(
        message=str(reason or ""),
        stage=str(stage or ""),
        kind=str(kind or ""),
        code=str(code or ""),
        fatal=fatal,
    )
    return ProjectionFailure(
        reason=info.message,
        stage=info.stage,
        kind=info.kind,
        code=info.code,
        fatal=info.fatal,
    )


__all__ = [
    "LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION",
    "NATIVE_BRIDGE_SCHEMA_VERSION",
    "ProjectionFailurePayload",
    "ProjectionFrame",
    "ProjectionFailure",
    "FailureInfo",
    "failure_envelope",
]
