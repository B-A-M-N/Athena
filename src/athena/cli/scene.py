"""OI scene graph built from the shared projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from athena.cli.activity import VisualActionKind
from athena.cli.code_view import CodeViewport, make_code_view
from athena.cli.layout import Rect
from athena.cli.projection import ProjectionState
from athena.cli.terminal import sanitize_terminal_text


@dataclass(frozen=True)
class SceneEntity:
    id: str
    kind: str
    label: str
    status: str = "idle"
    anchor: str = "center"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OIScene:
    viewport: Rect
    status: str
    title: str = "ATHENA OI // GLASS COMPUTE"
    character: str = "owl"
    entities: list[SceneEntity] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    stream: list[str] = field(default_factory=list)
    buddy_anchor: str = "center"
    mode: VisualActionKind = VisualActionKind.IDLE
    code_view: CodeViewport | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()
    instruments: tuple[dict[str, Any], ...] = ()
    verification_checks: tuple[dict[str, Any], ...] = ()
    progress: dict[str, Any] = field(default_factory=dict)

    @property
    def anchors(self) -> dict[str, tuple[float, float]]:
        return {
            "files": (0.18, 0.31),
            "graph": (0.78, 0.27),
            "center": (0.50, 0.54),
            "alert": (0.78, 0.72),
            "lower-left": (0.23, 0.78),
            "lower-right": (0.74, 0.82),
        }

    def anchor_position(self, name: str) -> tuple[int, int]:
        fx, fy = self.anchors.get(name, self.anchors["center"])
        return (
            self.viewport.x + int(max(self.viewport.width - 1, 0) * fx),
            self.viewport.y + int(max(self.viewport.height - 1, 0) * fy),
        )


def _buddy_anchor(status: str, mode: VisualActionKind = VisualActionKind.IDLE) -> str:
    if mode in {VisualActionKind.CODE, VisualActionKind.READ, VisualActionKind.SEARCH}:
        return "files"
    if mode in {VisualActionKind.TEST, VisualActionKind.VERIFY, VisualActionKind.EXECUTE}:
        return "graph"
    if mode is VisualActionKind.FAILURE:
        return "alert"
    if mode is VisualActionKind.APPROVAL:
        return "lower-right"
    return {
        "READING": "files",
        "SEARCHING": "files",
        "EXECUTING": "graph",
        "FAILURE": "alert",
        "BLOCKED": "alert",
        "APPROVAL": "lower-right",
        "DELEGATED": "center",
        "RECOVERING": "lower-left",
        "SUCCESS": "lower-right",
    }.get(status, "center")


def _label(value: object) -> str:
    return sanitize_terminal_text(value).strip()


def _event_entity(event_type: str, payload: Mapping[str, Any]) -> SceneEntity | None:
    """Turn observable event payloads into scene entities when they have data."""
    if event_type in {"FileRead", "InspectionStarted", "SearchStarted", "ResearchStarted"}:
        value = (
            payload.get("path")
            or payload.get("resource")
            or payload.get("query")
            or payload.get("uri")
        )
        label = _label(value)
        if label:
            return SceneEntity(
                f"resource:{label}",
                "research" if event_type in {"SearchStarted", "ResearchStarted"} else "resource",
                label,
                "active",
                "files",
                {"event_type": event_type},
            )
    if event_type == "ArtifactCreated":
        value = payload.get("uri") or payload.get("artifact_uri") or payload.get("name")
        label = _label(value)
        if label:
            return SceneEntity(
                f"artifact:{label}", "artifact", label, "complete", "lower-right", {}
            )
    if event_type in {"ChildTaskCreated", "ChildTaskCompleted", "DelegationStarted"}:
        child_id = _label(
            payload.get("child_task_id") or payload.get("task_id") or payload.get("child_id")
        )
        if child_id:
            state = "complete" if event_type == "ChildTaskCompleted" else "active"
            return SceneEntity(
                f"child:{child_id}",
                "child_task",
                child_id,
                state,
                "graph",
                {
                    "parent_id": _label(payload.get("parent_task_id") or payload.get("task_id")),
                    "event_type": event_type,
                },
            )
    if event_type.startswith(("Workflow", "Verification", "Acceptance", "Generated")):
        value = _label(
            payload.get("workflow_id")
            or payload.get("workflow")
            or payload.get("criterion")
            or payload.get("capability")
            or payload.get("name")
        )
        if value:
            kind = (
                "workflow"
                if event_type.startswith("Workflow")
                else (
                    "verification"
                    if event_type.startswith(("Verification", "Acceptance"))
                    else "generated_tool"
                )
            )
            return SceneEntity(
                f"{kind}:{value}",
                kind,
                value,
                _label(payload.get("status")) or "active",
                "graph",
                {"event_type": event_type},
            )
    if event_type == "InstrumentProduced":
        instrument = payload.get("instrument")
        if isinstance(instrument, Mapping):
            value = _label(instrument.get("title") or instrument.get("kind") or "instrument")
            return SceneEntity(
                f"instrument:{_label(instrument.get('id') or value)}",
                "instrument",
                value,
                "complete",
                "lower-right",
                {"instrument": dict(instrument)},
            )
    return None


def build_oi_scene(
    state: ProjectionState,
    viewport: Rect,
    *,
    character: str = "owl",
) -> OIScene:
    """Build a bounded scene entirely from the canonical projection."""
    entities: list[SceneEntity] = []
    seen: set[str] = set()
    for operation in list(state.operations.values())[-6:]:
        metadata = {
            "target": operation.target,
            "command": operation.command,
            "execution_id": operation.execution_id,
            "exit_code": operation.exit_code,
            "progress": operation.progress,
            "artifact": operation.artifact,
        }
        entity = SceneEntity(
            operation.id, "operation", operation.label, operation.state, "graph", metadata
        )
        entities.append(entity)
        seen.add(entity.id)
        if operation.target:
            resource = SceneEntity(
                f"resource:{operation.id}",
                "resource",
                operation.target,
                operation.state,
                "files",
                {"operation_id": operation.id},
            )
            entities.append(resource)
            seen.add(resource.id)
        if operation.artifact:
            artifact = SceneEntity(
                f"artifact:{operation.id}",
                "artifact",
                operation.artifact,
                "complete",
                "lower-right",
                {"operation_id": operation.id},
            )
            entities.append(artifact)
            seen.add(artifact.id)

    for event_type, payload in state.raw_events:
        event_entity = _event_entity(event_type, payload)
        if event_entity is not None and event_entity.id not in seen:
            entities.append(event_entity)
            seen.add(event_entity.id)

    active = state.operations.get(state.active_operation_id or "")
    if active is None and state.last_operation_id:
        active = state.operations.get(state.last_operation_id)
    try:
        mode = VisualActionKind(
            state.semantic_state
            if state.semantic_state != VisualActionKind.IDLE.value
            else active.action_kind
            if active
            else VisualActionKind.IDLE.value
        )
    except ValueError:
        mode = VisualActionKind.IDLE
    if state.status in {"FAILURE", "WARNING", "BLOCKED"}:
        mode = VisualActionKind.FAILURE
    elif state.status == "APPROVAL":
        mode = VisualActionKind.APPROVAL
    elif state.status == "RECOVERING":
        mode = VisualActionKind.RECOVER
    buddy_anchor = _buddy_anchor(state.status, mode)
    if active and active.target and state.status in {"READING", "SEARCHING"}:
        buddy_anchor = "files"
    alerts = [text for glyph, text in list(state.recent)[-4:] if glyph in {"!", "?"}]
    code_view = None
    if active and (active.content_preview or active.diff_preview):
        code_view = make_code_view(
            path=active.target or active.label,
            text=active.content_preview or "\n".join(active.diff_preview),
            mutation_state=active.mutation_state,
            diff_hunks=active.diff_preview,
            preview_truncated=active.preview_truncated,
        )
    progress: dict[str, Any] = (
        {
            "value": active.progress_value,
            "determinate": active.progress_determinate,
            "label": active.progress,
        }
        if active
        else {}
    )
    if state.pending_approval:
        progress["approval"] = {
            key: state.pending_approval[key]
            for key in ("capability_id", "target", "path", "reason", "scopes")
            if state.pending_approval.get(key) is not None
        }
    return OIScene(
        viewport=viewport,
        status=state.status,
        character=sanitize_terminal_text(character).strip().lower() or "owl",
        entities=entities,
        alerts=alerts,
        stream=list(state.stream)[-8:] + ([state.stream_partial] if state.stream_partial else []),
        buddy_anchor=buddy_anchor,
        mode=mode,
        code_view=code_view,
        diagnostics=tuple(active.diagnostics if active else state.diagnostics),
        verification_checks=tuple(state.verification_checks.values()),
        instruments=tuple(state.instruments),
        progress=progress,
    )


__all__ = ["OIScene", "SceneEntity", "build_oi_scene"]
