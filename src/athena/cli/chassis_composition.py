"""Stateless dual-pane chassis, aperture, and control-rail composition."""

from __future__ import annotations

from typing import Any

from athena.presentation.ansi import fit_cells
from athena.presentation.text import sanitize_terminal_text


class DualPaneChassisComposer:
    """Compose the terminal chrome around already-composed pane content."""

    @staticmethod
    def _fit(text: Any, width: int) -> str:
        width = max(int(width), 0)
        text = sanitize_terminal_text(text).replace("\n", " ")
        return fit_cells(text, width)

    def compose(
        self,
        *,
        layout: Any,
        left: list[str],
        right: list[str],
        status: str,
        display: str,
        full_screen: bool,
        oi_enabled: bool,
        model_label: str,
        prompt_text: str,
        right_scroll: int,
        pane_gap: int = 3,
    ) -> list[str]:
        cols, rows = layout.columns, layout.rows
        lines = [" " * cols for _ in range(rows)]
        left_w, right_w = layout.operator.width, layout.oi.width
        lines[0] = self._fit("ATHENA  //  OPERATOR INSTRUMENT", cols)
        lines[1] = self._fit(
            f"STATUS {status:<13}  ·  local-first / event projection  ·  {display.upper()}",
            cols,
        )
        lines[2] = self._fit("─" * max(cols - 4, 1), cols)

        outer_op = layout.outer_operator
        outer_oi = layout.outer_oi
        chassis_top = outer_op.y
        chassis_height = outer_op.height
        chassis_bot = chassis_top + chassis_height - 1
        inner_h = max(chassis_height - 2, 1)
        inner_w_left = max(left_w - 2, 1)
        inner_w_right = max(right_w - 2, 1)
        right_title = "OI // HISTORY" if right_scroll else "ATHENA OI // GLASS COMPUTE"
        cabinet_x = max(layout.operator.x - 1, 0)
        seam = max(int(pane_gap) - 2, 0)
        prefix = " " * cabinet_x
        seam_fill = "░" * max(seam, 1)
        chassis_total_width = outer_op.width + len(seam_fill) + outer_oi.width + 2

        def left_body_row(value: str) -> str:
            return prefix + "│" + self._fit(value, inner_w_left) + "│"

        def right_body_row(value: str) -> str:
            return prefix + "│" + self._fit(value, inner_w_right) + "│"

        lines[chassis_top] = self._fit(
            prefix + "╭" + "─" * chassis_total_width + "╮",
            cols,
        )
        lines[chassis_top + 1] = self._fit(
            (prefix + "╓" + "─" * inner_w_left + "╖")
            + " "
            + seam_fill
            + " "
            + right_body_row(right_title),
            cols,
        )
        for index in range(inner_h):
            row = chassis_top + 2 + index
            if row >= chassis_bot:
                break
            left_content = left[index] if index < len(left) else ""
            right_content = right[index] if index < len(right) else ""
            lines[row] = self._fit(
                left_body_row(left_content) + " " + right_body_row(right_content),
                cols,
            )

        last_inner_row = chassis_top + inner_h - 1
        if last_inner_row > chassis_top + 1 and last_inner_row < chassis_bot:
            left_bottom = prefix + "╙" + "─" * inner_w_left + "╜"
            right_bottom = prefix + "│" + "─" * inner_w_right + "│"
            lines[last_inner_row] = self._fit(left_bottom + " " + right_bottom, cols)

        lines[chassis_bot] = self._fit(
            prefix + "╰" + "─" * chassis_total_width + "╯",
            cols,
        )
        controls_y = layout.controls.y
        compact = cols < 120
        lines[controls_y] = self._fit("─" * max(cols - 4, 1), cols)
        surface = "GLASS" if full_screen and display == "glass" else display.upper()
        oi_state = "ON" if oi_enabled else "OFF"
        model_state = self._fit(model_label, 18) if model_label else "unconfigured"
        lines[controls_y + 1] = self._fit(
            f"TASK {status}  SURFACE {surface}  OI {oi_state}  MODEL {model_state}",
            cols,
        )
        if not compact:
            running = status in {
                "EXECUTING",
                "THINKING",
                "RESPONDING",
                "SEARCHING",
                "READING",
                "INSPECTING",
                "TOOLS",
                "APPROVAL",
                "DELEGATED",
            }
            status_short = status[:6].ljust(6)
            identity = f"ATHENA  {status_short}  {'●' if running else '◌'}"
            view = "GLASS" if full_screen and display == "glass" else display.upper()
            lines[controls_y + 2] = self._fit(
                f"{identity}  BRIGHT —  FOCUS —  VIEW {view}",
                cols,
            )
            prompt_y = layout.prompt.y
            lines[prompt_y] = self._fit("─" * max(cols - 4, 1), cols)
            if prompt_y + 1 < rows:
                text = prompt_text or "type a request · /help for controls"
                lines[prompt_y + 1] = self._fit(
                    f"❯ {text}    Ctrl-C cancel · Ctrl-D exit",
                    cols,
                )
        else:
            prompt_y = layout.prompt.y
            if prompt_y < rows:
                lines[prompt_y] = self._fit("─" * max(cols - 4, 1), cols)
            if prompt_y + 1 < rows:
                text = prompt_text or "type a request · /help for controls"
                lines[prompt_y + 1] = self._fit(f"❯ {text}", cols)
        return lines


__all__ = ["DualPaneChassisComposer"]
