"""Neutral continuation DTOs for approval/input-paused capability calls."""

from __future__ import annotations

from athena.protocol.capabilities import CapabilityRequest, DispatchDirectives
from athena.protocol.policy import PolicyDecision


class SuspendedCall:
    """A capability call parked on an ``ask`` decision, awaiting approval."""

    def __init__(
        self,
        call_id: str,
        request: CapabilityRequest,
        decision: PolicyDecision,
        approval_id: str | None = None,
        directives: DispatchDirectives | None = None,
    ) -> None:
        self.call_id = call_id
        self.request = request
        self.decision = decision
        self.approval_id = approval_id
        self.directives = directives
        self.workflow_run_id: str | None = None
        self.workflow_id: str | None = None
        self.workflow_parent_request: CapabilityRequest | None = None


__all__ = ["SuspendedCall"]
