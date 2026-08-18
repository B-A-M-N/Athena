"""Policy domain model.

Policy evaluation (BUILDSPEC 33-36) decides allow / ask / deny for capability
calls and records approval grants. All policy request/decision/approval types
live here so engine implementations import from this single source of truth.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.capabilities import EffectClass
from athena.protocol.tasks import WorkspaceSpec


class PolicyVerdict(str, enum.Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalScope(str, enum.Enum):
    CALL = "call"
    TASK = "task"
    SESSION = "session"
    PROJECT = "project"
    PROFILE = "profile"


class ApprovalState(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Principal:
    kind: str
    id: str


@dataclass(frozen=True)
class PolicyRequest:
    principal: Principal
    task_id: str
    capability_id: str
    arguments: Mapping[str, Any]
    workspace: WorkspaceSpec
    execution_backend: str | None = None
    effects: frozenset[EffectClass] = frozenset()
    session_id: str | None = None
    call_id: str | None = None


@dataclass(frozen=True)
class PolicyDecision:
    decision: PolicyVerdict
    reason: str
    matched_rule: str | None = None
    approval_scope_options: tuple[ApprovalScope, ...] = ()


@dataclass(frozen=True)
class ApprovalGrant:
    id: str
    principal: Principal
    scope: ApprovalScope
    capability: str | None = None
    resource_pattern: str | None = None
    effect: EffectClass | None = None
    task_id: str | None = None
    session_id: str | None = None
    expires_at: datetime | None = None


__all__ = [
    "PolicyVerdict",
    "ApprovalScope",
    "ApprovalState",
    "Principal",
    "PolicyRequest",
    "PolicyDecision",
    "ApprovalGrant",
]