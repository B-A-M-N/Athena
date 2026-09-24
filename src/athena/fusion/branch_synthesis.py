"""Branch-bound generated-capability admission for Fusion.

This mechanism owns proof/identity/workspace/environment checks for synthesis
requests that target a shadow branch. It does not promote or commit a branch;
the Fusion orchestrator and canonical capability/reality seams retain those
authorities.
"""

from __future__ import annotations

import os
from typing import Any


__all__ = ["BranchSynthesisMechanism"]


class BranchSynthesisMechanism:
    """Prepare and admit a generated capability against one exact branch."""

    def __init__(
        self,
        *,
        shadow: Any,
        synthesis: Any,
        dispatcher: Any,
        fabric: Any,
        verification_environment: Any,
        verification_environment_resolver: Any,
        synthesis_ref: Any,
    ) -> None:
        self._shadow = shadow
        self._synthesis = synthesis
        self._dispatcher = dispatcher
        self._fabric = fabric
        self._verification_environment = verification_environment
        self._verification_environment_resolver = verification_environment_resolver
        self._synthesis_ref = synthesis_ref

    async def run(
        self,
        registry: Any,
        *,
        name: str,
        description: str,
        code: str,
        input_schema: dict,
        effects: set,
        task_id: str | None,
        validation_cases: list[dict],
        branch_id: str | None = None,
        workspace: Any | None = None,
    ) -> dict[str, Any]:
        from athena.synthesis.engine import SynthesisEngine

        engine = self._synthesis
        if engine is None:
            engine = SynthesisEngine()
            if self._synthesis_ref is not None:
                self._synthesis_ref.set(engine)
        engine.bind_dispatcher(self._dispatcher)

        branch = self._shadow.get_branch(branch_id) if branch_id else None
        self._validate_branch(branch, branch_id, task_id, workspace)
        effective_workspace = workspace or (branch.shadow_workspace if branch else None)
        branch_record = self._branch_record(branch, task_id)
        environment = self._verification_environment
        resolver = self._verification_environment_resolver
        if environment is None and resolver is not None:
            try:
                environment = resolver(task_id)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise ValueError(f"verification environment is unavailable: {exc}") from exc
        environment_record = environment.to_record() if environment is not None else None
        workspace_fingerprint = await self._workspace_fingerprint(
            effective_workspace, branch_record
        )
        provenance = {
            "origin": "shadow_experiment",
            "branch_id": branch.id if branch is not None else None,
            "workspace_fingerprint": workspace_fingerprint,
            "verification_environment": environment_record,
        }
        cap = engine.synthesize(
            name=name,
            description=description,
            code=code,
            input_schema=input_schema,
            effects=effects,
            task_id=task_id,
            provenance=provenance,
        )
        cap = await engine.validate(
            cap,
            validation_cases,
            workspace_root=getattr(effective_workspace, "root", None),
            workspace=effective_workspace,
            task_id=task_id,
        )
        surface = self._fabric or registry
        admitted = engine.register_ephemeral(surface, cap)
        candidate = engine.to_skill_candidate(cap.id) if cap.validation.get("all_passed") else None
        return {
            "capability_id": cap.id,
            "admitted": admitted,
            "validation": cap.validation,
            "proof": engine.proof_for(cap.id),
            "skill_candidate_proposed": candidate is not None,
            "branch_id": branch.id if branch is not None else None,
            "workspace_fingerprint": workspace_fingerprint,
            "verification_environment": environment_record,
        }

    @staticmethod
    def _validate_branch(
        branch: Any, branch_id: str | None, task_id: str | None, workspace: Any | None
    ) -> None:
        if branch_id and (branch is None or branch.task_id != task_id):
            raise ValueError("synthesis branch is unavailable for this task")
        if branch is None:
            return
        effective_workspace = workspace or branch.shadow_workspace
        if effective_workspace is not branch.shadow_workspace:
            raise ValueError("synthesis workspace must be the selected branch workspace")
        if effective_workspace is None or not os.path.isdir(effective_workspace.root):
            raise ValueError("synthesis branch workspace is unavailable")
        if not branch.verification_certificate:
            raise ValueError("synthesis branch has no verification certificate")

    @staticmethod
    def _branch_record(branch: Any, task_id: str | None) -> dict[str, Any] | None:
        if branch is None:
            return None
        record = branch.verification_certificate.to_record()
        if record.get("task_id") != task_id or record.get("branch_id") != branch.id:
            raise ValueError("synthesis branch proof identity does not match the selected branch")
        return record

    async def _workspace_fingerprint(
        self, workspace: Any | None, branch_record: dict[str, Any] | None
    ) -> str | None:
        if workspace is None:
            return None
        fingerprint = await self._shadow.workspace_fingerprint(workspace.root)
        if branch_record and branch_record.get("candidate_fingerprint"):
            if fingerprint != branch_record["candidate_fingerprint"]:
                raise ValueError("synthesis branch workspace fingerprint changed before validation")
        return fingerprint
