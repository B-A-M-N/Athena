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
from athena.execution.local import LocalBackend
from athena.execution.manager import ExecutionManager, Sink
from athena.execution.diagnostics import Diagnostic, normalize_diagnostics

__all__ = [
    "ExecutionManager",
    "Sink",
    "ExecutionBackend",
    "BackendRegistry",
    "register_backend",
    "get_backend",
    "LocalBackend",
    "ContainerBackend",
    "process_tree",
    "Diagnostic",
    "normalize_diagnostics",
]
