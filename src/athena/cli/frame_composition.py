"""Stateless dual-pane text and scene composition.

This module reads canonical presentation state and terminal-buffer snapshots.
It does not reduce events, own lifecycle state, or execute operator actions.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from typing import Any

from athena.presentation.ansi import fit_cells
from athena.presentation.ansi_scene import render_scene_lines
from athena.presentation.projection import ProjectionState
from athena.presentation.text import sanitize_terminal_text


class DualPaneFrameComposer:
    """Compose bounded pane lines from explicit presentation inputs."""

    def __init__(
        self,
        projection: ProjectionState,
        window_snapshot: Callable[[int, int], list[str]],
    ) -> None:
        self.projection = projection
        self._window_snapshot = window_snapshot

    @staticmethod
    def fit(text: Any, width: int) -> str:
        width = max(int(width), 0)
        text = sanitize_terminal_text(text).replace("\n", " ")
        return fit_cells(text, width)

    @classmethod
    def wrap(cls, text: str, width: int) -> list[str]:
        width = max(width, 1)
        text = sanitize_terminal_text(text)
        result: list[str] = []
        for raw in text.splitlines() or [""]:
            if not raw:
                result.append("")
                continue
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

    def left_lines(
        self,
        height: int,
        width: int,
        *,
        model_text: str,
        details: bool,
        left_scroll: int,
    ) -> list[str]:
        lines = ["CHAT LOG", "─" * min(width, 18)]
        for entry in self.projection.chat:
            label = "YOU" if entry["role"] == "user" else "ATHENA"
            wrapped = self.wrap(entry["text"], max(width - 9, 1))
            for index, text in enumerate(wrapped):
                lines.append(f"{label:<7} {text}" if index == 0 else f"{'':7} {text}")
            lines.append("")
        if model_text:
            lines.append("ATHENA  responding…")
            for text in self.wrap(model_text, max(width - 9, 1)):
                lines.append(f"{'':7} {text}")
        elif self.projection.thinking:
            lines.append("ATHENA  thinking…")
            if details:
                lines.extend(
                    [
                        "         ┌ reasoning ─────────────────",
                        "         │ provider reasoning is active",
                        "         └───────────────────────────",
                    ]
                )
            else:
                lines.append("         (details hidden · /details to expand)")
        stop = len(lines) - left_scroll if left_scroll else len(lines)
        visible = lines[max(0, stop - height) : stop]
        return [self.fit(line, width) for line in visible] + [
            "" for _ in range(max(0, height - len(visible)))
        ]

    @staticmethod
    def glyph(state: str) -> str:
        return {
            "complete": "✓",
            "success": "✓",
            "failed": "!",
            "failure": "!",
            "approval": "?",
            "running": "●",
            "interrupted": "!",
        }.get(state, "·")

    def operation_history_lines(self) -> list[str]:
        """Return compact completed/parked operation rows."""
        rows = ["OPERATION HISTORY"]
        history = [
            operation
            for operation in reversed(list(self.projection.operations.values()))
            if operation.id != self.projection.active_operation_id
        ][:4]
        if not history:
            rows.append("· no completed operations")
            return rows
        for operation in history:
            line = f"{self.glyph(operation.state)} {operation.label}  {operation.state.upper()}"
            if operation.target:
                line += f" · {operation.target}"
            if operation.artifact:
                line += f" · artifact {operation.artifact}"
            rows.append(line)
        return rows

    def active_lines(
        self,
        height: int,
        width: int,
        *,
        details: bool,
        right_scroll: int,
    ) -> list[str]:
        active_lines: list[str] = []
        active = self.projection.operations.get(self.projection.active_operation_id or "")
        if active:
            active_lines.extend(
                [
                    "ACTIVE OPERATION",
                    f"{self.glyph(active.state)} {active.label}  {active.state.upper()}",
                ]
            )
            if active.target:
                active_lines.append(f"target  {active.target}")
            if active.command:
                active_lines.append(f"> {active.command}")
            if active.progress:
                active_lines.append(f"progress  {active.progress}")
            active_lines.extend(f"stderr  {item}" for item in list(active.error)[-2:])
            active_lines.extend(f"stdout  {item}" for item in list(active.output)[-2:])
            if active.artifact:
                active_lines.append(f"artifact  {active.artifact}")
        else:
            active_lines.extend(["ACTIVE OPERATION", "· no capability is running"])

        approval_lines: list[str] = []
        pending_approvals = self.projection.ordered_pending_approvals()
        if pending_approvals:
            approval_lines.extend(["", f"APPROVAL REQUIRED ({len(pending_approvals)})"])
            for index, approval in enumerate(pending_approvals):
                approval_id = approval.get("approval_id") or approval.get("id") or "?"
                label = (
                    active.label
                    if index == 0 and active
                    else approval.get("capability_id") or "capability"
                )
                approval_lines.append(f"? {approval_id}  {label}  PAUSED")
                target = (
                    active.target
                    if index == 0 and active
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
                    or (active.detail if index == 0 and active else "")
                )
                if reason:
                    approval_lines.append(f"reason  {reason}")
                scopes = [str(scope) for scope in approval.get("scopes") or ()]
                choices = " ".join(
                    f"{scope_index}:{scope}" for scope_index, scope in enumerate(scopes, 1)
                )
                approval_lines.append(f"keys  {choices or '1:allow'} d:deny")
                approval_lines.append("paused · choose a scope")

        secondary: list[str] = [""]
        secondary.extend(self.operation_history_lines())
        secondary.extend(["", "RECENT ACTIVITY"])
        secondary.extend(f"{glyph} {text}" for glyph, text in list(self.projection.recent)[-6:])
        if details and self.projection.maintenance:
            secondary.extend(["", "LEARNING / MAINTENANCE"])
            secondary.extend(
                f"{glyph} {text}" for glyph, text in list(self.projection.maintenance)[-6:]
            )
        secondary.extend(["", "LIVE STREAM"])
        for item in self._window_snapshot(min(5, max(height, 1)), max(width - 2, 1))[-5:]:
            if item.strip():
                secondary.append(f"│ {item}")

        def expanded(lines: list[str]) -> list[str]:
            output: list[str] = []
            for line in lines:
                output.extend(self.wrap(line, width))
            return output

        critical = expanded(active_lines + approval_lines)
        tail = expanded(secondary)
        if right_scroll:
            all_lines = critical + tail
            stop = len(all_lines) - right_scroll
            visible = all_lines[max(0, stop - height) : stop]
        elif len(critical) >= height:
            visible = critical[:height]
        else:
            visible = critical + tail[-(height - len(critical)) :]
        return [self.fit(line, width) for line in visible] + [
            "" for _ in range(max(0, height - len(visible)))
        ]

    def scene_lines(
        self,
        height: int,
        width: int,
        *,
        scene: Any,
        mascot: Any,
        mascot_enabled: bool,
    ) -> list[str]:
        return render_scene_lines(
            self.projection,
            scene,
            width=width,
            height=height,
            buddy_lines=(
                [f"BUDDY · {mascot.state.upper()}"] + mascot.render(max_width=min(20, width))
                if mascot_enabled
                else ()
            ),
            buddy_enabled=mascot_enabled,
            recent=self.projection.recent,
        )

    def right_lines(
        self,
        height: int,
        width: int,
        *,
        display: str,
        right_scroll: int,
        details: bool,
        scene: Any,
        mascot: Any,
        mascot_enabled: bool,
    ) -> list[str]:
        if display == "glass":
            return [""] * height
        if right_scroll:
            return self.active_lines(
                height,
                width,
                details=details,
                right_scroll=right_scroll,
            )
        return self.scene_lines(
            height,
            width,
            scene=scene,
            mascot=mascot,
            mascot_enabled=mascot_enabled,
        )
