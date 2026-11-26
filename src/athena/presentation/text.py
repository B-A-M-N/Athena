"""Bounded, terminal-safe text utilities for presentation consumers."""

from __future__ import annotations

import re


# CSI, OSC, DCS, SOS, PM, APC and Kitty graphics payloads are all terminal
# protocols, never display content. The terminators cover both 7-bit ST and
# their 8-bit C1 forms.
_PROTOCOL = re.compile(
    r"(?:"
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x9d[^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b(?:P|X|\^|_)[^\x1b]*(?:\x1b\\)"
    r"|\x90[^\x1b]*(?:\x1b\\)"
    r"|\x98[^\x1b]*(?:\x1b\\)"
    r"|\x9e[^\x1b]*(?:\x1b\\)"
    r"|\x9f[^\x1b]*(?:\x1b\\)"
    r")",
    re.DOTALL,
)


def sanitize_terminal_text(value: object) -> str:
    """Return printable projection text while retaining canonical input raw."""
    text = _PROTOCOL.sub("", str(value or "")).replace("\r", "\n")
    return "".join(
        char
        for char in text
        if char in {"\n", "\t"} or (ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F)
    )


__all__ = ["sanitize_terminal_text"]
