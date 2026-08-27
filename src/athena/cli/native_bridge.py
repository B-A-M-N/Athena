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
) -> dict[str, Any]:
    """Build one native-terminal frame from canonical projection state."""
    width = max(int(width), 1)
    height = max(int(height), 1)
    scene = build_oi_scene(state, Rect(0, 0, width, height))
    entities: list[dict[str, Any]] = []
    for entity in scene.entities:
        parent_id = entity.metadata.get("parent_id")
        entities.append({
            "id": sanitize_terminal_text(entity.id),
            "kind": sanitize_terminal_text(entity.kind),
            "label": sanitize_terminal_text(entity.label),
            "status": sanitize_terminal_text(entity.status),
            "parent_id": sanitize_terminal_text(parent_id) if parent_id else None,
        })
    lines = render_scene_lines(
        state,
        scene,
        width=width,
        height=height,
        recent=state.recent,
        buddy_enabled=False,
    )
    return {
        "title": "ATHENA OI // GLASS COMPUTE",
        "status": sanitize_terminal_text(state.status),
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
