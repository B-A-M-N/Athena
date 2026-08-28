"""Lifecycle records for cheap task-local computation."""

from __future__ import annotations

import hashlib
import json
import textwrap
from dataclasses import replace
from typing import Any

from athena.affordances.models import ScratchComputationRecord, ScratchProgram
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

    def create(
        self, *, code: str, task_id: str | None, runtime: str = "python", purpose: str = ""
    ) -> ScratchProgram:
        program = ScratchProgram(
            id=new_id("scratch"),
            code=code,
            runtime=runtime,
            task_id=task_id,
            provenance={"purpose": purpose},
        )
        self._programs[program.id] = program
        return program

    def get(self, program_id: str, *, task_id: str | None = None) -> ScratchProgram:
        """Return one scratch program only when the caller owns its task."""
        program = self._programs.get(program_id)
        if program is None or (task_id is not None and program.task_id != task_id):
            raise KeyError(program_id)
        return program

    def set_contract(
        self,
        program_id: str,
        *,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any] | None = None,
    ) -> ScratchProgram:
        program = self.get(program_id)
        updated = replace(
            program,
            input_schema=dict(input_schema),
            output_schema=dict(output_schema or {}) or None,
        )
        self._programs[program_id] = updated
        return updated

    def record_result(
        self,
        program_id: str,
        *,
        ok: bool,
        output: str = "",
        error: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        if program_id not in self._programs:
            raise KeyError(program_id)
        self._results.setdefault(program_id, []).append(
            {
                "ok": ok,
                "output": output,
                "error": error,
                "arguments": dict(arguments or {}),
            }
        )

    def for_task(self, task_id: str | None) -> list[ScratchProgram]:
        return [p for p in self._programs.values() if p.task_id == task_id]

    def results(self, program_id: str) -> list[dict[str, Any]]:
        return list(self._results.get(program_id, ()))

    def computation_record(self, program_id: str) -> ScratchComputationRecord:
        program = self.get(program_id)
        results = self._results.get(program_id, [])
        outputs_by_input: dict[str, str] = {}
        successful = 0
        for result in results:
            if not result.get("ok"):
                continue
            successful += 1
            arguments = dict(result.get("arguments") or {})
            key = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
            output = str(result.get("output") or "")
            previous = outputs_by_input.setdefault(key, output)
            if previous != output:
                outputs_by_input[key] = "__NONDETERMINISTIC__"
        output_values: list[Any] = []
        for output in outputs_by_input.values():
            if output == "__NONDETERMINISTIC__":
                continue
            try:
                output_values.append(json.loads(output))
            except (TypeError, ValueError):
                pass
        normalized = textwrap.dedent(program.code).strip()
        return ScratchComputationRecord(
            scratch_id=program.id,
            normalized_code=normalized,
            input_schema=program.input_schema,
            output_schema=(
                _output_schema(output_values) if output_values else program.output_schema
            ),
            code_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            uses=len(results),
            successful_uses=successful,
            deterministic=("__NONDETERMINISTIC__" not in outputs_by_input),
        )

    def validation_cases(self, program_id: str) -> list[dict[str, Any]]:
        """Build positive behavioral fixtures from successful observations."""
        cases: list[dict[str, Any]] = []
        seen: set[str] = set()
        import json

        for result in self._results.get(program_id, ()):
            if not result.get("ok"):
                continue
            arguments = dict(result.get("arguments") or {})
            signature = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
            if signature in seen:
                continue
            seen.add(signature)
            case: dict[str, Any] = {"args": arguments}
            try:
                case["expect_output"] = json.loads(result.get("output") or "null")
            except (TypeError, ValueError):
                pass
            cases.append(case)
        return cases

    def promotion_ready(self, program_id: str) -> bool:
        """Require distinct successful inputs before automatic elevation."""
        results = self._results.get(program_id, ())
        return (
            sum(bool(result.get("ok")) for result in results) >= 2
            and len(self.validation_cases(program_id)) >= 2
        )

    def discard_task(self, task_id: str | None) -> None:
        ids = [p.id for p in self.for_task(task_id)]
        for program_id in ids:
            self._programs.pop(program_id, None)
            self._results.pop(program_id, None)


def _output_schema(values: list[Any]) -> dict[str, Any] | None:
    if not values:
        return None
    kinds = {_json_type(value) for value in values}
    if len(kinds) > 1:
        return {"type": "array", "items": {"type": "object"}}
    kind = next(iter(kinds))
    if kind == "object":
        keys = sorted({str(key) for value in values if isinstance(value, dict) for key in value})
        return {
            "type": "object",
            "properties": {
                key: {
                    "type": _json_type(
                        next(
                            (
                                value[key]
                                for value in values
                                if isinstance(value, dict) and key in value
                            ),
                            None,
                        )
                    )
                }
                for key in keys
            },
            "additionalProperties": True,
        }
    return {"type": kind}


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


__all__ = ["ScratchManager"]
