"""ANSI cell-grid renderer with diff repainting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TextIO

try:
    from wcwidth import wcswidth
except ImportError:  # pragma: no cover - the fallback keeps core CLI usable
    import unicodedata

    def wcswidth(value: str) -> int:  # type: ignore[misc]
        width = 0
        for char in value:
            if unicodedata.combining(char) or unicodedata.category(char) in {"Cc", "Cf"}:
                continue
            width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        return width


ESC = "\x1b["


def cell_width(value: str) -> int:
    return max(wcswidth(value), 0)


def fit_cells(value: str, width: int, *, ellipsis: str = "…") -> str:
    """Truncate by terminal cells, not Python code points."""
    if width <= 0:
        return ""
    if cell_width(value) <= width:
        return value + " " * (width - cell_width(value))
    budget = max(width - cell_width(ellipsis), 0)
    out: list[str] = []
    used = 0
    for char in value:
        char_width = max(cell_width(char), 0)
        if used + char_width > budget:
            break
        out.append(char)
        used += char_width
    result = "".join(out) + (ellipsis if budget < width else "")
    return result + " " * max(width - cell_width(result), 0)


@dataclass(frozen=True)
class ChangedSpan:
    row: int
    start: int
    text: str


class CellGridDiffRenderer:
    """Render only changed rows/spans after the initial frame."""

    def __init__(self, output: TextIO) -> None:
        self.output = output
        self._previous: tuple[str, ...] | None = None
        self.last_changed_spans: tuple[ChangedSpan, ...] = ()

    @staticmethod
    def _spans(previous: str, current: str, row: int) -> list[ChangedSpan]:
        width = max(len(previous), len(current))
        before = previous.ljust(width)
        after = current.ljust(width)
        # A row-local repaint is both safer for variable-width Unicode and
        # still avoids the expensive full-screen clear. The row is the
        # smallest stable cell span we can reason about without a terminal
        # cursor-width query.
        if before != after:
            return [ChangedSpan(row, 0, after)]
        spans: list[ChangedSpan] = []
        index = 0
        while index < width:
            if before[index] == after[index]:
                index += 1
                continue
            start = index
            while index < width and before[index] != after[index]:
                index += 1
            spans.append(ChangedSpan(row, start, after[start:index]))
        return spans

    def draw(self, lines: Iterable[str], *, columns: int | None = None) -> str:
        current = tuple(
            fit_cells(str(line).replace("\n", " "), columns)
            if columns is not None
            else str(line).replace("\n", " ")
            for line in lines
        )
        if self._previous is None:
            spans = [ChangedSpan(row, 0, line) for row, line in enumerate(current)]
            payload = "\x1b[2J\x1b[H" + "".join(
                f"{ESC}{span.row + 1};1H{span.text}" for span in spans
            )
        else:
            rows = max(len(self._previous), len(current))
            spans = []
            for row in range(rows):
                spans.extend(
                    self._spans(
                        self._previous[row] if row < len(self._previous) else "",
                        current[row] if row < len(current) else "",
                        row,
                    )
                )
            payload = "".join(f"{ESC}{span.row + 1};{span.start + 1}H{span.text}" for span in spans)
        self._previous = current
        self.last_changed_spans = tuple(spans)
        if payload:
            self.output.write(payload)
            self.output.flush()
        return payload

    def reset(self) -> None:
        self._previous = None
        self.last_changed_spans = ()


__all__ = ["CellGridDiffRenderer", "ChangedSpan", "cell_width", "fit_cells"]
