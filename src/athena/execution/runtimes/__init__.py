"""Execution runtimes.

Borrowed and adapted from Classic Open Interpreter's execution engine:
persistent shell and persistent Python subprocesses sharing state across
executions within a runtime session (Scenario B), with process-tree ownership
for cancellation (BUILDSPEC 47-50).
"""

from athena.execution.runtimes.base import BaseRuntime
from athena.execution.runtimes.python import PythonRuntime
from athena.execution.runtimes.shell import ShellRuntime

try:
    from athena.execution.runtimes.node import NodeRuntime
except Exception:  # pragma: no cover - optional runtime
    NodeRuntime = None  # type: ignore[assignment,misc]

try:
    from athena.execution.runtimes.powershell import PowerShellRuntime
except Exception:  # pragma: no cover - optional runtime
    PowerShellRuntime = None  # type: ignore[assignment,misc]

__all__ = [
    "BaseRuntime",
    "PythonRuntime",
    "ShellRuntime",
    "NodeRuntime",
    "PowerShellRuntime",
]