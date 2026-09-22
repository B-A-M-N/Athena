"""Bounded streamed-output collection beneath ``ExecutionManager``."""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

from athena.protocol.execution import ExecutionEventType, ExecutionExitStatus, ExecutionResult

_DEFAULT_MAX_BYTES = 8 * 1024 * 1024


class ExecutionResultCollector:
    """Buffer one canonical execution stream into a bounded result."""

    def __init__(self, *, max_results_bytes: int | None = _DEFAULT_MAX_BYTES) -> None:
        self._max_results_bytes = max_results_bytes

    async def collect(
        self,
        events: AsyncIterator[Any],
        *,
        execution_id: str,
        sink: Any | None = None,
    ) -> ExecutionResult:
        started = time.monotonic()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        stdout_bytes = 0
        stderr_bytes = 0
        exit_status: ExecutionExitStatus = ExecutionExitStatus.FAILED
        exit_code: int | None = None
        cap = self._max_results_bytes or float("inf")

        if sink:
            await sink.chunk("", stream="start")

        async for event in events:
            if event.type == ExecutionEventType.STDOUT:
                data = event.data or ""
                if stdout_bytes < cap:
                    take = (
                        data if stdout_bytes + len(data) <= cap else data[: int(cap - stdout_bytes)]
                    )
                    stdout_parts.append(take)
                    stdout_bytes += len(take)
                    if stdout_bytes >= cap:
                        stdout_parts.append("\n[output truncated]")
                if sink:
                    await sink.chunk(data, stream="stdout")
            elif event.type == ExecutionEventType.STDERR:
                data = event.data or ""
                if stderr_bytes < cap:
                    take = (
                        data if stderr_bytes + len(data) <= cap else data[: int(cap - stderr_bytes)]
                    )
                    stderr_parts.append(take)
                    stderr_bytes += len(take)
                if sink:
                    await sink.chunk(data, stream="stderr")
            elif event.type == ExecutionEventType.EXITED:
                exit_status = event.exit_status or ExecutionExitStatus.EXITED
                exit_code = event.exit_code

        if sink:
            await sink.chunk("", stream="exit")

        return ExecutionResult(
            execution_id=execution_id,
            exit_code=exit_code,
            status=exit_status,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            duration_ms=int((time.monotonic() - started) * 1000),
        )


__all__ = ["ExecutionResultCollector"]
