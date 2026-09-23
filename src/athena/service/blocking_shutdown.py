"""Lifecycle-safe shutdown for the shared blocking worker pools."""

from __future__ import annotations

import logging

from athena.concurrency import shutdown_blocking

_logger = logging.getLogger("athena.service")


async def shutdown_blocking_workers(*, timeout: float = 10.0) -> None:
    """Drain blocking adapters without making stop failure opaque."""
    try:
        await shutdown_blocking(timeout=timeout)
    except Exception as exc:
        _logger.warning("blocking worker shutdown incomplete: %s", exc)


__all__ = ["shutdown_blocking_workers"]
