"""Scheduled durable activation and persistence bookkeeping for generated caps.

Subordinate to :class:`athena.affordances.fabric.CapabilityFabric`. This module
owns asynchronous persist-then-activate scheduling and shutdown flushing. It
does not decide overlay admission or lifecycle transitions.
"""

from __future__ import annotations

import asyncio
import logging

_logger = logging.getLogger("athena.affordances")

__all__ = ["DurableActivationScheduler"]


class DurableActivationScheduler:
    """Track scheduled persist-then-activate work and surface its failures."""

    def __init__(self, fabric) -> None:
        self._fabric = fabric
        self._tasks: set[asyncio.Task] = set()
        self._errors: dict[asyncio.Task, BaseException] = {}

    def schedule(self, generated, executor, *, owner: str) -> None:
        """Schedule durable activation on the running loop."""
        try:
            task = asyncio.create_task(
                self._fabric.persist_and_activate(generated, executor, owner=owner)
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"cannot activate durable capability {generated.id} without a running loop"
            ) from exc
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            self._errors[task] = RuntimeError("generated capability persistence task cancelled")
            return
        error = task.exception()
        if error is not None:
            self._errors[task] = error
            _logger.error(
                "generated capability persistence failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def flush(self) -> None:
        """Wait for scheduled overlay persistence before shutdown."""
        if self._tasks:
            tasks = tuple(self._tasks)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.difference_update(tasks)
            for task, result in zip(tasks, results):
                if isinstance(result, BaseException):
                    self._errors.setdefault(task, result)
        if self._errors:
            errors = tuple(self._errors.values())
            self._errors.clear()
            raise RuntimeError(
                "generated capability persistence failed: "
                + "; ".join(str(error) for error in errors)
            )
