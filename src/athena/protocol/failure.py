"""Neutral, typed failure facts shared by lifecycle and presentation seams."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from athena.protocol.errors import (
    AthenaError,
    CapabilityError,
    ExecutionError,
    ModelUnavailable,
    PolicyDenied,
    ProviderError,
    RequestCancelled,
    TaskError,
)


_KIND_BY_STAGE = {
    "model_routing": "model_routing",
    "model_provider": "model_provider",
    "capability": "capability",
    "execution": "execution",
    "verification": "verification",
    "recovery": "recovery",
    "policy": "policy",
    "task": "task",
    "diagnostics": "diagnostics",
}


@dataclass(frozen=True)
class FailureInfo:
    """A bounded failure envelope with no classification from message text."""

    message: str = ""
    code: str = ""
    stage: str = ""
    kind: str = ""
    fatal: bool = False

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | None = None,
        *,
        default_kind: str = "",
        default_stage: str = "",
        default_fatal: bool = True,
    ) -> "FailureInfo":
        """Read explicit fields from an event or nested ``failure`` object."""
        data = payload if isinstance(payload, Mapping) else {}
        nested = data.get("failure")
        failure = nested if isinstance(nested, Mapping) else data
        message = str(failure.get("message") or failure.get("reason") or failure.get("error") or "")
        code = str(failure.get("code") or failure.get("failure_code") or "")
        stage = str(failure.get("stage") or default_stage or "")
        kind = str(
            failure.get("kind")
            or failure.get("failure_kind")
            or _KIND_BY_STAGE.get(stage, "")
            or default_kind
            or ""
        )
        fatal_value = failure.get("fatal", default_fatal)
        fatal = fatal_value if isinstance(fatal_value, bool) else default_fatal
        return cls(
            message=message,
            code=code,
            stage=stage,
            kind=kind,
            fatal=fatal,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "reason": self.message,
            "stage": self.stage,
            "kind": self.kind,
            "code": self.code,
            "fatal": self.fatal,
        }


@dataclass(frozen=True)
class RecoveryDiagnostic:
    """Typed evidence envelope consumed by the kernel's recovery reasoning.

    This is descriptive data only. It names permitted recovery actions but
    does not select or execute one; the AgentKernel remains the sole decision
    authority.
    """

    operation: str = ""
    failure_class: str = ""
    verification: tuple[Mapping[str, Any], ...] = ()
    environment: Mapping[str, Any] = field(default_factory=dict)
    resource_constraints: Mapping[str, Any] = field(default_factory=dict)
    permitted_recovery: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "failure_class": self.failure_class,
            "verification": [dict(item) for item in self.verification],
            "environment": dict(self.environment or {}),
            "resource_constraints": dict(self.resource_constraints or {}),
            "permitted_recovery": list(self.permitted_recovery),
            "evidence": dict(self.evidence or {}),
        }


def failure_from_exception(exc: BaseException) -> FailureInfo:
    """Translate a typed exception into a presentation-safe failure fact.

    This is deliberately type-based.  The worker is the boundary where an
    exception becomes a durable task outcome, so error strings must never be
    used to infer the stage or severity of that outcome.
    """
    message = str(exc) or type(exc).__name__
    code = str(getattr(exc, "code", "") or "worker_failure")
    if isinstance(exc, ModelUnavailable):
        return FailureInfo(
            message=message,
            code=code,
            stage="model_admission",
            kind="model_routing",
            fatal=True,
        )
    if isinstance(exc, ProviderError):
        return FailureInfo(
            message=message,
            code=code,
            stage="model_provider",
            kind="model_provider",
            fatal=True,
        )
    if isinstance(exc, CapabilityError):
        return FailureInfo(
            message=message,
            code=code,
            stage="capability",
            kind="capability",
            fatal=True,
        )
    if isinstance(exc, ExecutionError):
        return FailureInfo(
            message=message,
            code=code,
            stage="execution",
            kind="execution",
            fatal=True,
        )
    if isinstance(exc, PolicyDenied):
        return FailureInfo(
            message=message,
            code=code,
            stage="policy",
            kind="policy",
            fatal=True,
        )
    if isinstance(exc, RequestCancelled):
        return FailureInfo(
            message=message,
            code=code,
            stage="task",
            kind="task",
            fatal=False,
        )
    if isinstance(exc, TaskError):
        return FailureInfo(
            message=message,
            code=code,
            stage="task",
            kind="task",
            fatal=True,
        )
    if isinstance(exc, AthenaError):
        return FailureInfo(
            message=message,
            code=code,
            stage="task",
            kind="task",
            fatal=True,
        )
    return FailureInfo(
        message=message,
        code=code,
        stage="task",
        kind="task",
        fatal=True,
    )


__all__ = ["FailureInfo", "RecoveryDiagnostic", "failure_from_exception"]
