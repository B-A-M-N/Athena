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


class TaskError(AthenaError):
    code = "task_error"


class TaskBudgetExceeded(TaskError):
    code = "task_budget_exceeded"
    retryable = False


class TaskDeadlineExceeded(TaskError):
    code = "task_deadline_exceeded"
    retryable = False


class IllegalStateTransition(TaskError):
    code = "illegal_state_transition"


class ProviderError(AthenaError):
    code = "provider_error"
    retryable = False


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication_error"


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limit"
    retryable = True


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


class RequestCancelled(AthenaError):
    code = "request_cancelled"


class Cancelled(RequestCancelled):
    code = "cancelled"


class CapabilityError(AthenaError):
    code = "capability_error"


class CapabilityUnavailable(CapabilityError):
    code = "capability_unavailable"


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
    "TaskError",
    "TaskBudgetExceeded",
    "TaskDeadlineExceeded",
    "IllegalStateTransition",
    "ProviderError",
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