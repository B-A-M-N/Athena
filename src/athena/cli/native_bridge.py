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
) -> dict[str, Any]:
    """Build one native-terminal frame from canonical projection state."""
    has_viewport = width is not None and height is not None
    viewport_width = max(int(width), 1) if width is not None else 1
    viewport_height = max(int(height), 1) if height is not None else 1
    scene = build_oi_scene(
        state,
        Rect(0, 0, viewport_width, viewport_height),
        character=character,
    )
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
    frame = {
        "schema_version": 2,
        "title": "ATHENA OI // GLASS COMPUTE",
        "status": sanitize_terminal_text(state.status),
        "semantic_state": sanitize_terminal_text(scene.mode.value),
        "buddy": {
            "state": sanitize_terminal_text(scene.mode.value),
            "anchor": sanitize_terminal_text(scene.buddy_anchor),
            "status": sanitize_terminal_text(scene.status),
            "character": sanitize_terminal_text(scene.character),
        },
        "active_operation": operation,
        "code_view": serialized_code,
        "diagnostics": [dict(item) for item in scene.diagnostics],
        "instruments": [dict(item) for item in scene.instruments],
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
        "view": {
            "label": "action" if scene.mode in _ACTION_VIEW_MODES else "overview",
            "mode": sanitize_terminal_text(scene.mode.value),
            "history": False,
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
    return frame


def write_native_projection(
    output: TextIO,
    state: ProjectionState,
    *,
    width: int | None = None,
    height: int | None = None,
    character: str = "owl",
) -> None:
    """Write and flush one bridge frame for the native frontend."""
    frame = native_projection_frame(
        state,
        width=width,
        height=height,
        character=character,
    )
    output.write(json.dumps(frame, sort_keys=True, ensure_ascii=False) + "\n")
    output.flush()
