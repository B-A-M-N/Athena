"""Subordinate commit-plan and proof-validation mechanisms for ShadowEngine.

The engine remains the single commit authority. These helpers own exact
phases of that authority and never decide whether promotion happens.
"""

from __future__ import annotations

import base64
import os

from athena.causal.checkpoint import run_checkpoint_worker
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
)
from athena.protocol.continuations import SuspendedCall
from athena.protocol.tasks import MutationMode, WorkspaceSpec
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.capabilities import DispatchDirectives
from athena.verification.certificate import certificate_digest as _certificate_digest

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from athena.shadow.engine import ShadowBranch, ShadowEngine


def _manifest_fingerprint(manifest: dict[str, str]) -> str:
    from athena.shadow.engine import _manifest_fingerprint

    return _manifest_fingerprint(manifest)


def _environment_fingerprint(workspace, policy_profile):
    from athena.shadow.engine import _environment_fingerprint

    return _environment_fingerprint(workspace, policy_profile)


__all__ = [
    "apply_commit_plan",
    "build_commit_plan",
    "detect_stale_base_conflict",
    "rollback_partial_commit",
    "validate_commit_certificate",
]


async def validate_commit_certificate(
    engine: "ShadowEngine",
    branch: "ShadowBranch",
) -> tuple[dict | None, str]:
    """Validate proof identity and candidate/environment fingerprints.

    Returns ``(failure_response, candidate_fingerprint)``. On failure, branch
    state is durably updated and the candidate is retained or cleaned using
    the engine's existing rules.
    """
    candidate_fingerprint = _manifest_fingerprint(
        await engine._manifest_async(branch.shadow_workspace.root)
    )
    certificate = branch.verification_certificate
    if not certificate:
        branch.status = branch_status("FAILED")
        branch.error = "verification certificate missing"
        engine._persist_branches()
        await engine._cleanup(branch)
        return (
            {"status": "FAILED", "branch": branch.id, "error": branch.error},
            candidate_fingerprint,
        )

    environment_fingerprint = _environment_fingerprint(
        branch.shadow_workspace,
        branch.policy_profile,
    )
    if (
        certificate.get("certificate_hash") != _certificate_digest(certificate)
        or certificate.get("base_fingerprint") != _manifest_fingerprint(branch.base_manifest)
        or certificate.get("candidate_fingerprint") != candidate_fingerprint
        or certificate.get("environment_fingerprint") != environment_fingerprint
    ):
        branch.status = branch_status("RECOVERY_REQUIRED")
        branch.commit_state = "STALE_CERTIFICATE"
        branch.error = "verification certificate stale: candidate or environment changed"
        engine._persist_branches()
        return (
            {
                "status": "STALE_CERTIFICATE",
                "branch": branch.id,
                "error": branch.error,
            },
            candidate_fingerprint,
        )
    return None, candidate_fingerprint


async def build_commit_plan(
    engine: "ShadowEngine",
    branch: "ShadowBranch",
    changes: dict,
    *,
    approval_id: str | None,
) -> tuple[list[CapabilityRequest], dict[str, DispatchDirectives], list[dict]]:
    """Build canonical requests, immutable directives, and the commit plan."""
    base_root = branch.base_workspace.root
    requests: list[CapabilityRequest] = []
    shadow_root = branch.shadow_workspace.root
    for rel in changes["modified"] + changes["added"]:
        try:
            content_result = await run_checkpoint_worker(
                "read",
                root=str(engine._roots_parent),
                workspace_root=shadow_root,
                relative=rel,
            )
            content = base64.b64decode(
                str(content_result["content_base64"]),
                validate=True,
            )
        except (OSError, ValueError) as exc:
            branch.status = branch_status("FAILED")
            branch.error = f"cannot create canonical commit plan for {rel}: {exc}"
            engine._persist_branches()
            await engine._cleanup(branch)
            raise ValueError(branch.error) from exc
        requests.append(
            CapabilityRequest(
                capability_id="fs",
                arguments={
                    "operation": "write",
                    "path": rel,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                    "create_dirs": True,
                },
                task_id=branch.task_id,
                call_id=new_id("commit"),
                origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
                metadata={"_verified_candidate_commit": True},
            )
        )
    for rel in changes["deleted"]:
        requests.append(
            CapabilityRequest(
                capability_id="fs",
                arguments={"operation": "delete", "path": rel},
                task_id=branch.task_id,
                call_id=new_id("commit"),
                origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
                metadata={"_verified_candidate_commit": True},
            )
        )

    directives_by_call_id: dict[str, DispatchDirectives] = {}
    commit_plan: list[dict] = []
    try:
        for request in requests:
            raw_path = str(request.arguments["path"])
            path = raw_path if os.path.isabs(raw_path) else os.path.join(base_root, raw_path)
            relative = os.path.relpath(
                os.path.realpath(path),
                os.path.realpath(base_root),
            ).replace(os.sep, "/")
            expected = branch.base_preimages.get(relative)
            if relative in changes["added"]:
                expected = "<missing>"
            if expected is None:
                branch.status = branch_status("RECOVERY_REQUIRED")
                branch.commit_state = "RECOVERY_REQUIRED"
                branch.error = (
                    "commit lacks an immutable base preimage for "
                    f"{relative}; candidate requires revalidation"
                )
                engine._persist_branches()
                raise ValueError(branch.error)
            modes: dict[str, int] = {}
            mode: int | None = None
            if request.arguments.get("operation") != "delete":
                mode_result = await run_checkpoint_worker(
                    "mode",
                    root=str(engine._roots_parent),
                    workspace_root=branch.shadow_workspace.root,
                    relative=raw_path,
                )
                mode = int(mode_result["mode"])
                modes[os.path.realpath(path)] = mode
            directives_by_call_id[request.call_id] = DispatchDirectives(
                expected_preimages={os.path.realpath(path): expected},
                expected_modes=modes,
                transaction_id=branch.id,
                approval_id=approval_id,
            )
            commit_plan.append(
                engine._commit_plan_record_for(
                    request,
                    expected_preimage=expected,
                    mode=mode,
                )
            )
    except (OSError, ValueError) as exc:
        branch.status = branch_status("FAILED")
        branch.commit_state = "FAILED"
        branch.error = f"cannot establish commit precondition: {exc}"
        branch.commit_completed_at = utcnow().isoformat()
        engine._persist_branches()
        await engine._cleanup(branch)
        raise

    return requests, directives_by_call_id, commit_plan


def branch_status(name: str):
    """Resolve BranchStatus without importing the engine at module load."""
    from athena.shadow.engine import BranchStatus

    return getattr(BranchStatus, name)


async def rollback_partial_commit(
    engine: "ShadowEngine",
    outcomes,
) -> dict[str, list[str]]:
    """Compensate mutations that completed before a batch failure.

    ``dispatch_many`` is preflight-atomic, not mutation-transactional:
    individual filesystem calls can finish before another call fails.
    Every successful call must therefore be undone through the service's
    auditable rollback path before the branch is reported as failed.
    """
    rolled_back: list[str] = []
    errors: list[str] = []
    undo = getattr(getattr(engine, "_service", None), "undo_mutation", None)
    for item in reversed(outcomes):
        if not isinstance(item, CapabilityResult):
            continue
        if item.status.value != "ok":
            continue
        mutation = (item.metadata or {}).get("mutation")
        mutation_id = mutation.get("mutation_id") if isinstance(mutation, dict) else None
        if not mutation_id:
            errors.append("successful mutation had no durable mutation id")
            continue
        if undo is None:
            errors.append(f"no rollback authority for {mutation_id}")
            continue
        try:
            outcome = await undo(mutation_id)
        except Exception as exc:  # noqa: BLE001 - preserve recovery state
            errors.append(f"{mutation_id}: {exc}")
            continue
        if outcome.get("status") != "ok":
            errors.append(f"{mutation_id}: {outcome.get('error', 'rollback failed')}")
        else:
            rolled_back.append(mutation_id)
    return {"rolled_back": rolled_back, "errors": errors}


async def detect_stale_base_conflict(
    engine: "ShadowEngine",
    branch: "ShadowBranch",
    base_root: str,
) -> dict | None:
    """Return a CONFLICT result when reality drifted from the immutable base.

    Compares the COMPLETE real workspace against the branch's creation-time
    manifest immediately before the commit intent is persisted. Checking only
    diff resources would allow an unrelated concurrent edit to invalidate the
    proof while the candidate was being promoted.
    """
    from athena.shadow.engine import BranchStatus
    from athena.shadow.persistence import (
        manifest_conflicts as _manifest_conflicts,
        manifest_fingerprint as _manifest_fingerprint,
    )

    current_base_manifest = await engine._manifest_async(base_root)
    expected_base_fingerprint = _manifest_fingerprint(branch.base_manifest)
    current_base_fingerprint = _manifest_fingerprint(current_base_manifest)
    if current_base_fingerprint == expected_base_fingerprint:
        return None
    conflicts = _manifest_conflicts(branch.base_manifest, current_base_manifest)
    branch.status = BranchStatus.CONFLICTED
    branch.commit_state = "CONFLICTED"
    branch.error = "commit CONFLICT: complete base workspace changed since branch creation"
    branch.commit_outcome = {
        "status": "conflict",
        "expected_base_fingerprint": expected_base_fingerprint,
        "current_base_fingerprint": current_base_fingerprint,
        "conflicts": conflicts,
    }
    engine._persist_branches()
    # Keep the candidate workspace and its durable record: it is the evidence
    # needed for rebase/reverification or explicit discard.
    return {"status": "CONFLICT", "branch": branch.id, "conflicts": conflicts}


async def apply_commit_plan(
    engine: "ShadowEngine",
    branch: "ShadowBranch",
    *,
    requests: list[CapabilityRequest],
    directives_by_call_id: dict[str, DispatchDirectives],
    changes: dict[str, list[str]],
    candidate_fingerprint: str,
) -> dict[str, Any]:
    """Apply the planned mutations and verify the final fingerprint.

    Returns either the applied-mutation record (success) or a canonical
    failure/recovery result. The engine remains the single commit authority;
    this is its apply-and-verify phase.
    """
    from athena.shadow.engine import BranchStatus
    from athena.shadow.persistence import manifest_fingerprint as _manifest_fingerprint
    from athena.protocol.messages import utcnow

    applied: dict[str, object] = {
        "written": list(changes["modified"] + changes["added"]),
        "deleted": list(changes["deleted"]),
        "mutation_results": [],
    }
    base_root = branch.base_workspace.root
    if requests:
        if engine.dispatcher is None:
            raise RuntimeError("ShadowEngine not bound to a dispatcher")
        branch.commit_state = "APPLYING"
        engine._persist_branches()
        # A verified branch is the trusted commit controller. Its canonical fs
        # requests must cross the reality boundary once, against the real base
        # workspace, rather than opening a fresh speculative branch.
        commit_workspace = WorkspaceSpec(
            id=branch.base_workspace.id,
            root=base_root,
            readable=branch.base_workspace.readable,
            writable=branch.base_workspace.writable,
            temp_root=branch.base_workspace.temp_root,
            execution_backend=branch.base_workspace.execution_backend,
            network_policy=branch.base_workspace.network_policy,
            mutation_mode=MutationMode.DIRECT,
            revision=branch.base_workspace.revision,
        )
        outcomes = await engine.dispatcher.dispatch_many(
            requests,
            workspace=commit_workspace,
            profile=branch.policy_profile,
            preflight=True,
            directives_by_call_id=directives_by_call_id,
        )
        failed = [
            item
            for item in outcomes
            if isinstance(item, CapabilityResult) and item.status.value != "ok"
        ]
        suspended = [item for item in outcomes if isinstance(item, SuspendedCall)]
        if failed or suspended or len(outcomes) != len(requests):
            branch.commit_state = "FAILED"
            if failed:
                reason = failed[0].error or "capability request failed"
            elif suspended:
                reason = "commit requires approval"
            else:
                reason = "commit dispatch returned incomplete results"
            rollback = await rollback_partial_commit(engine, outcomes)
            branch.error = f"commit not applied: {reason}"
            if rollback["errors"]:
                branch.error += "; recovery required: " + "; ".join(rollback["errors"])
                branch.commit_state = "RECOVERY_REQUIRED"
            branch.commit_outcome = {
                "status": ("recovery_required" if rollback["errors"] else "failed"),
                "reason": reason,
                "rollback": rollback,
            }
            branch.commit_completed_at = utcnow().isoformat()
            branch.status = (
                BranchStatus.RECOVERY_REQUIRED if rollback["errors"] else BranchStatus.FAILED
            )
            engine._persist_branches()
            if not rollback["errors"]:
                await engine._cleanup(branch)
            return {
                "status": ("RECOVERY_REQUIRED" if rollback["errors"] else "FAILED"),
                "branch": branch.id,
                "error": branch.error,
            }

        applied["mutation_results"] = [
            {
                "mutation_id": (item.metadata or {}).get("mutation", {}).get("mutation_id"),
                "mutation_sequence": (item.metadata or {}).get("mutation_sequence"),
                "mutation_event_sequence": (item.metadata or {}).get("mutation_event_sequence"),
            }
            for item in outcomes
            if isinstance(item, CapabilityResult)
        ]
    final_manifest = await engine._manifest_async(base_root)
    final_fingerprint = _manifest_fingerprint(final_manifest)
    if final_fingerprint != candidate_fingerprint:
        branch.status = BranchStatus.RECOVERY_REQUIRED
        branch.commit_state = "RECOVERY_REQUIRED"
        branch.error = (
            "commit applied but final workspace fingerprint does not match the verified candidate"
        )
        branch.commit_outcome = {
            **applied,
            "status": "recovery_required",
            "candidate_fingerprint": candidate_fingerprint,
            "final_fingerprint": final_fingerprint,
        }
        branch.commit_completed_at = utcnow().isoformat()
        engine._persist_branches()
        # The candidate and commit ledger remain available for operator
        # reconciliation. Never discard evidence after an unproven write.
        return {"status": "RECOVERY_REQUIRED", "branch": branch.id, "error": branch.error}
    applied["final_fingerprint"] = final_fingerprint
    return applied
