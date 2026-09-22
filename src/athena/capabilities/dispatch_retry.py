"""Bounded retry mechanics for :class:`CapabilityDispatcher`."""

from __future__ import annotations

import asyncio
from typing import Any

from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    RetryPolicy,
    classify_exception_failure,
)
from athena.protocol.events import EV

__all__ = ["invoke_with_retry"]


async def invoke_with_retry(
    dispatcher: Any, executor: Any, request: CapabilityRequest, **kwargs: Any
):
    """Retry only classified, known-outcome read-only failures."""
    descriptor = getattr(executor, "descriptor", None)
    policy = descriptor.resolve_retry_policy() if descriptor is not None else RetryPolicy.NEVER
    max_attempts = 2 if policy is RetryPolicy.READ_ONLY else 1
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        try:
            result = await executor.invoke(request, **kwargs)
            if attempts:
                if (
                    isinstance(result, CapabilityResult)
                    and result.status is CapabilityResultStatus.FAILED
                ):
                    metadata = dict(result.metadata or {})
                    diagnostics = list(metadata.get("diagnostics") or [])
                    diagnostics.extend(attempts)
                    object.__setattr__(
                        result,
                        "metadata",
                        {
                            **metadata,
                            "retry_attempts": len(attempts) + 1,
                            "diagnostics": diagnostics,
                        },
                    )
            return result
        except (KeyboardInterrupt, SystemExit):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = classify_exception_failure(
                exc,
                descriptor=descriptor,
                operation=str((request.arguments or {}).get("operation") or ""),
                resource=str((request.arguments or {}).get("path") or ""),
            )
            attempts.append(failure.to_metadata())
            await dispatcher._emit(
                EV["CAPABILITY_PROGRESS"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "message": f"retry attempt {attempt} classified {failure.code.value}",
                    "attempt": attempt,
                    "failure": failure.to_metadata(),
                    "determinate": False,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            if attempt >= max_attempts or not (
                failure.retryable
                and failure.outcome_known
                and dispatcher._retry_budget_allows(request)
            ):
                raise
    raise AssertionError("unreachable")  # pragma: no cover
