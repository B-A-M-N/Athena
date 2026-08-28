"""Integrated prompt/approval input boundary for the terminal surface."""

from __future__ import annotations

import sys
from typing import Callable, TextIO


class PromptController:
    """Keep prompt ownership with the surface, while remaining test-injectable."""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] | None = None,
        stdin=None,
        output: TextIO | None = None,
    ) -> None:
        self.input_fn = input_fn
        self.stdin = stdin or sys.stdin
        self.output = output or sys.stdout
        self.history: list[str] = []

    def read(self, prompt: str = "athena> ") -> str:
        if self.input_fn is not None:
            value = self.input_fn(prompt)
        else:
            self.output.write(prompt)
            self.output.flush()
            value = self.stdin.readline()
            if value == "":
                raise EOFError
            value = value.rstrip("\n")
        if value.strip():
            self.history.append(value)
        return value


__all__ = ["PromptController"]
