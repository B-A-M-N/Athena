"""Execution subsystem (BUILDSPEC 9).

The public execution API is intentionally lazy.  Blocking-call scheduling
lives in the neutral :mod:`athena.concurrency` bridge, and eager import of
every backend here used to pull durable state modules back through
this package during interpreter startup.  That made isolated worker
processes fail with a partially initialized ``Database`` module.  Lazy
exports preserve ``from athena.execution import ...`` compatibility without
making the package initializer an architectural dependency hub.
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "process_tree": ("athena.execution.process_tree", None),
    "BackendRegistry": ("athena.execution.backend", "BackendRegistry"),
    "ExecutionBackend": ("athena.execution.backend", "ExecutionBackend"),
    "get_backend": ("athena.execution.backend", "get_backend"),
    "register_backend": ("athena.execution.backend", "register_backend"),
    "ContainerBackend": ("athena.execution.container", "ContainerBackend"),
    "SSHBackend": ("athena.execution.ssh", "SSHBackend"),
    "SSHProfile": ("athena.execution.ssh", "SSHProfile"),
    "LocalBackend": ("athena.execution.local", "LocalBackend"),
    "LocalRuntimeSupervisor": ("athena.execution.runtime_host", "LocalRuntimeSupervisor"),
    "SupervisedLocalBackend": ("athena.execution.runtime_host", "SupervisedLocalBackend"),
    "ExecutionManager": ("athena.execution.manager", "ExecutionManager"),
    "Sink": ("athena.execution.manager", "Sink"),
    "Diagnostic": ("athena.execution.diagnostics", "Diagnostic"),
    "normalize_diagnostics": ("athena.execution.diagnostics", "normalize_diagnostics"),
    "normalize_diagnostics_payload": (
        "athena.execution.diagnostics",
        "normalize_diagnostics_payload",
    ),
    "ProjectEnvironmentFingerprint": (
        "athena.execution.environment",
        "ProjectEnvironmentFingerprint",
    ),
    "ToolchainBinding": ("athena.execution.verification_environment", "ToolchainBinding"),
    "VerificationEnvironment": (
        "athena.execution.verification_environment",
        "VerificationEnvironment",
    ),
    "ConformanceReceipt": ("athena.execution.conformance", "ConformanceReceipt"),
    "run_backend_conformance": ("athena.execution.conformance", "run_backend_conformance"),
    "MAX_FRAME_BYTES": ("athena.execution.ssh_protocol", "MAX_FRAME_BYTES"),
    "PROTOCOL_NAME": ("athena.execution.ssh_protocol", "PROTOCOL_NAME"),
    "PROTOCOL_VERSION": ("athena.execution.ssh_protocol", "PROTOCOL_VERSION"),
    "SSHProtocolError": ("athena.execution.ssh_protocol", "SSHProtocolError"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(module_name)
    value = module if attribute_name is None else getattr(module, attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "ExecutionManager",
    "Sink",
    "ExecutionBackend",
    "BackendRegistry",
    "register_backend",
    "get_backend",
    "LocalBackend",
    "LocalRuntimeSupervisor",
    "SupervisedLocalBackend",
    "ContainerBackend",
    "SSHBackend",
    "SSHProfile",
    "process_tree",
    "Diagnostic",
    "normalize_diagnostics",
    "normalize_diagnostics_payload",
    "ProjectEnvironmentFingerprint",
    "ToolchainBinding",
    "VerificationEnvironment",
    "ConformanceReceipt",
    "run_backend_conformance",
    "MAX_FRAME_BYTES",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "SSHProtocolError",
]
