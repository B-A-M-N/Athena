"""Serialize the shared OI projection for Athena's native terminal.

The Rust frontend is intentionally a read-only terminal/compositor. This
module is the Python-side bridge: it derives a frame from the same
``ProjectionState`` used by the hosted Glass and ANSI surfaces, then emits one
newline-delimited JSON object suitable for ``athena-terminal --bridge-stdin``.
It owns no task state and performs no inference.
"""

from __future__ import annotations

import json
from typing import Any, TextIO

from athena.cli.layout import Rect
from athena.cli.projection import ProjectionState
from athena.cli.render.scene import render_scene_lines
from athena.cli.scene import build_oi_scene
from athena.cli.terminal import sanitize_terminal_text

__all__ = ["native_projection_frame", "write_native_projection"]


def native_projection_frame(
    state: ProjectionState,
    *,
    width: int = 72,
    height: int = 24,
    character: str = "owl",
) -> dict[str, Any]:
    """Build one native-terminal frame from canonical projection state."""
    width = max(int(width), 1)
    height = max(int(height), 1)
    scene = build_oi_scene(state, Rect(0, 0, width, height), character=character)
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
    lines = render_scene_lines(
        state,
        scene,
        width=width,
        height=height,
        recent=state.recent,
        buddy_enabled=False,
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
    return {
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
        "workspace_entities": workspace_entities,
        "runtime_entities": runtime_entities,
        "oi": [sanitize_terminal_text(line) for line in lines],
        "entities": entities,
        "alerts": [sanitize_terminal_text(alert) for alert in scene.alerts[-4:]],
    }


def write_native_projection(
    output: TextIO,
    state: ProjectionState,
    *,
    width: int = 72,
    height: int = 24,
) -> None:
    """Write and flush one bridge frame for the native frontend."""
    frame = native_projection_frame(state, width=width, height=height)
    output.write(json.dumps(frame, sort_keys=True, ensure_ascii=False) + "\n")
    output.flush()
