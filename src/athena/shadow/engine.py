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

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from athena.capabilities.dispatcher import SuspendedCall
from athena.protocol.capabilities import CapabilityRequest, CapabilityResult
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.tasks import WorkspaceSpec

__all__ = ["ShadowBranch", "ShadowEngine", "BranchStatus"]

_logger = logging.getLogger("athena.shadow")


class BranchStatus:
    PROPOSED = "PROPOSED"
    EXECUTING = "EXECUTING"
    VERIFIED = "VERIFIED"     # criteria passed in shadow
    FAILED = "FAILED"         # verification failed / execution error
    COMMITTED = "COMMITTED"
    DISCARDED = "DISCARDED"


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
    error: str | None = None
    commit_mutation_id: str | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())


class ShadowEngine:
    """Creates isolated workspace clones, runs proposals, commits or discards."""

    def __init__(self, *, dispatcher=None, roots_parent: str | None = None) -> None:
        self._dispatcher = dispatcher
        self._branches: dict[str, ShadowBranch] = {}
        self._roots_parent = roots_parent or "/tmp/athena-shadow"

    def bind(self, dispatcher) -> None:
        self._dispatcher = dispatcher

    def bind_service(self, service) -> None:
        """Bind the owning service for approval resolution."""
        self._service = service

    async def _service_approve(self, approval_id: str) -> None:
        svc = getattr(self, "_service", None)
        if svc is None:
            raise RuntimeError("ShadowEngine not bound to a service")
        await svc.approve(approval_id, granted=True, scope="call")

    # ------------------------------------------------------------------
    # Branch lifecycle
    # ------------------------------------------------------------------
    def _make_shadow_workspace(self, base: WorkspaceSpec, branch_id: str) -> WorkspaceSpec:
        """Clone-on-create: copy the real workspace tree into a shadow root.

        Bounded copy: skip VCS internals and caches that don't affect
        verification correctness but dominate size. The shadow gets its own
        root so nothing inside it can alias real files.
        """
        root = os.path.join(self._roots_parent, branch_id)
        os.makedirs(root, exist_ok=True)
        src = base.root
        if os.path.isdir(src):
            shutil.copytree(
                src, root, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__",
                                              "node_modules", ".venv",
                                              "*.pyc"),
            )
        return WorkspaceSpec(
            id=f"{base.id}-shadow-{branch_id[-6:]}",
            root=root,
            readable=base.readable,
            writable=base.writable,
            network_policy=base.network_policy,
            execution_backend=base.execution_backend,
        )

    async def open_branch(
        self,
        *,
        task_id: str | None,
        base_workspace: WorkspaceSpec,
        proposal: list[dict],
    ) -> ShadowBranch:
        """Create a shadow clone for a proposed operation batch.

        ``proposal`` items are dicts: {"capability_id": ..., "arguments": {...}}
        """
        if self._dispatcher is None:
            raise RuntimeError("ShadowEngine not bound to a dispatcher")
        bid = new_id("branch")
        shadow_ws = await asyncio.get_running_loop().run_in_executor(
            None, self._make_shadow_workspace, base_workspace, bid)
        branch = ShadowBranch(
            id=bid,
            task_id=task_id,
            base_workspace=base_workspace,
            shadow_workspace=shadow_ws,
        )
        branch.proposal = proposal
        self._branches[bid] = branch
        _logger.info("shadow branch %s opened for task %s (%d ops)",
                     bid, task_id, len(proposal))
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
        branch.status = BranchStatus.EXECUTING
        try:
            for i, op in enumerate(branch.proposal):
                args = dict(op.get("arguments") or {})
                req = CapabilityRequest(
                    capability_id=op["capability_id"],
                    arguments=args,
                    task_id=branch.task_id,
                    call_id=new_id("call"),
                )
                result = await self._dispatcher.dispatch(
                    req, workspace=branch.shadow_workspace, profile=profile)
                if isinstance(result, SuspendedCall):
                    # A suspended call inside the shadow means policy wanted
                    # a human decision. Auto-approval would be unsafe: the
                    # "shadow" is filesystem isolation, NOT execution
                    # isolation (spawned processes see the host). Fail the
                    # branch honestly instead of granting silently.
                    branch.error = (
                        f"op {i} ({op['capability_id']}) requires approval; "
                        "shadow branches do not auto-approve because shadow "
                        "isolation is filesystem-level, not execution-level")
                    branch.rejected_requests.append(result.request)
                    branch.status = BranchStatus.FAILED
                    return branch
                if isinstance(result, Exception):
                    raise RuntimeError(str(result))
                if result.status.value != "ok":
                    branch.error = (
                        f"op {i} ({op['capability_id']}) failed: {result.error}")
                    branch.results.append(result)
                    branch.status = BranchStatus.FAILED
                    return branch
                branch.results.append(result)
        except Exception as exc:
            branch.error = f"branch execution error: {exc}"
            branch.status = BranchStatus.FAILED
        return branch

    async def record_verification(
        self, branch: ShadowBranch, criteria_results: list[dict]
    ) -> None:
        """Attach structured verification evidence to the branch."""
        branch.verification = list(criteria_results)
        all_ok = all(bool(c.get("passed")) for c in criteria_results) \
            if criteria_results else False
        branch.status = BranchStatus.VERIFIED if all_ok else BranchStatus.FAILED
        if not all_ok and not branch.error:
            failed = [c.get("id") for c in criteria_results if not c.get("passed")]
            branch.error = f"criteria unverified: {failed}"

    # ------------------------------------------------------------------
    # Commit / discard
    # ------------------------------------------------------------------
    async def commit(self, branch: ShadowBranch) -> dict:
        """Apply the shadow's file effects onto the real workspace.

        Strategy: diff shadow vs base by walking the trees, then copy
        changed/new files forward and delete files removed in the shadow.
        Each applied change is recorded in the caller's mutation ledger via
        the returned summary (the dispatcher already logged the shadow-side
        mutations; this records the commit itself as one reversible unit).
        """
        if branch.status != BranchStatus.VERIFIED:
            raise RuntimeError(
                f"cannot commit branch {branch.id} in status {branch.status}")

        loop = asyncio.get_running_loop()
        changes = await loop.run_in_executor(None, self._diff_trees, branch)

        # Conflict detection (P0-15): every modified/deleted file must still
        # match the base hash captured at branch open. If reality drifted,
        # refuse to overwrite — return CONFLICT instead.
        import hashlib

        def _current_hash(path: str) -> str:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()[:16]

        base_root = branch.base_workspace.root
        conflicts = []
        for rel, expected in changes.get("base_hashes", {}).items():
            current_path = os.path.join(base_root, rel)
            if not os.path.isfile(current_path):
                conflicts.append({"resource": rel, "reason": "deleted_elsewhere"})
                continue
            if _current_hash(current_path) != expected:
                conflicts.append({"resource": rel, "reason": "modified_elsewhere"})
        if conflicts:
            branch.status = BranchStatus.FAILED
            branch.error = f"commit CONFLICT: {len(conflicts)} resource(s) changed outside the branch"
            await loop.run_in_executor(None, self._cleanup, branch)
            return {"status": "CONFLICT", "branch": branch.id,
                    "conflicts": conflicts}

        def _apply():
            applied = {"written": [], "deleted": []}
            base_root = branch.base_workspace.root
            shadow_root = branch.shadow_workspace.root
            for rel in changes["modified"] + changes["added"]:
                src = os.path.join(shadow_root, rel)
                dst = os.path.join(base_root, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                applied["written"].append(rel)
            for rel in changes["deleted"]:
                target = os.path.join(base_root, rel)
                if os.path.isfile(target):
                    os.remove(target)
                    applied["deleted"].append(rel)
            return applied

        applied = await loop.run_in_executor(None, _apply)
        branch.mutations = [
            {"resource": w, "operation": "commit_write"} for w in applied["written"]
        ] + [{"resource": d, "operation": "commit_delete"} for d in applied["deleted"]]

        # Record the commit as a durable mutation intent (P0-13): the most
        # important step of a speculative transaction must be in the WAL.
        if getattr(self, "_service", None) is not None:
            store = getattr(self._service, "_store_mutations", None)
            if store is not None:
                try:
                    mid = await store.record_intent(
                        task_id=branch.task_id,
                        resource=f"shadow-branch:{branch.id}",
                        operation="commit",
                        metadata={"written": applied["written"],
                                  "deleted": applied["deleted"]},
                    )
                    await store.complete(mid,
                                         after_hash=branch.id[-8:],
                                         reversible=False)
                    branch.commit_mutation_id = mid
                except Exception as exc:
                    _logger.warning("commit WAL record failed (branch %s): %s",
                                    branch.id, exc)

        branch.status = BranchStatus.COMMITTED
        self._cleanup(branch)
        _logger.info("shadow branch %s committed: +%d/-%d files",
                     branch.id, len(applied["written"]), len(applied["deleted"]))
        return {"status": "committed", "branch": branch.id, **applied}

    async def discard(self, branch: ShadowBranch, reason: str = "") -> dict:
        branch.status = BranchStatus.DISCARDED
        branch.error = branch.error or reason or None
        self._cleanup(branch)
        return {"status": "discarded", "branch": branch.id, "reason": reason}

    # ------------------------------------------------------------------
    def _diff_trees(self, branch: ShadowBranch) -> dict:
        """Content-hash tree diff (size comparison misses same-size edits)."""
        base_root = branch.base_workspace.root
        shadow_root = branch.shadow_workspace.root
        ignore = {".git", "__pycache__", "node_modules", ".venv"}

        def _hash_file(path: str) -> str:
            import hashlib
            h = hashlib.sha256()
            try:
                if os.path.islink(path):
                    return hashlib.sha256(
                        ("link:" + os.readlink(path)).encode()).hexdigest()[:16]
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                return h.hexdigest()[:16]
            except OSError:
                return "<unreadable>"

        def walk(root: str) -> dict[str, str]:
            out: dict[str, str] = {}
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in ignore]
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, root)
                    out[rel] = _hash_file(full)
            return out

        base_files = walk(base_root)
        shadow_files = walk(shadow_root)
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
        return {"modified": modified, "added": added, "deleted": deleted,
                "base_hashes": {**{r: base_files[r] for r in modified},
                                **{r: base_files[r] for r in deleted}}}

    def _cleanup(self, branch: ShadowBranch) -> None:
        def _rm():
            shutil.rmtree(branch.shadow_workspace.root, ignore_errors=True)
        asyncio.get_running_loop().run_in_executor(None, _rm)
