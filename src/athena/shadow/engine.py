"""Shadow execution: speculative branches of ONE agent (BUILDSPEC fusion).

A task can propose a batch of operations, run them against a SHADOW
workspace (an isolated copy-on-write clone), verify acceptance criteria
there through the same OI execution plane and policy boundary, then either:

  COMMIT   - apply the proven mutation set to the real workspace, or
  DISCARD  - drop the entire branch; reality untouched.

This is an execution transaction at the agent-lifecycle level:
    hypothesize -> shadow execute -> prove -> commit.
Not chat branching. Not subagents. One intelligence experimenting safely.

All state is durable (mutation ledger + events) so branches are auditable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena.protocol.continuations import SuspendedCall
from athena.shadow.persistence import (
    branch_record as _branch_record,
    commit_plan_digest as _commit_plan_digest,
    commit_plan_record as _commit_plan_record,
    environment_fingerprint as _environment_fingerprint,
    manifest_fingerprint as _manifest_fingerprint,
    rebase_rules as _rebase_rules,
    resource_kind as _resource_kind,
    unsupported_commit_resources as _unsupported_commit_resources,
    workspace_from_record as _workspace_from_record,
    environment_record as _environment_record,
)
from athena.shadow.commit_mechanisms import (
    apply_commit_plan,
    build_commit_plan,
    detect_stale_base_conflict,
    rollback_partial_commit,
    validate_commit_certificate,
)
from athena.causal.checkpoint import run_checkpoint_worker
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    DispatchDirectives,
)
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.tasks import MutationMode, NetworkPolicy, WorkspaceSpec
from athena.workspace_manifest import (
    content_manifest,
    copy_ignore,
    copy_workspace_tree,
)
from athena.verification.certificate import (
    VerificationCertificate,
)

__all__ = [
    "BranchStatus",
    "ShadowBranch",
    "ShadowEngine",
    "VerificationCertificate",
]

_logger = logging.getLogger("athena.shadow")


def _read_utf8(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _read_commit_bytes(path: str) -> bytes:
    """Read only regular candidate files for canonical commit planning."""
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("candidate commit supports regular files only")
    with open(path, "rb") as handle:
        return handle.read()


def _candidate_mode(shadow_root: str, relative: str) -> int:
    path = relative if os.path.isabs(relative) else os.path.join(shadow_root, relative)
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("candidate commit supports regular files only")
    return stat.S_IMODE(os.stat(path).st_mode)


class BranchStatus:
    PROPOSED = "PROPOSED"
    EXECUTING = "EXECUTING"
    VERIFIED = "VERIFIED"  # criteria passed in shadow
    COMMITTING = "COMMITTING"  # real-workspace mutation is in flight
    FAILED = "FAILED"  # verification failed / execution error
    CONFLICTED = "CONFLICTED"  # real workspace drifted; candidate retained
    COMMITTED = "COMMITTED"
    DISCARDED = "DISCARDED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass
class ShadowBranch:
    """One speculative branch of a task."""

    id: str
    task_id: str | None
    base_workspace: WorkspaceSpec
    shadow_workspace: WorkspaceSpec
    proposal: list[dict] = field(default_factory=list)
    status: str = BranchStatus.PROPOSED
    results: list[CapabilityResult] = field(default_factory=list)
    rejected_requests: list[CapabilityRequest] = field(default_factory=list)
    mutations: list[dict] = field(default_factory=list)
    verification: list[dict] = field(default_factory=list)
    verification_started_at: str | None = None
    unsupported_resources: list[dict[str, str]] = field(default_factory=list)
    verification_certificate: VerificationCertificate = field(
        default_factory=VerificationCertificate.empty
    )
    # Immutable snapshot of the real workspace at branch creation. Commit
    # conflict checks must compare reality with this, never with a manifest
    # freshly captured after the branch has already run.
    base_manifest: dict[str, str] = field(default_factory=dict)
    # Full content hashes are the immutable per-resource CAS values used at
    # commit time; the compact manifest also encodes type and mode.
    base_preimages: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    commit_mutation_id: str | None = None
    commit_plan: list[dict] = field(default_factory=list)
    commit_outcome: dict = field(default_factory=dict)
    commit_state: str = "NOT_STARTED"
    commit_started_at: str | None = None
    commit_completed_at: str | None = None
    checkpoint_id: str | None = None
    policy_profile: str | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())


@dataclass(frozen=True)
class DiscardDecision:
    allowed: bool
    reason: str


class ShadowEngine:
    """Creates isolated workspace clones, runs proposals, commits or discards."""

    def __init__(
        self,
        *,
        dispatcher=None,
        roots_parent: str | None = None,
        state_root: str | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._branches: dict[str, ShadowBranch] = {}
        self._roots_parent = Path(
            roots_parent
            or (os.path.join(state_root, "shadows") if state_root else "/tmp/athena-shadow")
        )
        self._state_root = Path(state_root or self._roots_parent)
        self._branch_state = self._state_root / "branches.json"
        self._load_branches()

    def bind(self, dispatcher) -> None:
        self._dispatcher = dispatcher

    dispatcher = property(lambda engine: engine._dispatcher)

    def bind_service(self, service) -> None:
        self._service = service

    def get_branch(self, branch_id: str) -> ShadowBranch | None:
        """Return an in-process branch by ID for status/control operations."""
        return self._branches.get(branch_id)

    def list_branches(self) -> list[ShadowBranch]:
        return list(self._branches.values())

    def attach_checkpoint(self, branch: ShadowBranch, checkpoint_id: str | None) -> None:
        """Persist the checkpoint that defines the branch's restore boundary."""
        branch.checkpoint_id = checkpoint_id
        self._persist_branches()

    async def _service_approve(self, approval_id: str) -> None:
        svc = getattr(self, "_service", None)
        if svc is None:
            raise RuntimeError("ShadowEngine not bound to a service")
        await svc.approve(approval_id, granted=True, scope="call")

    async def _request_candidate_apply_approval(
        self, branch: ShadowBranch, plan_digest: str
    ) -> str | None:
        service = getattr(self, "_service", None)
        requester = getattr(service, "request_candidate_apply_approval", None)
        if not callable(requester):
            return None
        return await requester(branch, plan_digest=plan_digest)

    # ------------------------------------------------------------------
    # Branch lifecycle
    # ------------------------------------------------------------------
    def _make_shadow_workspace(
        self,
        base: WorkspaceSpec,
        branch_id: str,
    ) -> tuple[WorkspaceSpec, dict[str, str]]:
        """Clone-on-create: copy the real workspace tree into a shadow root.

        Bounded copy: skip VCS internals and caches that don't affect
        verification correctness but dominate size. The shadow gets its own
        root so nothing inside it can alias real files.
        """
        root = os.path.join(self._roots_parent, branch_id)
        os.makedirs(root, exist_ok=True)
        src = base.root
        # Capture the base at the same lifecycle boundary as branch creation.
        # Capturing after copytree would leave a race window in which a real
        # workspace edit could be mistaken for the branch's own starting
        # state.
        base_manifest = self._manifest(src)
        if os.path.isdir(src):
            copy_workspace_tree(
                src,
                root,
                dirs_exist_ok=True,
                ignore=copy_ignore,
            )
        return self._workspace_for_branch(base, branch_id), base_manifest

    def _workspace_for_branch(
        self,
        base: WorkspaceSpec,
        branch_id: str,
    ) -> WorkspaceSpec:
        """Build the child workspace contract for a shadow branch."""
        root = os.path.join(self._roots_parent, branch_id)
        return WorkspaceSpec(
            id=f"{base.id}-shadow-{branch_id[-6:]}",
            root=root,
            readable=_rebase_rules(base.readable, base.root, root),
            writable=_rebase_rules(base.writable, base.root, root),
            # A copied directory is not an isolation boundary.  The branch
            # therefore requests the sandbox backend and denies outbound
            # networking; execution must fail closed if that backend is not
            # available.
            network_policy=NetworkPolicy.DENY,
            execution_backend="shadow",
            mutation_mode=MutationMode.DIRECT,
            revision=base.revision,
        )

    async def preflight_clone_transport(self) -> dict[str, str]:
        """Prove the actual clone path using a disposable probe workspace.

        A manifest scan proves hashing, not copying.  This probe creates a
        tiny controlled source beneath ``_roots_parent``, invokes the same
        checkpoint worker clone operation as :meth:`open_branch`, verifies an
        independent destination, and removes both sides even on failure.
        """
        probe_id = f"clone-probe-{uuid.uuid4().hex[:12]}"
        source = self._roots_parent / f"{probe_id}-source"
        clone = self._roots_parent / probe_id
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            (source / "athena-clone-probe.txt").write_text("clone-ready\n", encoding="utf-8")
            result = await run_checkpoint_worker(
                "clone",
                root=str(self._roots_parent),
                checkpoint_id=probe_id,
                workspace_root=str(source),
            )
            clone_file = clone / "athena-clone-probe.txt"
            if (
                not clone_file.is_file()
                or clone_file.read_text(encoding="utf-8") != "clone-ready\n"
            ):
                raise RuntimeError("checkpoint clone did not independently copy probe data")
            if dict(result.get("base_manifest") or {}).get("athena-clone-probe.txt") is None:
                raise RuntimeError("checkpoint clone did not produce a base manifest")
            return {
                "source_root": str(source),
                "clone_root": str(clone),
                "base_manifest_hash": str(result.get("base_manifest", {}).get("hash", "")),
            }
        finally:
            shutil.rmtree(source, ignore_errors=True)
            if clone.is_symlink():
                clone.unlink(missing_ok=True)
            elif clone.exists():
                shutil.rmtree(clone, ignore_errors=True)

    async def open_branch(
        self,
        *,
        task_id: str | None,
        base_workspace: WorkspaceSpec,
        proposal: list[dict],
        profile: str | None = None,
    ) -> ShadowBranch:
        """Create a shadow clone for a proposed operation batch.

        ``proposal`` items are dicts: {"capability_id": ..., "arguments": {...}}
        """
        if self._dispatcher is None:
            raise RuntimeError("ShadowEngine not bound to a dispatcher")
        bid = new_id("branch")
        worker_result = await run_checkpoint_worker(
            "clone",
            root=str(self._roots_parent),
            checkpoint_id=bid,
            workspace_root=base_workspace.root,
        )
        base_manifest = dict(worker_result.get("base_manifest") or {})
        base_preimages = dict(worker_result.get("base_preimages") or {})
        shadow_ws = self._workspace_for_branch(base_workspace, bid)
        branch = ShadowBranch(
            id=bid,
            task_id=task_id,
            base_workspace=base_workspace,
            shadow_workspace=shadow_ws,
            policy_profile=profile,
            base_manifest=base_manifest,
            base_preimages=base_preimages,
        )
        branch.proposal = proposal
        self._branches[bid] = branch
        self._persist_branches()
        _logger.info("shadow branch %s opened for task %s (%d ops)", bid, task_id, len(proposal))
        return branch

    async def execute_branch(
        self,
        branch: ShadowBranch,
        *,
        profile: str | None = None,
    ) -> ShadowBranch:
        """Run every proposed operation inside the SHADOW workspace.

        Uses the SAME dispatcher -> registry -> policy path as real work;
        the only difference is the workspace bound into each request.
        """
        # The commit phase must evaluate the exact same requested policy
        # profile as the speculative execution phase.  Without persisting
        # this, callers that supply the profile here would silently fall back
        # to the service default during commit (usually supervised), turning a
        # proven branch into an approval suspension at the final mutation
        # boundary.
        if profile is not None:
            branch.policy_profile = profile
        branch.status = BranchStatus.EXECUTING
        self._persist_branches()
        try:
            for i, op in enumerate(branch.proposal):
                args = dict(op.get("arguments") or {})
                req = CapabilityRequest(
                    capability_id=op["capability_id"],
                    arguments=args,
                    task_id=branch.task_id,
                    call_id=new_id("call"),
                    origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
                )
                result = await self._dispatcher.dispatch(
                    req, workspace=branch.shadow_workspace, profile=profile
                )
                if isinstance(result, SuspendedCall):
                    # A suspended call inside the shadow means policy wanted
                    # a human decision. Auto-approval would be unsafe: the
                    # "shadow" is filesystem isolation, NOT execution
                    # isolation (spawned processes see the host). Fail the
                    # branch honestly instead of granting silently.
                    branch.error = (
                        f"op {i} ({op['capability_id']}) requires approval; "
                        "shadow branches do not auto-approve because shadow "
                        "isolation is filesystem-level, not execution-level"
                    )
                    branch.rejected_requests.append(result.request)
                    branch.status = BranchStatus.FAILED
                    self._persist_branches()
                    return branch
                if isinstance(result, Exception):
                    raise result
                if result.status.value != "ok":
                    branch.error = f"op {i} ({op['capability_id']}) failed: {result.error}"
                    branch.results.append(result)
                    branch.status = BranchStatus.FAILED
                    self._persist_branches()
                    return branch
                branch.results.append(result)
                self._persist_branches()
        except Exception as exc:  # noqa: BLE001 - branch must become FAILED
            branch.error = f"branch execution error: {exc}"
            branch.status = BranchStatus.FAILED
            self._persist_branches()
        return branch

    async def record_verification(
        self,
        branch: ShadowBranch,
        criteria_results: list[dict],
        verification_plan: Mapping[str, Any] | None = None,
        proof_authority: Mapping[str, Any] | None = None,
    ) -> None:
        """Attach structured verification evidence to the branch."""
        branch.verification = list(criteria_results)
        branch.verification_started_at = branch.verification_started_at or utcnow().isoformat()
        all_ok = all(bool(c.get("passed")) for c in criteria_results) if criteria_results else False
        branch.status = BranchStatus.VERIFIED if all_ok else BranchStatus.FAILED
        if not all_ok and not branch.error:
            failed = [c.get("id") for c in criteria_results if not c.get("passed")]
            branch.error = f"criteria unverified: {failed}"
        if all_ok:
            changes = await self._diff_trees_async(branch)
            branch.unsupported_resources = _unsupported_commit_resources(
                branch,
                changes,
                shadow_root=branch.shadow_workspace.root,
            )
            if branch.unsupported_resources:
                branch.status = BranchStatus.FAILED
                branch.commit_state = "UNSUPPORTED_RESOURCE"
                branch.error = "candidate contains unsupported resources: " + ", ".join(
                    f"{item['resource']} ({item['kind']})" for item in branch.unsupported_resources
                )
                branch.verification_certificate = VerificationCertificate.empty()
                self._persist_branches()
                return
            candidate_manifest = await self._manifest_async(branch.shadow_workspace.root)
            changed_resources = [
                {
                    "path": relative,
                    "before_hash": branch.base_manifest.get(relative),
                    "after_hash": candidate_manifest.get(relative),
                    "resource_type": _resource_kind(
                        os.path.join(branch.shadow_workspace.root, relative)
                    ),
                }
                for relative in sorted(
                    set(changes["modified"]) | set(changes["added"]) | set(changes["deleted"])
                )
            ]
            environment = _environment_record(
                branch.shadow_workspace,
                branch.policy_profile,
            )
            certificate = {
                "certificate_schema_version": 1,
                "branch_id": branch.id,
                "task_id": branch.task_id,
                "base_fingerprint": _manifest_fingerprint(branch.base_manifest),
                "base_revision": _manifest_fingerprint(branch.base_manifest),
                "candidate_fingerprint": _manifest_fingerprint(candidate_manifest),
                "candidate_revision": _manifest_fingerprint(candidate_manifest),
                "environment_fingerprint": _environment_fingerprint(
                    branch.shadow_workspace,
                    branch.policy_profile,
                ),
                "project_environment": environment,
                "changed_resources": changed_resources,
                "verification_plan_id": str(
                    (verification_plan or {}).get("plan_id") or f"branch:{branch.id}"
                ),
                "project_index_revision": ((verification_plan or {}).get("index_revision")),
                "criteria": list(criteria_results),
                "acceptance_criteria": list(criteria_results),
                "impacted_tests": list((verification_plan or {}).get("impacted_tests") or []),
                "project_invariants": list((verification_plan or {}).get("invariants") or []),
                "verification_strength": str(
                    (verification_plan or {}).get("required_strength") or "standard"
                ),
                "verification_rationale": list((verification_plan or {}).get("rationale") or []),
                "evidence_ids": [],
                "verification_started_at": branch.verification_started_at,
                "verification_completed_at": utcnow().isoformat(),
                "issued_at": utcnow().isoformat(),
            }
            if proof_authority is not None:
                certificate["proof_authority"] = {
                    "source_revision": str(proof_authority.get("source_revision") or ""),
                    "design_bundle_hash": str(proof_authority.get("design_bundle_hash") or ""),
                    "gate_bundle_hash": str(proof_authority.get("gate_bundle_hash") or ""),
                }
            branch.verification_certificate = VerificationCertificate.issue(certificate)
        else:
            branch.verification_certificate = VerificationCertificate.empty()
        self._persist_branches()

    # ------------------------------------------------------------------
    # Commit / discard
    # ------------------------------------------------------------------
    @staticmethod
    def _commit_plan_record_for(
        request: CapabilityRequest, *, expected_preimage: str, mode: int | None
    ) -> dict:
        return _commit_plan_record(
            request,
            expected_preimage=expected_preimage,
            mode=mode,
        )

    async def _validate_commit_certificate(
        self,
        branch: ShadowBranch,
    ) -> tuple[dict | None, str]:
        """Delegate certificate/proof validation to the subordinate mechanism."""
        return await validate_commit_certificate(self, branch)

    async def _build_commit_plan(
        self,
        branch: ShadowBranch,
        changes: dict,
        *,
        approval_id: str | None,
    ) -> tuple[
        list[CapabilityRequest],
        dict[str, DispatchDirectives],
        list[dict],
    ]:
        """Delegate canonical plan construction to the subordinate mechanism."""
        return await build_commit_plan(self, branch, changes, approval_id=approval_id)

    async def commit(
        self,
        branch: ShadowBranch,
        *,
        defer_cleanup: bool = False,
        approval_id: str | None = None,
    ) -> dict:
        """Apply a verified branch through the canonical mutation capability.

        The shadow diff is a *plan*, not permission to mutate the real tree.
        Every write/delete is converted to an ``fs`` capability request and
        dispatched against the base workspace.  Consequently the ordinary
        policy, approval, before-state/WAL, and mutation event boundaries are
        used for the commit itself too.
        """
        if branch.status == BranchStatus.FAILED and branch.commit_state == "UNSUPPORTED_RESOURCE":
            # Unsupported resources are rejected during verification.  Keep
            # commit idempotent for callers that inspect the retained
            # candidate after that early rejection.
            return {
                "status": "UNSUPPORTED_RESOURCE",
                "branch": branch.id,
                "resources": list(branch.unsupported_resources),
                "error": branch.error,
            }
        if branch.status != BranchStatus.VERIFIED:
            raise RuntimeError(f"cannot commit branch {branch.id} in status {branch.status}")

        validation_failure, candidate_fingerprint = await self._validate_commit_certificate(branch)
        if validation_failure is not None:
            return validation_failure

        changes = await self._diff_trees_async(branch)

        unsupported = _unsupported_commit_resources(
            branch,
            changes,
            shadow_root=branch.shadow_workspace.root,
        )
        if unsupported:
            branch.status = BranchStatus.FAILED
            branch.commit_state = "UNSUPPORTED_RESOURCE"
            branch.error = "candidate commit supports regular files only: " + ", ".join(
                f"{item['resource']} ({item['kind']})" for item in unsupported
            )
            branch.commit_outcome = {
                "status": "unsupported_resource",
                "resources": unsupported,
            }
            branch.commit_completed_at = utcnow().isoformat()
            self._persist_branches()
            # Keep the candidate available for explicit discard or a future
            # commit implementation that supports this resource kind.
            return {
                "status": "UNSUPPORTED_RESOURCE",
                "branch": branch.id,
                "resources": unsupported,
                "error": branch.error,
            }

        base_root = branch.base_workspace.root

        # Compare the COMPLETE real workspace against the immutable branch
        # base immediately before the commit intent is persisted.  Checking
        # only resources in the diff would allow an unrelated concurrent edit
        # to invalidate the branch's proof while the candidate was promoted.
        conflict = await detect_stale_base_conflict(self, branch, base_root)
        if conflict is not None:
            return conflict

        requests, directives_by_call_id, commit_plan = await self._build_commit_plan(
            branch,
            changes,
            approval_id=approval_id,
        )

        plan_digest = _commit_plan_digest(commit_plan)
        previous_plan_digest = str(branch.commit_outcome.get("plan_digest") or "")
        if branch.commit_state == "AWAITING_APPROVAL" and previous_plan_digest != plan_digest:
            branch.status = BranchStatus.RECOVERY_REQUIRED
            branch.commit_state = "STALE_COMMIT_PLAN"
            branch.error = "durable candidate commit plan changed; approval cannot be replayed"
            self._persist_branches()
            return {"status": "RECOVERY_REQUIRED", "branch": branch.id, "error": branch.error}

        if changes["deleted"] and approval_id is None:
            branch.commit_plan = commit_plan
            branch.commit_state = "AWAITING_APPROVAL"
            branch.commit_outcome = {
                "status": "approval_required",
                "deleted": list(changes["deleted"]),
                "plan_digest": plan_digest,
            }
            branch.error = "candidate deletion requires operator approval"
            self._persist_branches()
            try:
                candidate_approval_id = await self._request_candidate_apply_approval(
                    branch, plan_digest
                )
            except Exception as exc:  # preserve the candidate if approval persistence fails
                branch.error = f"candidate approval request failed: {exc}"
                self._persist_branches()
                return {
                    "status": "APPROVAL_REQUIRED",
                    "branch": branch.id,
                    "deleted": list(changes["deleted"]),
                    "error": branch.error,
                }
            if not candidate_approval_id:
                branch.error = "candidate approval authority is unavailable"
                self._persist_branches()
                return {
                    "status": "APPROVAL_REQUIRED",
                    "branch": branch.id,
                    "deleted": list(changes["deleted"]),
                    "error": branch.error,
                }
            branch.commit_outcome["approval_id"] = candidate_approval_id
            self._persist_branches()
            return {
                "status": "APPROVAL_REQUIRED",
                "branch": branch.id,
                "deleted": list(changes["deleted"]),
                "approval_id": candidate_approval_id,
                "plan_digest": plan_digest,
                "error": branch.error,
            }
        if changes["deleted"]:
            expected_approval_id = str(branch.commit_outcome.get("approval_id") or "")
            if not expected_approval_id or approval_id != expected_approval_id:
                branch.error = "candidate apply approval does not match the durable commit plan"
                self._persist_branches()
                return {
                    "status": "APPROVAL_REQUIRED",
                    "branch": branch.id,
                    "deleted": list(changes["deleted"]),
                    "error": branch.error,
                }

        applied_result = await apply_commit_plan(
            self,
            branch,
            requests=requests,
            directives_by_call_id=directives_by_call_id,
            changes=changes,
            candidate_fingerprint=candidate_fingerprint,
        )
        if "status" in applied_result:
            # Failure/recovery result from the apply phase.
            return applied_result
        applied = applied_result
        final_fingerprint = str(applied["final_fingerprint"])
        branch.mutations = [
            {"resource": w, "operation": "commit_write"} for w in applied["written"]
        ] + [{"resource": d, "operation": "commit_delete"} for d in applied["deleted"]]

        branch.commit_outcome = {
            **applied,
            "candidate_fingerprint": candidate_fingerprint,
            "final_fingerprint": final_fingerprint,
        }
        branch.commit_state = "COMMIT_PROVEN"
        branch.commit_completed_at = utcnow().isoformat()
        branch.status = BranchStatus.COMMITTED
        self._persist_branches()
        if not defer_cleanup:
            await self._cleanup(branch)
        _logger.info(
            "shadow branch %s committed: +%d/-%d files",
            branch.id,
            len(list(applied["written"])),
            len(list(applied["deleted"])),
        )
        return {
            "status": "committed",
            "branch": branch.id,
            **applied,
            "candidate_fingerprint": candidate_fingerprint,
            "final_fingerprint": final_fingerprint,
        }

    async def _rollback_partial_commit(self, outcomes) -> dict[str, list[str]]:
        """Delegate the rollback phase to the engine's commit mechanisms."""
        return await rollback_partial_commit(self, outcomes)

    async def discard(self, branch: ShadowBranch, reason: str = "") -> dict:
        decision = self.can_discard(branch)
        if not decision.allowed:
            return {
                "status": "RECOVERY_REQUIRED",
                "branch": branch.id,
                "error": decision.reason,
            }
        branch.status = BranchStatus.DISCARDED
        branch.commit_state = "DISCARDED"
        branch.error = branch.error or reason or None
        self._persist_branches()
        await self._cleanup(branch)
        return {"status": "discarded", "branch": branch.id, "reason": reason}

    @staticmethod
    def can_discard(branch: ShadowBranch) -> "DiscardDecision":
        """Classify whether deleting a branch would destroy recovery evidence."""
        blocked = {
            "PLANNED",
            "APPLYING",
            "COMMIT_PROVEN",
            "FINALIZED",
        }
        state = str(branch.commit_state or "NOT_STARTED")
        # A certificate that became stale before any real mutation is still
        # safe to discard.  A recovery-required branch with any other state
        # may contain the only evidence needed to reconcile the checkout.
        if branch.status == BranchStatus.RECOVERY_REQUIRED and state == "STALE_CERTIFICATE":
            return DiscardDecision(True, "stale certificate can be safely discarded")
        if state in blocked:
            return DiscardDecision(
                False,
                f"cannot discard branch {branch.id}: commit state {state} retains recovery evidence",
            )
        if branch.status == BranchStatus.RECOVERY_REQUIRED:
            return DiscardDecision(
                False,
                f"cannot discard branch {branch.id}: recovery-required branch retains evidence",
            )
        if branch.status == BranchStatus.COMMITTING:
            return DiscardDecision(
                False,
                f"cannot discard branch {branch.id}: commit is still in flight",
            )
        return DiscardDecision(True, "discard is safe for this branch state")

    async def finalize_proven(self, branch: ShadowBranch) -> None:
        """Finish cleanup for a commit proven before a process stopped."""
        if branch.status != BranchStatus.COMMITTED:
            return
        if branch.commit_state not in {"COMMIT_PROVEN", "FINALIZED"}:
            return
        await self._cleanup(branch)

    # ------------------------------------------------------------------
    async def _manifest_async(self, root: str) -> dict[str, str]:
        """Read a workspace manifest through the isolated filesystem worker."""
        result = await run_checkpoint_worker(
            "manifest",
            root=str(self._roots_parent),
            workspace_root=root,
        )
        value = result.get("manifest")
        if not isinstance(value, dict):
            raise RuntimeError("manifest worker returned an invalid manifest")
        return {str(key): str(digest) for key, digest in value.items()}

    async def workspace_fingerprint(self, root: str) -> str:
        """Return the canonical complete-tree fingerprint for recovery checks."""
        return _manifest_fingerprint(await self._manifest_async(root))

    async def _diff_trees_async(self, branch: ShadowBranch) -> dict:
        """Compute a branch diff without relying on the default thread pool."""
        base_files = (
            branch.base_manifest
            if branch.base_manifest is not None
            else await self._manifest_async(branch.base_workspace.root)
        )
        shadow_files = await self._manifest_async(branch.shadow_workspace.root)
        return self._diff_from_manifests(base_files, shadow_files)

    def _diff_trees(self, branch: ShadowBranch) -> dict:
        """Content-hash tree diff (size comparison misses same-size edits)."""
        base_root = branch.base_workspace.root
        shadow_root = branch.shadow_workspace.root

        # ``base_manifest`` is the immutable snapshot captured at branch
        # open (commit conflict checks MUST compare against it, never a
        # manifest freshly captured after the branch has run). An empty
        # workspace yields an empty-but-valid manifest, so only ``None``
        # (legacy branches without a snapshot) may take the fallback.
        base_files = (
            branch.base_manifest if branch.base_manifest is not None else self._manifest(base_root)
        )
        shadow_files = self._manifest(shadow_root)
        return self._diff_from_manifests(base_files, shadow_files)

    @staticmethod
    def _diff_from_manifests(
        base_files: Mapping[str, str],
        shadow_files: Mapping[str, str],
    ) -> dict:
        """Build a diff from two already captured immutable manifests."""
        modified, added, deleted = [], [], []
        for rel, digest in shadow_files.items():
            if rel not in base_files:
                added.append(rel)
            elif base_files[rel] != digest:
                modified.append(rel)
        for rel in base_files:
            if rel not in shadow_files:
                deleted.append(rel)
        # Base content hashes recorded for conflict detection at commit.
        return {
            "modified": modified,
            "added": added,
            "deleted": deleted,
            "base_hashes": {
                **{r: base_files[r] for r in modified},
                **{r: base_files[r] for r in deleted},
                **{r: "<missing>" for r in added},
            },
        }

    def _conflicts(
        self,
        base_root: str,
        base_hashes: dict[str, str],
    ) -> list[dict[str, str]]:
        """Report real-workspace edits since a branch's captured base."""
        current_manifest = self._manifest(base_root)
        conflicts: list[dict[str, str]] = []
        for rel, expected in base_hashes.items():
            current = current_manifest.get(rel, "<missing>")
            if expected == "<missing>" and current != "<missing>":
                conflicts.append({"resource": rel, "reason": "created_elsewhere"})
            elif expected != "<missing>" and current != expected:
                reason = "deleted_elsewhere" if current == "<missing>" else "modified_elsewhere"
                conflicts.append({"resource": rel, "reason": reason})
        return conflicts

    @staticmethod
    def _manifest(root: str) -> dict[str, str]:
        """Return content/type hashes for a workspace tree.

        Symlink targets and file contents are represented differently, so a
        same-size replacement or symlink retarget cannot disappear from a
        speculative diff. Directory symlinks are recorded but never followed.
        """
        return content_manifest(root)

    async def _cleanup(self, branch: ShadowBranch) -> None:
        # Shadow trees can be arbitrarily large and their deletion may block
        # on the host filesystem.  Keep that work off Athena's event loop and
        # out of its shared executor, just like checkpoint capture/restore.
        # The worker's delete operation is idempotent and validates the
        # branch id before removing only this exact child of roots_parent.
        await run_checkpoint_worker(
            "delete",
            root=str(self._roots_parent),
            checkpoint_id=branch.id,
            workspace_root=str(self._roots_parent),
        )
        if branch.status == BranchStatus.COMMITTED and branch.commit_state == "COMMIT_PROVEN":
            branch.commit_state = "FINALIZED"
        self._persist_branches()

    async def reconcile_startup(self, event_store=None) -> int:
        """Fail closed on branches interrupted during real-workspace commit.

        A branch in ``COMMITTING``/``PLANNED``/``APPLYING`` state has a
        durable intent but no durable terminal outcome. The mutation ledger
        remains authoritative for individual effects; this layer refuses to
        replay or infer the batch result. An operator can inspect the plan
        and explicitly discard or recover it after reconciling those rows.
        """
        count = 0
        for branch in self._branches.values():
            if branch.status != BranchStatus.COMMITTING and branch.commit_state not in {
                "PLANNED",
                "APPLYING",
            }:
                continue
            branch.status = BranchStatus.RECOVERY_REQUIRED
            branch.commit_state = "RECOVERY_REQUIRED"
            branch.error = (
                "process stopped during branch commit; reconcile the durable "
                "mutation ledger before deciding whether to discard or recover"
            )
            self._persist_branches()
            count += 1
            if event_store is not None:
                try:
                    await event_store.append_event(
                        "ShadowBranchRecoveryRequired",
                        {
                            "branch_id": branch.id,
                            "commit_state": branch.commit_state,
                            "commit_plan": branch.commit_plan,
                            "reason": branch.error,
                        },
                        task_id=branch.task_id,
                    )
                except Exception as exc:  # pragma: no cover - telemetry only
                    _logger.warning(
                        "could not emit shadow recovery event for %s: %s",
                        branch.id,
                        exc,
                    )
        return count

    # ------------------------------------------------------------------
    # Durable branch metadata
    # ------------------------------------------------------------------
    def _persist_branches(self) -> None:
        """Persist branch metadata atomically and fail closed on I/O errors."""
        self._state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        records = [_branch_record(branch) for branch in self._branches.values()]
        tmp = self._branch_state.with_suffix(".tmp")
        payload = json.dumps(records, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._branch_state)
        try:
            directory_fd = os.open(self._state_root, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load_branches(self) -> None:
        """Reload metadata and mark incomplete branches needing reconciliation."""
        try:
            records = json.loads(self._branch_state.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return
        if not isinstance(records, list):
            return
        changed = False
        for record in records:
            if not isinstance(record, dict) or not record.get("id"):
                continue
            try:
                status = str(record.get("status") or BranchStatus.PROPOSED)
                certificate_record = dict(record.get("verification_certificate") or {})
                certificate = VerificationCertificate.from_record(certificate_record)
                if (
                    certificate_record
                    and not certificate.valid()
                    and status
                    in {
                        BranchStatus.PROPOSED,
                        BranchStatus.EXECUTING,
                        BranchStatus.VERIFIED,
                        BranchStatus.COMMITTING,
                        BranchStatus.COMMITTED,
                    }
                ):
                    status = BranchStatus.RECOVERY_REQUIRED
                    record["error"] = (
                        "verification certificate integrity check failed after restart; "
                        "candidate requires reconciliation"
                    )
                    record["commit_state"] = "STALE_CERTIFICATE"
                    changed = True
                shadow_root = str(record["shadow_workspace"]["root"])
                if status in {
                    BranchStatus.PROPOSED,
                    BranchStatus.EXECUTING,
                    BranchStatus.VERIFIED,
                    BranchStatus.COMMITTING,
                } and not os.path.isdir(shadow_root):
                    status = BranchStatus.RECOVERY_REQUIRED
                    record["error"] = (
                        "shadow workspace is missing after restart; "
                        "commit outcome requires operator reconciliation"
                    )
                    changed = True
                branch = ShadowBranch(
                    id=str(record["id"]),
                    task_id=record.get("task_id"),
                    base_workspace=_workspace_from_record(record["base_workspace"]),
                    shadow_workspace=_workspace_from_record(record["shadow_workspace"]),
                    proposal=[dict(item) for item in record.get("proposal") or ()],
                    status=status,
                    verification=[dict(item) for item in record.get("verification") or ()],
                    verification_started_at=record.get("verification_started_at"),
                    unsupported_resources=[
                        dict(item)
                        for item in record.get("unsupported_resources") or ()
                        if isinstance(item, dict)
                    ],
                    verification_certificate=certificate,
                    mutations=[dict(item) for item in record.get("mutations") or ()],
                    base_manifest=dict(record.get("base_manifest") or {}),
                    base_preimages=dict(record.get("base_preimages") or {}),
                    error=record.get("error"),
                    commit_plan=[dict(item) for item in record.get("commit_plan") or ()],
                    commit_outcome=dict(record.get("commit_outcome") or {}),
                    commit_state=str(record.get("commit_state") or "NOT_STARTED"),
                    commit_started_at=record.get("commit_started_at"),
                    commit_completed_at=record.get("commit_completed_at"),
                    checkpoint_id=record.get("checkpoint_id"),
                    policy_profile=record.get("policy_profile"),
                    created_at=str(record.get("created_at") or utcnow().isoformat()),
                )
            except (KeyError, TypeError, ValueError):
                _logger.warning("ignoring malformed shadow branch record")
                continue
            self._branches[branch.id] = branch
        if changed:
            self._persist_branches()
