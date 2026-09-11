"""Bounded MCP connection supervision.

The supervisor owns only transport reconnection. Tool/resource/prompt
publication remains the service's connection callback, so a dead server is
never represented by stale registered tools.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any


class MCPConnectionSupervisor:
    CONNECTED = "CONNECTED"
    BACKOFF = "BACKOFF"
    HALF_OPEN = "HALF_OPEN"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    STOPPED = "STOPPED"

    def __init__(
        self,
        names: Iterable[str],
        reconnect: Callable[[str], Awaitable[dict[str, Any]]],
        *,
        interval_seconds: float = 5.0,
        max_backoff_seconds: float = 300.0,
        jitter: float = 0.2,
        circuit_failures: int = 3,
        circuit_open_seconds: float = 300.0,
    ) -> None:
        self._names = tuple(str(name) for name in names)
        self._reconnect = reconnect
        self._base = max(0.05, float(interval_seconds))
        self._max = max(self._base, float(max_backoff_seconds))
        self._jitter = max(0.0, min(float(jitter), 1.0))
        self._circuit_failures = max(1, int(circuit_failures))
        self._circuit_open_seconds = max(self._base, float(circuit_open_seconds))
        self._failures = {name: 0 for name in self._names}
        self._opened_until = {name: 0.0 for name in self._names}
        self._states = {name: self.STOPPED for name in self._names}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def states(self) -> dict[str, str]:
        return dict(self._states)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="athena-mcp-supervisor")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for name in self._names:
            self._states[name] = self.STOPPED

    def notify(self, name: str) -> None:
        if name in self._states:
            self._wake.set()

    def mark_failed(self, name: str) -> None:
        """Record a live transport loss and wake reconnect immediately."""
        if name not in self._states:
            return
        self._failures[name] += 1
        self._mark_failure(name)
        self._wake.set()

    def mark_connected(self, name: str) -> None:
        """Publish an already-established connection without exposing state."""
        if name not in self._states:
            return
        self._failures[name] = 0
        self._opened_until[name] = 0.0
        self._states[name] = self.CONNECTED
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), self._delay())
            except TimeoutError:
                pass
            self._wake.clear()
            for name in self._names:
                if self._stop.is_set():
                    return
                if self._states.get(name) == self.CONNECTED:
                    continue
                if self._states.get(name) == self.CIRCUIT_OPEN:
                    if time.monotonic() < self._opened_until[name]:
                        continue
                    self._states[name] = self.HALF_OPEN
                self._states[name] = self.HALF_OPEN
                try:
                    result = await self._reconnect(name)
                    if str(result.get("state") or "") == "connected":
                        self._failures[name] = 0
                        self._opened_until[name] = 0.0
                        self._states[name] = self.CONNECTED
                    else:
                        self._failures[name] += 1
                        self._mark_failure(name)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._failures[name] += 1
                    self._mark_failure(name)

    def _mark_failure(self, name: str) -> None:
        if self._failures[name] >= self._circuit_failures:
            self._opened_until[name] = time.monotonic() + self._circuit_open_seconds
            self._states[name] = self.CIRCUIT_OPEN
        else:
            self._states[name] = self.BACKOFF

    def _delay(self) -> float:
        failures = max(self._failures.values(), default=0)
        delay = min(self._base * (2**failures), self._max)
        return delay * random.uniform(1.0 - self._jitter, 1.0 + self._jitter)


__all__ = ["MCPConnectionSupervisor"]
