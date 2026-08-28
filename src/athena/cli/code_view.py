"""Bounded code/diff material for action-specific OI scenes."""

from __future__ import annotations

from dataclasses import dataclass

from athena.cli.activity import language_for_path
from athena.cli.terminal import sanitize_terminal_text

MAX_CODE_PREVIEW = 128 * 1024


@dataclass(frozen=True)
class CodeViewport:
    path: str
    language: str
    text: str
    visible_start: int = 0
    visible_end: int = 0
    reveal_offset: int = 0
    mutation_state: str = ""
    diff_hunks: tuple[str, ...] = ()
    preview_truncated: bool = False

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self.text.splitlines())


def bounded_preview(value: object, *, limit: int = MAX_CODE_PREVIEW) -> tuple[str, bool]:
    """Sanitize and bound presentation text without changing event truth."""
    text = sanitize_terminal_text(value)
    if len(text) <= limit:
        return text, False
    marker = "\n… [preview truncated]"
    return text[: max(limit - len(marker), 0)] + marker, True


def make_code_view(
    *,
    path: object,
    text: object,
    mutation_state: str = "",
    diff_hunks: tuple[str, ...] = (),
    preview_truncated: bool = False,
) -> CodeViewport | None:
    clean_path = sanitize_terminal_text(path).strip()
    clean_text, truncated = bounded_preview(text)
    if not clean_path or not clean_text:
        return None
    total = len(clean_text.splitlines())
    return CodeViewport(
        path=clean_path,
        language=language_for_path(clean_path),
        text=clean_text,
        visible_end=total,
        mutation_state=mutation_state,
        diff_hunks=diff_hunks,
        preview_truncated=preview_truncated or truncated,
    )


__all__ = ["CodeViewport", "MAX_CODE_PREVIEW", "bounded_preview", "make_code_view"]
