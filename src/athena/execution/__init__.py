"""Execution subsystem (BUILDSPEC 9).

Central authority for process execution (INV-005). Streams execution events
from runtimes (persistent Python/shell subprocesses), enforces process
ownership per task, and owns backends (local / container).
"""

from athena.execution import process_tree
from athena.execution.backend import (
    BackendRegistry,
    ExecutionBackend,
    get_backend,
    register_backend,
)
from athena.execution.container import ContainerBackend
from athena.execution.ssh import SSHBackend, SSHProfile
from athena.execution.local import LocalBackend
from athena.execution.runtime_host import LocalRuntimeSupervisor, SupervisedLocalBackend
from athena.execution.manager import ExecutionManager, Sink
from athena.execution.diagnostics import (
    Diagnostic,
    normalize_diagnostics,
    normalize_diagnostics_payload,
)
from athena.execution.environment import (
    ProjectEnvironmentFingerprint,
    ToolchainBinding,
    VerificationEnvironment,
)
from athena.execution.conformance import ConformanceReceipt, run_backend_conformance

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
]
