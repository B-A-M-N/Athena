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
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from athena.capabilities.dispatcher import SuspendedCall
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
)
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.tasks import NetworkPolicy, PathRule, WorkspaceSpec

__all__ = ["BranchStatus", "ShadowBranch", "ShadowEngine"]

_logger = logging.getLogger("athena.shadow")


def _read_utf8(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class BranchStatus:
    PROPOSED = "PROPOSED"
    EXECUTING = "EXECUTING"
    VERIFIED = "VERIFIED"     # criteria passed in shadow
    FAILED = "FAILED"         # verification failed / execution error
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
    # Immutable snapshot of the real workspace at branch creation. Commit
    # conflict checks must compare reality with this, never with a manifest
    # freshly captured after the branch has already run.
    base_manifest: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    commit_mutation_id: str | None = None
    policy_profile: str | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())


class ShadowEngine:
    """Creates isolated workspace clones, runs proposals, commits or discards."""

    def __init__(
        self, *, dispatcher=None, roots_parent: str | None = None,
        state_root: str | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._branches: dict[str, ShadowBranch] = {}
        self._roots_parent = roots_parent or "/tmp/athena-shadow"
        self._state_root = Path(state_root or self._roots_parent)
        self._branch_state = self._state_root / "branches.json"
        self._load_branches()

    def bind(self, dispatcher) -> None:
        self._dispatcher = dispatcher

    def bind_service(self, service) -> None:
        """Bind the owning service for approval resolution."""
        self._service = service

    def get_branch(self, branch_id: str) -> ShadowBranch | None:
        """Return an in-process branch by ID for status/control operations."""
        return self._branches.get(branch_id)

    def list_branches(self) -> list[ShadowBranch]:
        """Return known branch records in creation order."""
        return list(self._branches.values())

    async def _service_approve(self, approval_id: str) -> None:
        svc = getattr(self, "_service", None)
        if svc is None:
            raise RuntimeError("ShadowEngine not bound to a service")
        await svc.approve(approval_id, granted=True, scope="call")

    # ------------------------------------------------------------------
    # Branch lifecycle
    # ------------------------------------------------------------------
    def _make_shadow_workspace(
        self, base: WorkspaceSpec, branch_id: str,
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
            # A copied directory is not an isolation boundary.  The branch
            # therefore requests the sandbox backend and denies outbound
            # networking; execution must fail closed if that backend is not
            # available.
            network_policy=NetworkPolicy.DENY,
                execution_backend="shadow",
        ), base_manifest

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
        shadow_ws, base_manifest = await asyncio.get_running_loop().run_in_executor(
            None, self._make_shadow_workspace, base_workspace, bid)
        branch = ShadowBranch(
            id=bid,
            task_id=task_id,
            base_workspace=base_workspace,
            shadow_workspace=shadow_ws,
            policy_profile=profile,
            base_manifest=base_manifest,
        )
        branch.proposal = proposal
        self._branches[bid] = branch
        self._persist_branches()
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
                    self._persist_branches()
                    return branch
                if isinstance(result, Exception):
                    raise result
                if result.status.value != "ok":
                    branch.error = (
                        f"op {i} ({op['capability_id']}) failed: {result.error}")
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
        self._persist_branches()

    # ------------------------------------------------------------------
    # Commit / discard
    # ------------------------------------------------------------------
    async def commit(self, branch: ShadowBranch) -> dict:
        """Apply a verified branch through the canonical mutation capability.

        The shadow diff is a *plan*, not permission to mutate the real tree.
        Every write/delete is converted to an ``fs`` capability request and
        dispatched against the base workspace.  Consequently the ordinary
        policy, approval, before-state/WAL, and mutation event boundaries are
        used for the commit itself too.
        """
        if branch.status != BranchStatus.VERIFIED:
            raise RuntimeError(
                f"cannot commit branch {branch.id} in status {branch.status}")

        loop = asyncio.get_running_loop()
        changes = await loop.run_in_executor(None, self._diff_trees, branch)

        # Conflict detection (P0-15): every modified/deleted file must still
        # match the base hash captured at branch open. If reality drifted,
        # refuse to overwrite — return CONFLICT instead.
        base_root = branch.base_workspace.root
        conflicts = self._conflicts(
            base_root, changes.get("base_hashes", {}),
        )
        if conflicts:
            branch.status = BranchStatus.FAILED
            branch.error = f"commit CONFLICT: {len(conflicts)} resource(s) changed outside the branch"
            self._persist_branches()
            await self._cleanup(branch)
            return {"status": "CONFLICT", "branch": branch.id,
                    "conflicts": conflicts}

        requests: list[CapabilityRequest] = []
        outcomes: list[CapabilityResult | SuspendedCall] = []
        shadow_root = branch.shadow_workspace.root
        for rel in changes["modified"] + changes["added"]:
            source = os.path.join(shadow_root, rel)
            try:
                content = await asyncio.to_thread(_read_utf8, source)
            except (OSError, UnicodeDecodeError) as exc:
                branch.status = BranchStatus.FAILED
                branch.error = f"cannot create canonical commit plan for {rel}: {exc}"
                self._persist_branches()
                await self._cleanup(branch)
                return {"status": "FAILED", "branch": branch.id,
                        "error": branch.error}
            requests.append(CapabilityRequest(
                capability_id="fs",
                arguments={"operation": "write", "path": rel,
                           "content": content, "create_dirs": True},
                task_id=branch.task_id,
                call_id=new_id("commit"),
                origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
            ))
        for rel in changes["deleted"]:
            requests.append(CapabilityRequest(
                capability_id="fs",
                arguments={"operation": "delete", "path": rel},
                task_id=branch.task_id,
                call_id=new_id("commit"),
                origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
            ))

        if requests:
            if self._dispatcher is None:
                raise RuntimeError("ShadowEngine not bound to a dispatcher")
            outcomes = await self._dispatcher.dispatch_many(
                requests,
                workspace=branch.base_workspace,
                profile=branch.policy_profile,
                preflight=True,
            )
            failed = [item for item in outcomes
                      if isinstance(item, CapabilityResult)
                      and item.status.value != "ok"]
            suspended = [item for item in outcomes if isinstance(item, SuspendedCall)]
            if failed or suspended or len(outcomes) != len(requests):
                branch.status = BranchStatus.FAILED
                if failed:
                    reason = failed[0].error or "capability request failed"
                elif suspended:
                    reason = "commit requires approval"
                else:
                    reason = "commit dispatch returned incomplete results"
                branch.error = f"commit not applied: {reason}"
                self._persist_branches()
                await self._cleanup(branch)
                return {"status": "FAILED", "branch": branch.id,
                        "error": branch.error}

        applied = {
            "written": list(changes["modified"] + changes["added"]),
            "deleted": list(changes["deleted"]),
            "mutation_results": [
                {
                    "mutation_id": (item.metadata or {}).get("mutation", {}).get(
                        "mutation_id"),
                    "mutation_sequence": (item.metadata or {}).get(
                        "mutation_sequence"),
                    "mutation_event_sequence": (item.metadata or {}).get(
                        "mutation_event_sequence"),
                }
                for item in outcomes
                if isinstance(item, CapabilityResult)
            ],
        }
        branch.mutations = [
            {"resource": w, "operation": "commit_write"} for w in applied["written"]
        ] + [{"resource": d, "operation": "commit_delete"} for d in applied["deleted"]]

        branch.status = BranchStatus.COMMITTED
        self._persist_branches()
        await self._cleanup(branch)
        _logger.info("shadow branch %s committed: +%d/-%d files",
                     branch.id, len(applied["written"]), len(applied["deleted"]))
        return {"status": "committed", "branch": branch.id, **applied}

    async def discard(self, branch: ShadowBranch, reason: str = "") -> dict:
        branch.status = BranchStatus.DISCARDED
        branch.error = branch.error or reason or None
        self._persist_branches()
        await self._cleanup(branch)
        return {"status": "discarded", "branch": branch.id, "reason": reason}

    # ------------------------------------------------------------------
    def _diff_trees(self, branch: ShadowBranch) -> dict:
        """Content-hash tree diff (size comparison misses same-size edits)."""
        base_root = branch.base_workspace.root
        shadow_root = branch.shadow_workspace.root

        # ``base_manifest`` is the immutable snapshot captured at branch
        # open (commit conflict checks MUST compare against it, never a
        # manifest freshly captured after the branch has run). An empty
        # workspace yields an empty-but-valid manifest, so only ``None``
        # (legacy branches without a snapshot) may take the fallback.
        base_files = (branch.base_manifest
                      if branch.base_manifest is not None
                      else self._manifest(base_root))
        shadow_files = self._manifest(shadow_root)
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
                "base_hashes": {
                    **{r: base_files[r] for r in modified},
                    **{r: base_files[r] for r in deleted},
                **{r: "<missing>" for r in added},
            }}

    def _conflicts(
        self, base_root: str, base_hashes: dict[str, str],
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
        import hashlib

        ignore = {".git", "__pycache__", "node_modules", ".venv"}

        def resource_hash(path: str) -> str:
            try:
                if os.path.islink(path):
                    value = "link:" + os.readlink(path)
                    return hashlib.sha256(value.encode()).hexdigest()[:16]
                if os.path.isdir(path):
                    return hashlib.sha256(b"directory").hexdigest()[:16]
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
                return digest.hexdigest()[:16]
            except OSError:
                return "<unreadable>"

        manifest: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            kept_dirs: list[str] = []
            for name in dirnames:
                if name in ignore:
                    continue
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    manifest[os.path.relpath(full, root)] = resource_hash(full)
                else:
                    kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in filenames:
                full = os.path.join(dirpath, name)
                manifest[os.path.relpath(full, root)] = resource_hash(full)
        return manifest

    async def _cleanup(self, branch: ShadowBranch) -> None:
        def _rm():
            shutil.rmtree(branch.shadow_workspace.root, ignore_errors=True)
        await asyncio.get_running_loop().run_in_executor(None, _rm)
        self._persist_branches()

    # ------------------------------------------------------------------
    # Durable branch metadata
    # ------------------------------------------------------------------
    def _persist_branches(self) -> None:
        """Persist branch metadata atomically; contents stay in the shadow root."""
        try:
            self._state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            records = [_branch_record(branch) for branch in self._branches.values()]
            tmp = self._branch_state.with_suffix(".tmp")
            tmp.write_text(json.dumps(records, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._branch_state)
        except OSError:
            _logger.warning("could not persist shadow branch state", exc_info=True)

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
                shadow_root = str(record["shadow_workspace"]["root"])
                if status in {
                    BranchStatus.PROPOSED, BranchStatus.EXECUTING,
                    BranchStatus.VERIFIED,
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
                    mutations=[dict(item) for item in record.get("mutations") or ()],
                    base_manifest=dict(record.get("base_manifest") or {}),
                    error=record.get("error"),
                    policy_profile=record.get("policy_profile"),
                    created_at=str(record.get("created_at") or utcnow().isoformat()),
                )
            except (KeyError, TypeError, ValueError):
                _logger.warning("ignoring malformed shadow branch record")
                continue
            self._branches[branch.id] = branch
        if changed:
            self._persist_branches()


def _workspace_record(workspace: WorkspaceSpec) -> dict:
    return {
        "id": workspace.id,
        "root": workspace.root,
        "readable": [{"path": rule.path, "allow": rule.allow} for rule in workspace.readable],
        "writable": [{"path": rule.path, "allow": rule.allow} for rule in workspace.writable],
        "temp_root": workspace.temp_root,
        "execution_backend": workspace.execution_backend,
        "network_policy": workspace.network_policy.value,
    }


def _workspace_from_record(record: dict) -> WorkspaceSpec:
    return WorkspaceSpec(
        id=str(record["id"]),
        root=str(record["root"]),
        readable=tuple(PathRule(**dict(rule)) for rule in record.get("readable") or ()),
        writable=tuple(PathRule(**dict(rule)) for rule in record.get("writable") or ()),
        temp_root=record.get("temp_root"),
        execution_backend=str(record.get("execution_backend") or "local"),
        network_policy=NetworkPolicy(str(record.get("network_policy") or "allow")),
    )


def _branch_record(branch: ShadowBranch) -> dict:
    return {
        "id": branch.id,
        "task_id": branch.task_id,
        "base_workspace": _workspace_record(branch.base_workspace),
        "shadow_workspace": _workspace_record(branch.shadow_workspace),
        "proposal": branch.proposal,
        "status": branch.status,
        "verification": branch.verification,
        "mutations": branch.mutations,
        "base_manifest": branch.base_manifest,
        "error": branch.error,
        "policy_profile": branch.policy_profile,
        "created_at": branch.created_at,
    }
