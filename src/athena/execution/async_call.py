"""Small blocking-call bridge that does not depend on asyncio's default executor."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable


class _CallResult:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


async def run_blocking(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one blocking call on an owned daemon thread without blocking asyncio.

    The host's default ``ThreadPoolExecutor`` is not reliable in every
    embedded/runtime environment Athena supports. A short-lived owned thread
    plus an async-yielding completion check keeps the blocking operation away
    from the event-loop thread without inheriting that executor's lifecycle.
    Callers that need concurrency limits should guard this function with a
    semaphore.
    """
    result = _CallResult()

    def invoke() -> None:
        try:
            result.value = function(*args, **kwargs)
        except BaseException as exc:  # propagate the exact worker failure
            result.error = exc
        finally:
            result.ready.set()

    threading.Thread(target=invoke, name="athena-blocking-call", daemon=True).start()
    while not result.ready.is_set():
        await asyncio.sleep(0)
    if result.error is not None:
        raise result.error
    return result.value


__all__ = ["run_blocking"]
