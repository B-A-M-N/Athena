"""Execution route and translation mechanics for :class:`RealityGate`.

The gate owns routing decisions. This module owns the immutable route record
and deterministic workspace-argument translation used by every disposition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.reality import ExecutionDisposition
from athena.protocol.tasks import WorkspaceSpec
from athena.reality.classification import RealityClassification

__all__ = ["RealityRoute"]


@dataclass(frozen=True)
class RealityRoute:
    """Resolved workspace and audit metadata for one invocation."""

    workspace: WorkspaceSpec
    disposition: ExecutionDisposition
    transaction_id: str | None = None
    checkpoint_id: str | None = None
    classification: RealityClassification | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "transaction_id": self.transaction_id,
            "checkpoint_id": self.checkpoint_id,
            "classification": (
                self.classification.to_record() if self.classification is not None else None
            ),
        }


def translate_workspace_arguments(
    request: CapabilityRequest,
    base_root: str,
    target_root: str,
) -> None:
    """Translate absolute task-root arguments into the shadow root.

    Relative paths remain relative to the routed workspace.  Shell source is
    intentionally not rewritten: absolute host paths are not made writable by
    the sandbox, which is safer than trying to parse arbitrary code.
    """
    base = os.path.realpath(os.path.abspath(base_root))
    target = os.path.realpath(os.path.abspath(target_root))
    args = dict(request.arguments or {})
    for key in ("path", "destination", "cwd", "workdir"):
        value = args.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            continue
        candidate = os.path.realpath(os.path.abspath(value))
        if candidate == base or candidate.startswith(base + os.sep):
            args[key] = target + candidate[len(base) :]
    object.__setattr__(request, "arguments", args)
