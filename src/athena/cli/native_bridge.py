"""Serialize the shared OI projection for Athena's native terminal.

The Rust frontend is intentionally a read-only terminal/compositor. This
module is the Python-side bridge: it derives a frame from the same
``ProjectionState`` used by the hosted Glass and ANSI surfaces, then emits one
newline-delimited JSON object suitable for ``athena-terminal --bridge-stdin``.
It owns no task state and performs no inference.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, TextIO

from athena.cli.activity import VisualActionKind
from athena.cli.layout import Rect
from athena.cli.projection import ProjectionState
from athena.cli.render.scene import render_scene_lines
from athena.cli.scene import TreeNode, build_oi_scene
from athena.cli.terminal import sanitize_terminal_text

__all__ = ["native_projection_frame", "write_native_projection"]

_ACTION_VIEW_MODES = frozenset(
    {
        VisualActionKind.CODE,
        VisualActionKind.TEST,
        VisualActionKind.VERIFY,
        VisualActionKind.FAILURE,
        VisualActionKind.SEARCH,
        VisualActionKind.APPROVAL,
        VisualActionKind.RECOVER,
        VisualActionKind.GENERATE,
    }
)
NATIVE_BRIDGE_SCHEMA_VERSION = 3


def _instrument_statuses(
    state: ProjectionState,
    active: Any,
) -> tuple[str, str, str]:
    """Expose only statuses supported by canonical projection facts."""
    status = state.status.casefold()
    if state.runtime_state_lost:
        system = "recovery_required"
    elif status in {"failure", "blocked", "error"}:
        system = "error"
    elif "not ready" in status or "unconfigured" in status:
        system = "not_ready"
    elif status in {"warning", "partial", "recovering"}:
        system = "degraded"
    else:
        system = "ready"

    network = "unknown"
    for event_type, payload in reversed(state.raw_events):
        if event_type not in {"NetworkStatusChanged", "EnvironmentReflected"}:
            continue
        candidate = str(
            payload.get("network_status") or payload.get("network_connectivity") or ""
        ).casefold()
        if candidate in {"available", "connected", "restricted", "blocked", "unknown"}:
            network = "available" if candidate == "connected" else candidate
            break

    state_name = str(getattr(active, "state", "") or "").casefold()
    if state.status.casefold() in {"approval", "waiting", "recovering"}:
        activity = "waiting"
    elif state.status.casefold() in {"failure", "blocked"} or state_name in {
        "failed",
        "blocked",
        "error",
    }:
        activity = "error"
    elif state.thinking or state_name in {"running", "active", "executing", "working"}:
        activity = "active"
    else:
        activity = "idle"
    return system, network, activity


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Keep bridge metadata serializable without inventing semantic fields."""
    if depth > 6:
        return sanitize_terminal_text(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            sanitize_terminal_text(key): _json_safe(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:128]]
    return sanitize_terminal_text(value)


def _tree_payload(nodes: tuple[TreeNode, ...]) -> list[dict[str, Any]]:
    """Serialize the normalized forest without dropping parent structure."""

    def encode(node: TreeNode) -> dict[str, Any]:
        return {
            "id": sanitize_terminal_text(node.id),
            "kind": sanitize_terminal_text(node.kind),
            "label": sanitize_terminal_text(node.label),
            "status": sanitize_terminal_text(node.status),
            "metadata": _json_safe(node.metadata),
            "children": [encode(child) for child in node.children],
        }

    return [encode(node) for node in nodes]


def native_projection_frame(
    state: ProjectionState,
    *,
    width: int | None = None,
    height: int | None = None,
    character: str = "owl",
    navigation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one native-terminal frame from canonical projection state."""
    has_viewport = width is not None and height is not None
    viewport_width = max(int(width), 1) if width is not None else 1
    viewport_height = max(int(height), 1) if height is not None else 1
    scene_key = (int(state.projection_revision), viewport_width, viewport_height, character)
    scene_cache = getattr(state, "_native_scene_cache", None)
    if isinstance(scene_cache, tuple) and scene_cache[:1] == (scene_key,):
        scene = scene_cache[1]
    else:
        scene = build_oi_scene(
            state,
            Rect(0, 0, viewport_width, viewport_height),
            character=character,
        )
        # The projection revision covers both reducer events and direct UI mutations.
        # Cache only the immutable scene object; frame dictionaries are still
        # rebuilt per call so callers cannot mutate cached output.
        state._native_scene_cache = (scene_key, scene)
    entities: list[dict[str, Any]] = []
    for entity in scene.entities:
        parent_id = entity.metadata.get("parent_id")
        entities.append(
            {
                "id": sanitize_terminal_text(entity.id),
                "kind": sanitize_terminal_text(entity.kind),
                "label": sanitize_terminal_text(entity.label),
                "status": sanitize_terminal_text(entity.status),
                "parent_id": sanitize_terminal_text(parent_id) if parent_id else None,
            }
        )
    workspace_entities = [
        entity for entity in entities if entity["kind"] in {"resource", "research", "artifact"}
    ]
    runtime_entities = [
        entity for entity in entities if entity["kind"] not in {"resource", "research", "artifact"}
    ]
    lines = (
        render_scene_lines(
            state,
            scene,
            width=viewport_width,
            height=viewport_height,
            recent=state.recent,
            buddy_enabled=False,
        )
        if has_viewport
        else []
    )
    active = state.operations.get(state.active_operation_id or "")
    if active is None and state.last_operation_id:
        active = state.operations.get(state.last_operation_id)
    code_view = scene.code_view
    operation = None
    if active is not None:
        operation = {
            "id": sanitize_terminal_text(active.id),
            "label": sanitize_terminal_text(active.label),
            "capability": sanitize_terminal_text(active.label),
            "operation": sanitize_terminal_text(active.detail),
            "target": sanitize_terminal_text(active.target),
            "state": sanitize_terminal_text(active.state),
            "action_kind": sanitize_terminal_text(active.action_kind),
            "mutation_state": sanitize_terminal_text(active.mutation_state),
            "progress": sanitize_terminal_text(active.progress),
            "progress_value": active.progress_value,
            "progress_determinate": active.progress_determinate,
        }
    serialized_code = None
    if code_view is not None:
        serialized_code = {
            "path": sanitize_terminal_text(code_view.path),
            "language": sanitize_terminal_text(code_view.language),
            "text": sanitize_terminal_text(code_view.text),
            "lines": [sanitize_terminal_text(line) for line in code_view.lines],
            "diff": [sanitize_terminal_text(line) for line in code_view.diff_hunks],
            "visible_start": code_view.visible_start,
            "visible_end": code_view.visible_end,
            "reveal_offset": code_view.reveal_offset,
            "mutation_state": sanitize_terminal_text(code_view.mutation_state),
            "preview_truncated": code_view.preview_truncated,
        }
    query = ""
    for event_type, payload in reversed(state.raw_events):
        if event_type in {"SearchStarted", "ResearchStarted", "FileRead", "InspectionStarted"}:
            query = sanitize_terminal_text(
                payload.get("query")
                or payload.get("path")
                or payload.get("resource")
                or payload.get("uri")
                or ""
            )
            if query:
                break
    action_kind = scene.mode.value
    action_label = sanitize_terminal_text(active.label if active else action_kind.upper())
    action_target = sanitize_terminal_text(active.target if active else query)
    action_detail = sanitize_terminal_text(active.detail if active and active.detail else query)
    current_action = {
        "kind": sanitize_terminal_text(action_kind),
        "label": action_label,
        "target": action_target,
        "detail": action_detail,
        "query": query,
        "progress": sanitize_terminal_text(active.progress if active else ""),
        "progress_value": active.progress_value if active else None,
        "progress_determinate": active.progress_determinate if active else False,
    }
    attention_items: list[dict[str, Any]] = []
    for approval in state.ordered_pending_approvals():
        approval_id = sanitize_terminal_text(
            approval.get("approval_id") or approval.get("id") or "approval"
        )
        target = sanitize_terminal_text(
            approval.get("target")
            or approval.get("path")
            or approval.get("resource")
            or "governed operation"
        )
        reason = sanitize_terminal_text(
            approval.get("reason") or approval.get("policy_reason") or "operator decision required"
        )
        attention_items.append(
            {
                "id": f"approval:{approval_id}",
                "approval_id": approval_id,
                "kind": "approval",
                "severity": "warning",
                "title": "APPROVAL REQUIRED",
                "summary": f"{target} · {reason}",
                "requires_action": True,
                "scopes": [
                    sanitize_terminal_text(scope)
                    for scope in (
                        approval.get("scopes") or approval.get("requested_scope") or ["call"]
                    )
                    if scope
                ],
                "related_object_id": active.id if active else None,
            }
        )
    system_status, network_status, activity_status = _instrument_statuses(state, active)
    if state.runtime_state_lost:
        attention_items.append(
            {
                "id": "runtime:state-lost",
                "kind": "runtime_state_lost",
                "severity": "failure",
                "title": "RUNTIME STATE LOST",
                "summary": sanitize_terminal_text(state.status_message),
                "requires_action": True,
                "recovery": _json_safe(state.runtime_recovery),
                "related_object_id": active.id if active else None,
            }
        )
    for index, (glyph, message) in enumerate(reversed(state.recent)):
        if glyph not in {"!", "?"}:
            continue
        if state.pending_approvals and message.lower().startswith("approval required"):
            continue
        attention_items.append(
            {
                "id": f"event:{state.event_count}:{index}",
                "kind": "notification",
                "severity": "failure" if glyph == "!" else "warning",
                "title": "ATTENTION" if glyph == "?" else "EVENT",
                "summary": sanitize_terminal_text(message),
                "requires_action": False,
                "related_object_id": active.id if active else None,
            }
        )
        if len(attention_items) >= 4:
            break
    frame = {
        "schema_version": NATIVE_BRIDGE_SCHEMA_VERSION,
        "title": "ATHENA OI // GLASS COMPUTE",
        "status": sanitize_terminal_text(state.status),
        "status_message": sanitize_terminal_text(state.status_message),
        "runtime_state_lost": state.runtime_state_lost,
        "runtime_recovery": _json_safe(state.runtime_recovery),
        "self_host_phase": sanitize_terminal_text(state.self_host_phase),
        "semantic_state": sanitize_terminal_text(scene.mode.value),
        "system_status": system_status,
        "network_status": network_status,
        "activity_status": activity_status,
        "buddy": {
            "state": sanitize_terminal_text(scene.mode.value),
            "anchor": sanitize_terminal_text(scene.buddy_anchor),
            "status": sanitize_terminal_text(scene.status),
            "character": sanitize_terminal_text(scene.character),
        },
        "active_operation": operation,
        "current_action": current_action,
        "code_view": serialized_code,
        "diagnostics": [dict(item) for item in scene.diagnostics],
        "attention_items": attention_items,
        "instruments": [
            *[dict(item) for item in scene.instruments],
            {
                "kind": "semantic_runtime_facts",
                "title": "Semantic runtime facts",
                "strategy": sanitize_terminal_text(scene.mode.value),
                "authority": "python_projection",
                "evidence": {
                    "verification_status": sanitize_terminal_text(state.verification_status),
                    "runtime_recovery": _json_safe(state.runtime_recovery),
                    "model_request_status": sanitize_terminal_text(scene.model_request_status),
                },
                "checkpoint": _json_safe(state.runtime_recovery.get("checkpoint"))
                if isinstance(state.runtime_recovery, Mapping)
                else None,
                "backend_passport": _json_safe(state.runtime_recovery.get("backend_passport"))
                if isinstance(state.runtime_recovery, Mapping)
                else None,
            },
        ],
        "verification": {
            "status": sanitize_terminal_text(state.verification_status),
            "checks": [dict(item) for item in scene.verification_checks],
        },
        "progress": dict(scene.progress),
        "model_request": {
            "provider": sanitize_terminal_text(scene.model_provider or ""),
            "model": sanitize_terminal_text(scene.model or ""),
            "role": sanitize_terminal_text(scene.model_role or ""),
            "request_id": sanitize_terminal_text(scene.model_request_id or ""),
            "status": sanitize_terminal_text(scene.model_request_status),
        },
        "workspace_tree": _tree_payload(scene.workspace_tree),
        "runtime_tree": _tree_payload(scene.runtime_tree),
        "trace": [sanitize_terminal_text(item) for item in scene.trace],
        "stream_tail": [sanitize_terminal_text(item) for item in list(state.stream)[-8:]],
        "view": {
            "label": "action" if scene.mode in _ACTION_VIEW_MODES else "overview",
            "mode": sanitize_terminal_text(scene.mode.value),
            # With no active action the right CRT becomes the bounded
            # operation-history view; active modes remain live projection.
            "history": scene.mode is VisualActionKind.IDLE
            or state.status.upper() in {"SUCCESS", "COMPLETE"},
            "history_label": "OI // HISTORY",
            "live_label": "OI // LIVE",
        },
        "workspace_entities": workspace_entities,
        "runtime_entities": runtime_entities,
        "oi": [sanitize_terminal_text(line) for line in lines],
        "entities": entities,
        "alerts": [sanitize_terminal_text(alert) for alert in scene.alerts[-4:]],
    }
    if has_viewport:
        frame["layout"] = {
            "viewport": {
                "x": scene.viewport.x,
                "y": scene.viewport.y,
                "width": scene.viewport.width,
                "height": scene.viewport.height,
            },
            "chrome": {
                "note": "native frontend owns physical placement",
            },
        }
    if navigation is not None:
        frame["navigation"] = _json_safe(navigation)
    return frame


def write_native_projection(
    output: TextIO,
    state: ProjectionState,
    *,
    width: int | None = None,
    height: int | None = None,
    character: str = "owl",
    navigation: Mapping[str, Any] | None = None,
) -> None:
    """Write and flush one bridge frame for the native frontend."""
    frame = native_projection_frame(
        state,
        width=width,
        height=height,
        character=character,
        navigation=navigation,
    )
    output.write(json.dumps(frame, sort_keys=True, ensure_ascii=False) + "\n")
    output.flush()
