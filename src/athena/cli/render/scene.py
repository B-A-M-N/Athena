"""Bounded ANSI projection of the shared OI scene.

The Pillow/Glass renderer and this ANSI renderer receive the same
``ProjectionState`` and ``OIScene``.  This module only changes the medium: it
does not own task state and it never lets Buddy overwrite scene text.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterable, Sequence

from athena.cli.activity import VisualActionKind
from athena.cli.projection import OperationNode, ProjectionState
from athena.cli.render.ansi import cell_width, fit_cells
from athena.cli.scene import OIScene
from athena.cli.terminal import sanitize_terminal_text


def _glyph(state: str) -> str:
    return {
        "complete": "✓",
        "success": "✓",
        "failed": "!",
        "failure": "!",
        "approval": "?",
        "running": "●",
        "interrupted": "!",
    }.get(state, "·")


def _entity_marker(status: object) -> str:
    return _glyph(str(status))


def _wrap(value: object, width: int) -> list[str]:
    width = max(int(width), 1)
    text = sanitize_terminal_text(value)
    result: list[str] = []
    for raw in text.splitlines() or [""]:
        if not raw:
            result.append("")
            continue
        # textwrap supplies sensible word boundaries; fit_cells performs the
        # final cell-width correction for CJK/emoji and other wide glyphs.
        result.extend(
            textwrap.wrap(
                raw,
                width=width,
                replace_whitespace=False,
                drop_whitespace=True,
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [""]
        )
    return result or [""]


class _CellCanvas:
    """Small cell-aware canvas used to keep overlays collision-safe."""

    def __init__(self, width: int, height: int) -> None:
        self.width = max(int(width), 1)
        self.height = max(int(height), 1)
        self._cells: list[list[str | None]] = [[None] * self.width for _ in range(self.height)]

    def put(self, row: int, column: int, value: object, *, overwrite: bool = True) -> bool:
        if not 0 <= row < self.height:
            return False
        text = sanitize_terminal_text(value).replace("\n", " ")
        column = max(int(column), 0)
        if column >= self.width:
            return False
        cursor = column
        cells: list[tuple[str, int]] = []
        for char in text:
            width = cell_width(char)
            if width <= 0:
                continue
            if cursor + width > self.width:
                break
            cells.append((char, width))
            cursor += width
        if not cells:
            return False
        occupied = column
        if not overwrite and any(
            self._cells[row][occupied + offset] is not None
            for _, width in cells
            for offset in range(width)
        ):
            return False
        cursor = column
        for char, width in cells:
            for offset in range(width):
                self._cells[row][cursor + offset] = char if offset == 0 else ""
            cursor += width
        return True

    def can_put(self, row: int, column: int, values: Sequence[str]) -> bool:
        if row < 0 or row + len(values) > self.height:
            return False
        padding = 2
        for offset, value in enumerate(values):
            text = sanitize_terminal_text(value).replace("\n", " ")
            if column < 0 or column >= self.width:
                return False
            used = 0
            for char in text:
                width = cell_width(char)
                if width <= 0:
                    continue
                used += width
                if column + used > self.width:
                    break
            left = max(column - padding, 0)
            right = min(column + used + padding, self.width)
            if any(self._cells[row + offset][index] is not None for index in range(left, right)):
                return False
            if column + used > self.width:
                return False
        return True

    def lines(self) -> list[str]:
        return [
            fit_cells("".join(" " if cell is None else cell for cell in row), self.width)
            for row in self._cells
        ]


def _operation_lines(operation: OperationNode | None) -> list[str]:
    if operation is None:
        return ["ACTIVE OPERATION", "· no capability is running"]
    lines = [
        "ACTIVE OPERATION",
        f"{_glyph(operation.state)} {operation.label}  {operation.state.upper()}",
    ]
    if operation.target:
        lines.append(f"target  {operation.target}")
    if operation.command:
        lines.append(f"> {operation.command}")
    if operation.progress:
        lines.append(f"progress  {operation.progress}")
    lines.extend(f"stderr  {item}" for item in list(operation.error)[-2:])
    lines.extend(f"stdout  {item}" for item in list(operation.output)[-2:])
    if operation.detail and operation.state in {"failed", "blocked", "approval"}:
        lines.append(f"detail  {operation.detail}")
    if operation.artifact:
        lines.append(f"artifact  {operation.artifact}")
    return lines


def _action_lines(
    state: ProjectionState,
    scene: OIScene,
    *,
    width: int,
    height: int,
    buddy_lines: Iterable[str],
    buddy_enabled: bool,
) -> list[str] | None:
    """Render material for an active action when a specialized view exists."""
    if scene.mode not in {
        VisualActionKind.CODE,
        VisualActionKind.TEST,
        VisualActionKind.VERIFY,
        VisualActionKind.FAILURE,
        VisualActionKind.SEARCH,
        VisualActionKind.APPROVAL,
        VisualActionKind.RECOVER,
        VisualActionKind.GENERATE,
    }:
        return None
    # A task-level failure without structured diagnostics still belongs in the
    # ordinary activity/history view.  The specialized result scene is for an
    # actual mismatch payload, so it never hides useful recent activity.
    if scene.mode is VisualActionKind.FAILURE and not scene.diagnostics:
        return None
    if scene.mode is VisualActionKind.CODE and scene.code_view is None:
        return None
    canvas = _CellCanvas(width, height)
    operation = state.operations.get(state.active_operation_id or "")
    if operation is None and state.last_operation_id:
        operation = state.operations.get(state.last_operation_id)
    label = operation.target if operation else (operation.label if operation else "workspace")
    titles = {
        VisualActionKind.CODE: f"CODE // {label}",
        VisualActionKind.TEST: f"TESTING // {label}",
        VisualActionKind.VERIFY: f"VERIFYING // {label}",
        VisualActionKind.FAILURE: "RESULT // MISMATCH DETECTED",
        VisualActionKind.SEARCH: "SEARCHING // SYMBOL GRAPH",
        VisualActionKind.APPROVAL: "APPROVAL // OPERATION SCOPE",
        VisualActionKind.RECOVER: "RECOVERING // RETAINED EVIDENCE",
        VisualActionKind.GENERATE: "GENERATING // CAPABILITY",
    }
    canvas.put(0, 0, titles[scene.mode])
    if scene.mode is VisualActionKind.TEST and operation is not None:
        canvas.put(
            1,
            0,
            f"ACTIVE OPERATION  {operation.label}  {operation.state.upper()}",
        )
    canvas.put(0, max(width // 2, 1), f"{state.status} · {scene.mode.value.upper()}")

    if scene.mode is VisualActionKind.CODE and scene.code_view is not None:
        view = scene.code_view
        canvas.put(1, 0, f"{view.language}  {view.mutation_state.upper() or 'PROPOSED'}")
        source = view.diff_hunks or tuple(view.lines)
        available = max(height - 5, 1)
        for row, line in enumerate(source[:available], 2):
            marker = "" if line.startswith(("+", "-", "@")) else " "
            canvas.put(row, 0, f"{marker}{row - 1:>4} {line}")
        if view.preview_truncated:
            canvas.put(min(height - 2, available + 2), 0, "… preview bounded for display")
    elif scene.mode is VisualActionKind.FAILURE:
        row = 2
        for diagnostic in scene.diagnostics[: max(height - 4, 1)]:
            message = (
                diagnostic.get("message")
                or diagnostic.get("detail")
                or diagnostic.get("error")
                or str(diagnostic)
            )
            location = (
                diagnostic.get("path") or diagnostic.get("file") or diagnostic.get("location") or ""
            )
            canvas.put(row, 0, f"! {location} {message}".strip())
            row += 1
    elif scene.mode is VisualActionKind.VERIFY:
        checks = scene.verification_checks
        for row, check in enumerate(checks[: max(height - 4, 1)], 2):
            status = str(check.get("status") or "running").casefold()
            glyph = (
                "✓"
                if status in {"passed", "complete", "completed"}
                else "!"
                if status in {"failed", "error"}
                else "●"
            )
            label = check.get("criterion") or check.get("check_id") or "check"
            canvas.put(row, 0, f"{glyph} {label}  {status}")
        if not checks:
            canvas.put(2, 0, "● waiting for verification checks")
    elif scene.mode is VisualActionKind.TEST:
        canvas.put(2, 0, "· impacted tests")
        progress = scene.progress
        if progress.get("determinate") and progress.get("value") is not None:
            total_cells = max(width - 4, 8)
            filled = int(total_cells * min(max(float(progress["value"]), 0.0), 1.0))
            canvas.put(3, 0, "[" + "█" * filled + "░" * (total_cells - filled) + "]")
            canvas.put(4, 0, str(progress.get("label") or ""))
        else:
            canvas.put(3, 0, str(progress.get("label") or "● running tests"))
    elif scene.mode is VisualActionKind.SEARCH:
        for row, entity in enumerate(scene.entities[: max(height - 3, 1)], 2):
            canvas.put(row, 0, f"· {entity.label}")
    elif scene.mode is VisualActionKind.APPROVAL:
        approval = state.pending_approval or {}
        # Keep the operator-facing sentence readable in both the ANSI and
        # retained renderers.  The scene title already carries the all-caps
        # machine label; this line is the actionable human message.
        canvas.put(2, 0, "Approval required")
        label = operation.label if operation else approval.get("capability_id") or "capability"
        canvas.put(3, 0, f"? {label}  PAUSED")
        target = (
            operation.target if operation else approval.get("target") or approval.get("path") or ""
        )
        if target:
            canvas.put(4, 0, f"target  {target}")
        reason = (
            approval.get("reason")
            or approval.get("policy_reason")
            or (operation.detail if operation else "")
        )
        if reason:
            canvas.put(5, 0, f"reason  {reason}")
        scopes = [str(scope) for scope in approval.get("scopes") or ()]
        canvas.put(
            6,
            0,
            f"keys  {' '.join(f'{i}:{scope}' for i, scope in enumerate(scopes, 1)) or '1:allow'} d:deny",
        )
    elif scene.mode is VisualActionKind.RECOVER:
        canvas.put(2, 0, state.status_message)
        canvas.put(3, 0, "· no speculative evidence is promoted during recovery")
    elif scene.mode is VisualActionKind.GENERATE:
        canvas.put(2, 0, "· constructing a bounded generated capability")
        if operation:
            canvas.put(3, 0, f"{operation.label}  {operation.state.upper()}")

    recent_items = list(state.recent)
    if recent_items and height >= 3:
        canvas.put(height - 2, 0, "RECENT ACTIVITY")
        glyph, text = recent_items[-1]
        canvas.put(height - 1, 0, f"{glyph} {text}")

    art = [
        fit_cells(sanitize_terminal_text(line), min(width, 20)).rstrip()
        for line in buddy_lines
        if line
    ]
    if buddy_enabled and art and height >= 8 and width >= 20:
        fx, fy = scene.anchors.get(scene.buddy_anchor, scene.anchors["center"])
        target_x = int((width - 1) * fx) - 5
        target_y = int((height - 1) * fy) - 1
        candidates = [
            (max(0, target_y + dy), max(0, target_x + dx))
            for distance in range(0, max(width, height))
            for dy, dx in ((0, distance), (0, -distance), (distance, 0), (-distance, 0))
        ]
        for row, column in candidates:
            if canvas.can_put(row, column, art):
                for offset, line in enumerate(art):
                    canvas.put(row + offset, column, line, overwrite=False)
                break
    return canvas.lines()


def render_scene_lines(
    state: ProjectionState,
    scene: OIScene,
    *,
    width: int,
    height: int,
    buddy_lines: Iterable[str] = (),
    buddy_enabled: bool = True,
    recent: Iterable[tuple[str, str]] | None = None,
) -> list[str]:
    """Render one fixed OI aperture without corrupting its labels.

    Buddy is tried at its semantic anchor and then moved to the nearest free
    cell region.  If a viewport is too small for the art, only the stable
    textual scene remains; no overlay is allowed to damage operation data.
    """
    width, height = max(int(width), 1), max(int(height), 1)
    action = _action_lines(
        state,
        scene,
        width=width,
        height=height,
        buddy_lines=buddy_lines,
        buddy_enabled=buddy_enabled,
    )
    if action is not None:
        return action
    canvas = _CellCanvas(width, height)
    split = max(width // 2, 18)
    canvas.put(0, 0, "WORKSPACE MAP")
    if split < width:
        canvas.put(0, split, "RUNTIME GRAPH")

    resources = [
        entity for entity in scene.entities if entity.kind in {"resource", "research", "artifact"}
    ]
    tree = tuple(f"{_entity_marker(entity.status)} {entity.label}" for entity in resources[:8]) or (
        "· no workspace resources observed",
    )
    for row, line in enumerate(tree, 1):
        canvas.put(row, 0, line)

    graph_x = min(max(split + 4, 21), max(width - 1, 0))
    runtime_entities = [
        entity
        for entity in scene.entities
        if entity.kind in {"operation", "child_task", "workflow", "verification", "generated_tool"}
    ]
    if runtime_entities:
        for index, entity in enumerate(runtime_entities[:6]):
            row = 1 + index * 2
            offset = 2 if index % 2 == 0 else -2
            canvas.put(row, graph_x + offset, f"{_entity_marker(entity.status)} {entity.label}")
            if index and not entity.metadata.get("parent_id"):
                canvas.put(row - 1, graph_x, "│")
    else:
        canvas.put(1, graph_x, "· no runtime operations observed")

    # Very small stream windows are useful for ``oi-stream`` and PTY smoke
    # tests; prioritize the live data instead of stacking a dashboard into
    # six rows.
    if height <= 11:
        canvas.put(1, 0, f"{state.status} · {state.status_message}")
        stream = list(state.stream)[-3:]
        if state.stream_partial:
            stream.append(state.stream_partial)
        for row, line in enumerate(stream[-max(height - 3, 1) :], 2):
            canvas.put(row, 0, line)
    else:
        active = state.operations.get(state.active_operation_id or "")
        attention_y = min(max(11, height // 2), max(height - 10, 0))
        active_lines = _operation_lines(active)
        for row, line in enumerate(active_lines[: max(height - attention_y, 1)], attention_y):
            canvas.put(row, 0, line, overwrite=False)

        next_y = attention_y + len(active_lines) + 1
        approval = state.pending_approval
        if approval:
            label = active.label if active else approval.get("capability_id") or "capability"
            approval_lines = [
                "APPROVAL REQUIRED",
                f"? {label}  PAUSED",
            ]
            target = (
                active.target
                if active
                else approval.get("target")
                or approval.get("resource")
                or approval.get("path")
                or ""
            )
            if target:
                approval_lines.append(f"target  {target}")
            reason = (
                approval.get("reason")
                or approval.get("policy_reason")
                or (active.detail if active else "")
            )
            if reason:
                approval_lines.extend(
                    f"reason  {line}" for line in _wrap(reason, max(width - 9, 1))[:2]
                )
            scopes = [str(scope) for scope in approval.get("scopes") or ()]
            approval_lines.append(
                f"keys  {' '.join(f'{i}:{scope}' for i, scope in enumerate(scopes, 1)) or '1:allow'} d:deny"
            )
            for row, line in enumerate(approval_lines[: max(height - next_y, 0)], next_y):
                canvas.put(row, 0, line, overwrite=False)
            next_y += len(approval_lines) + 1

        history_y = min(next_y, max(height - 5, 0))
        canvas.put(history_y, 0, "OPERATION HISTORY", overwrite=False)
        history = [
            operation
            for operation in reversed(list(state.operations.values()))
            if operation.id != state.active_operation_id
        ][:2]
        if history:
            for row, operation in enumerate(history, history_y + 1):
                line = f"{_glyph(operation.state)} {operation.label}  {operation.state.upper()}"
                if operation.artifact:
                    line += f" · artifact {operation.artifact}"
                canvas.put(row, 0, line, overwrite=False)
        else:
            canvas.put(history_y + 1, 0, "· no completed operations", overwrite=False)

        recent_items = list(recent if recent is not None else state.recent)
        recent_y = min(height - 3, max(history_y + 3, 0))
        canvas.put(recent_y, 0, "RECENT ACTIVITY", overwrite=False)
        if recent_items:
            glyph, text = recent_items[-1]
            canvas.put(recent_y + 1, 0, f"{glyph} {text}", overwrite=False)

        stream = list(state.stream)[-2:]
        if state.stream_partial:
            stream.append(state.stream_partial)
        if stream and height >= 4:
            trace_y = max(height - 3, 0)
            canvas.put(trace_y, 0, "LIVE TRACE", overwrite=False)
            for row, line in enumerate(stream[-2:], trace_y + 1):
                canvas.put(row, 0, f"│ {line}", overwrite=False)

    art = [sanitize_terminal_text(line) for line in buddy_lines]
    art = [fit_cells(line, min(width, 20)).rstrip() for line in art if line]
    if buddy_enabled and art and height >= 8 and width >= 20:
        fx, fy = scene.anchors.get(scene.buddy_anchor, scene.anchors["center"])
        target_x = int((width - 1) * fx) - 5
        target_y = int((height - 1) * fy) - 1
        candidates = [
            (target_y + dy, target_x + dx)
            for distance in range(0, max(width, height))
            for dy, dx in ((0, distance), (0, -distance), (distance, 0), (-distance, 0))
        ]
        for row, column in candidates:
            row, column = max(0, row), max(0, column)
            if canvas.can_put(row, column, art):
                for offset, line in enumerate(art):
                    canvas.put(row + offset, column, line, overwrite=False)
                break
    return canvas.lines()


__all__ = ["render_scene_lines"]
