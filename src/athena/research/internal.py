"""Private internal request/context records for research operations.

These are not capability contracts; they keep request-shaped operation
mechanics decoupled from the public ``CapabilityRequest`` boundary.
"""

from __future__ import annotations

__all__ = ["InternalContext", "InternalRequest"]


class InternalRequest:
    """Private domain request view consumed by internal operation mechanics."""

    def __init__(self, *, arguments, task_id, session_id, call_id):
        self.arguments = arguments
        self.task_id = task_id
        self.session_id = session_id
        self.call_id = call_id
        self.capability_id = "research"


class InternalContext:
    """Private domain context view consumed by internal operation mechanics."""

    def __init__(self, *, project_id):
        self.project_id = project_id

    @property
    def workspace(self):
        from types import SimpleNamespace

        return SimpleNamespace(id=self.project_id) if self.project_id else None
