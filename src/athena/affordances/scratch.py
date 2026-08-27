"""Lifecycle records for cheap task-local computation."""

from __future__ import annotations

from typing import Any

from athena.affordances.models import ScratchProgram
from athena.protocol.ids import new_id


class ScratchManager:
    """Track scratch programs without promoting them into global tools.

    Execution remains the normal ``execute`` capability.  This manager only
    gives the kernel/fabric a durable-neutral identity and lifecycle for a
    helper that is useful during one task.
    """

    def __init__(self) -> None:
        self._programs: dict[str, ScratchProgram] = {}
        self._results: dict[str, list[dict[str, Any]]] = {}

    def create(self, *, code: str, task_id: str | None,
               runtime: str = "python", purpose: str = "") -> ScratchProgram:
        program = ScratchProgram(
            id=new_id("scratch"), code=code, runtime=runtime, task_id=task_id,
            provenance={"purpose": purpose},
        )
        self._programs[program.id] = program
        return program

    def record_result(self, program_id: str, *, ok: bool, output: str = "",
                      error: str | None = None) -> None:
        if program_id not in self._programs:
            raise KeyError(program_id)
        self._results.setdefault(program_id, []).append({
            "ok": ok, "output": output, "error": error,
        })

    def for_task(self, task_id: str | None) -> list[ScratchProgram]:
        return [p for p in self._programs.values() if p.task_id == task_id]

    def results(self, program_id: str) -> list[dict[str, Any]]:
        return list(self._results.get(program_id, ()))

    def discard_task(self, task_id: str | None) -> None:
        ids = [p.id for p in self.for_task(task_id)]
        for program_id in ids:
            self._programs.pop(program_id, None)
            self._results.pop(program_id, None)


__all__ = ["ScratchManager"]
