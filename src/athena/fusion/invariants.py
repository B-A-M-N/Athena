"""Governed invariant-probe construction for Fusion experiments.

Invariant probes are declarative command specs executed through the canonical
capability dispatcher. This module owns translation and shadow-path rewriting;
the orchestrator owns when an experiment checks them and the Reality boundary
owns promotion.
"""

from __future__ import annotations

import os
from typing import Any

from athena.protocol.capabilities import CapabilityRequest, CapabilityRequestOrigin
from athena.protocol.continuations import SuspendedCall
from athena.protocol.ids import new_id

__all__ = ["InvariantProbeMechanism"]


class InvariantProbeMechanism:
    """Build and run declarative invariant probes under canonical dispatch."""

    def __init__(self, *, dispatcher: Any, world_state_store: Any) -> None:
        self._dispatcher = dispatcher
        self._world_state_store = world_state_store

    async def dispatch_probe(
        self,
        code: str,
        workspace: Any,
        profile: str | None,
        task_id: str | None = None,
    ) -> tuple[bool, str]:
        if self._dispatcher is None:
            raise RuntimeError("capability dispatcher is not bound to Fusion")
        request = CapabilityRequest(
            capability_id="execute",
            arguments={"language": "shell", "code": code},
            task_id=task_id,
            call_id=new_id("call"),
            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
        )
        result = await self._dispatcher.dispatch(request, workspace=workspace, profile=profile)
        if isinstance(result, SuspendedCall):
            return False, "probe requires approval; suspended"
        if isinstance(result, Exception):
            return False, str(result)
        ok = getattr(result.status, "value", str(result.status)) == "ok"
        detail = (getattr(result, "output", None) or "") + (getattr(result, "error", None) or "")
        return ok, detail

    def build(
        self,
        specs: list[dict] | None,
        *,
        branch: Any = None,
        profile: str | None = None,
        task_id: str | None = None,
    ):
        """Build an InvariantSet from declarative command specifications."""
        from athena.worldstate import InvariantSet

        invariant_set = InvariantSet(task_id=task_id, store=self._world_state_store)
        for spec in specs or []:
            probe = spec.get("probe")
            if probe is not None:
                raise ValueError(
                    "fusion invariants must use declarative command specs; "
                    "arbitrary Python probes are not durable"
                )
            if probe is None and spec.get("command") and branch is not None:
                code = self.rewrite_to_shadow(spec["command"], branch)

                async def command_probe(command=code):
                    ok, _ = await self.dispatch_probe(
                        command, branch.shadow_workspace, profile, task_id=task_id
                    )
                    return ok

                probe = command_probe
            if probe is None:
                raise ValueError(f"invariant spec needs 'command' or 'probe': {spec!r}")
            invariant_set.add(
                spec["description"],
                probe,
                definition={
                    "type": "command",
                    "command": spec.get("command"),
                    "required": bool(spec.get("required", True)),
                },
            )
        return invariant_set

    @staticmethod
    def rewrite_to_shadow(command: str, branch: Any) -> str:
        """Rewrite host paths to the shadow mount visible to sandbox execution."""
        real_root = os.path.realpath(branch.base_workspace.root)
        shadow_root = os.path.realpath(branch.shadow_workspace.root)
        if real_root == shadow_root:
            return command
        return command.replace(real_root, "/workspace")
