"""Typed error taxonomy for Athena.

Core structured errors defined in IMPLEMENTATIONSPEC section 111. Retries are
owned by the layer that understands the failure; these types classify failures
so callers can decide whether a retry is appropriate.
"""

from __future__ import annotations

from typing import Any


class AthenaError(Exception):
    """Base class for all Athena structured errors."""

    code = "athena_error"
    retryable = False

    def __init__(self, message: str, *, cause: BaseException | None = None, **data: Any) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            **self.data,
        }


class ConfigurationError(AthenaError):
    code = "configuration_error"


class ServiceNotReady(ConfigurationError):
    """The service is alive but cannot admit agent work yet."""

    code = "service_not_ready"
    http_status = 503


class ModelProviderUnconfigured(ServiceNotReady):
    """No model provider is configured for model-backed agent work."""

    code = "model_provider_unconfigured"


class TaskError(AthenaError):
    code = "task_error"


class TaskBudgetExceeded(TaskError):
    code = "task_budget_exceeded"
    retryable = False


class TaskDeadlineExceeded(TaskError):
    code = "task_deadline_exceeded"
    retryable = False


class ProviderOutcomeUnknown(TaskError):
    """A provider may have completed while the local receipt was unavailable."""

    code = "provider_outcome_unknown"
    retryable = False


class IllegalStateTransition(TaskError):
    code = "illegal_state_transition"


class TaskOwnershipLost(TaskError):
    """A worker's task lease was lost (expired, reclaimed, or cleared).

    Raised by the storage layer when a lease-renewal CAS fails: the worker no
    longer owns the task and MUST stop driving it immediately instead of
    risking duplicate execution alongside the new owner.
    """

    code = "task_ownership_lost"


class ProviderError(AthenaError):
    code = "provider_error"


class VoiceError(AthenaError):
    """Base error for the governed voice transport."""

    code = "voice_error"


class VoiceUnavailable(VoiceError):
    """Voice is disabled or has no configured capable provider."""

    code = "voice_unavailable"
    http_status = 503


class VoiceInputError(VoiceError):
    """The supplied audio or voice request is invalid."""

    code = "voice_input_invalid"
    http_status = 400


class VoiceResultNotReady(VoiceError):
    """A requested spoken task result does not exist yet."""

    code = "voice_result_not_ready"
    http_status = 409
    retryable = False


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication_error"


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limit"
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **data: Any) -> None:
        self.retry_after = max(0.0, float(retry_after)) if retry_after is not None else None
        if self.retry_after is not None:
            data.setdefault("retry_after_seconds", self.retry_after)
        super().__init__(message, **data)


class ProviderTimeout(ProviderError):
    code = "provider_timeout"
    retryable = True


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"
    retryable = True


class ProviderProtocolError(ProviderError):
    code = "provider_protocol_error"


class ProviderMalformedResponse(ProviderError):
    code = "provider_malformed_response"


class ModelUnavailable(ProviderError):
    code = "model_unavailable"


class ContextOverflow(ProviderError):
    code = "context_overflow"


class ContextIntegrityError(TaskError):
    """Canonical task context could not be read or verified.

    Transcript and authority-bearing context are not optional enrichments. A
    caller must recover or explicitly repair the durable state before another
    model turn can be admitted.
    """

    code = "context_integrity_error"
    retryable = True


class RequestCancelled(AthenaError):
    code = "request_cancelled"


class CancellationUncertain(TaskError):
    """Cancellation was requested but terminal cancellation is unproven."""

    code = "cancellation_uncertain"
    retryable = True


class Cancelled(RequestCancelled):
    code = "cancelled"


class CapabilityError(AthenaError):
    code = "capability_error"


class CapabilityUnavailable(CapabilityError):
    code = "capability_unavailable"


class CapabilityReadinessError(CapabilityError):
    """The requested task has no policy-permitted ready capability surface."""

    code = "capability_readiness_error"


class CapabilityValidationError(CapabilityError):
    code = "capability_validation_error"


class PolicyDenied(AthenaError):
    code = "policy_denied"
    retryable = False


class ApprovalExpired(AthenaError):
    code = "approval_expired"


class ExecutionError(AthenaError):
    code = "execution_error"


class ExecutionTimeout(ExecutionError):
    code = "execution_timeout"


class ExecutionInterrupted(ExecutionError):
    code = "execution_interrupted"


class RuntimeUnavailable(ExecutionError):
    code = "runtime_unavailable"


class FilesystemConflict(CapabilityError):
    code = "filesystem_conflict"


class MCPError(AthenaError):
    code = "mcp_error"


class PersistenceError(AthenaError):
    code = "persistence_error"


class RecoveryError(AthenaError):
    code = "recovery_error"


__all__ = [
    "AthenaError",
    "ConfigurationError",
    "ServiceNotReady",
    "ModelProviderUnconfigured",
    "TaskError",
    "TaskBudgetExceeded",
    "TaskDeadlineExceeded",
    "ProviderOutcomeUnknown",
    "IllegalStateTransition",
    "CancellationUncertain",
    "ProviderError",
    "ContextIntegrityError",
    "ProviderAuthenticationError",
    "ProviderRateLimitError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "ProviderProtocolError",
    "ProviderMalformedResponse",
    "ModelUnavailable",
    "ContextOverflow",
    "RequestCancelled",
    "Cancelled",
    "CapabilityError",
    "CapabilityUnavailable",
    "CapabilityReadinessError",
    "CapabilityValidationError",
    "PolicyDenied",
    "ApprovalExpired",
    "ExecutionError",
    "ExecutionTimeout",
    "ExecutionInterrupted",
    "RuntimeUnavailable",
    "FilesystemConflict",
    "MCPError",
    "PersistenceError",
    "RecoveryError",
]
