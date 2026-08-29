"""The execution reality boundary.

The capability name is not a sufficient safety boundary: ``execute``, a
generated capability, or a workflow can mutate a project without calling
``fs``.  ``RealityGate`` therefore classifies the concrete request at the
dispatcher boundary and binds every project-sensitive operation to one of
four dispositions:

    DIRECT        - the caller opted into immediate, unrecoverable mutation of
                    the real workspace.  No bookkeeping.
    ISOLATED      - a single, independently-discardable shadow clone for one
                    call.  The call observes one coherent copy-on-write reality
                    but never joins the task's sticky candidate branch.
    TRANSACTIONAL - the real workspace is mutated in place, but a checkpoint of
                    the workspace is captured first so a later failure can roll
                    the project back to exactly the pre-operation revision.
    SPECULATIVE   - a task-local, sticky shadow branch that accumulates every
                    project-sensitive operation until the task proves and
                    promotes (or discards) the whole candidate.

The gate is deliberately deterministic and has no model-facing decision.
Selection is driven by an explicit ``tier`` (forced by the caller) when
present, otherwise a conservative default heuristic:

    not project-sensitive            -> DIRECT
    sensitive, already on a branch    -> SPECULATIVE (coherent candidate)
    sensitive, single reversible op   -> ISOLATED
    sensitive, forced transactional    -> TRANSACTIONAL
    sensitive, otherwise              -> SPECULATIVE

This is the escalation ladder the dynamic speculation design points at: the
smallest safe change occupies the cheapest tier, and only a change whose blast
radius actually requires a full candidate promotes to SPECULATIVE.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from athena.causal.checkpoint import _run_worker as _run_checkpoint_worker
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    EffectClass,
)
from athena.protocol.messages import utcnow
from athena.protocol.tasks import MutationMode, WorkspaceSpec
from athena.reality.classification import (
    ExecutionDisposition,
    RealityClassification,
    RealityClassificationInput,
    RealityClassifier,
)


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


class TransactionRecoveryRequired(RuntimeError):
    """The real workspace drifted beyond the transaction's owned revision."""


class RealityGate:
    """Bind speculative / isolated / transactional task actions to reality."""

    _READ_ONLY_OPERATIONS = frozenset(
        {
            "read",
            "list",
            "stat",
            "screen",
            "wait_for",
            "status",
            "inspect",
            "tree",
            "usage",
            "tables",
            "schema",
            "explain",
            "search",
            "recall",
            "sources",
            "evidence",
            "gaps",
            "describe",
            "dependencies",
            "provenance",
            "history",
            "created_this_task",
            "workflows",
            "skills",
            "runtimes",
            "permissions",
            "devices",
            "changed_files",
            "overview",
            "cpu",
            "memory",
            "disk",
            "network",
            "ports",
            "toolchain",
            "services",
            "gpu",
            "env",
        }
    )
    _PROCESS_CAPABILITIES = frozenset(
        {
            "execute",
            "terminal_session",
            "debugger",
            "process",
            "shell",
            "bash",
        }
    )
    _PROJECT_CAPABILITIES = frozenset(
        {
            "database",
            "dependency",
            "fs",
            "workspace",
            "workflow",
            "scratch",
        }
    )
    # Ops whose failure mode is a single, reversible local change are the
    # cheapest safe thing to isolate: one ephemeral shadow, discarded alone.
    _ISOLATABLE_OPERATIONS = frozenset(
        {
            "write",
            "create",
            "mkdir",
            "touch",
            "delete",
            "remove",
            "rename",
            "move",
            "copy",
            "patch",
            "append",
            "update",
            "install",
            "add",
        }
    )

    def __init__(self, shadow_engine, *, checkpoint_manager=None) -> None:
        self._shadow = shadow_engine
        self._checkpoints = checkpoint_manager
        self._active: dict[str, Any] = {}
        self._ephemeral: dict[str, Any] = {}
        self._checkpoint_by_task: dict[str, str] = {}
        self._checkpoint_root_by_task: dict[str, str] = {}
        self._transaction_records: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._classifier = RealityClassifier()
        state_root = getattr(shadow_engine, "state_root", None)
        if state_root is None:
            state_root = getattr(shadow_engine, "_state_root", None)
        self._state_root = Path(state_root) if state_root else None
        self._transaction_state = (
            self._state_root / "reality-transactions.json" if self._state_root is not None else None
        )
        self._load_transaction_state()
        self._rehydrate_active_branches()

    def bind_checkpoint_manager(self, checkpoint_manager) -> None:
        """Attach the causal checkpoint backend used by TRANSACTIONAL tiers."""
        self._checkpoints = checkpoint_manager

    def active_branch(self, task_id: str | None):
        """Return the task's sticky candidate branch, if one exists."""
        return self._active.get(task_id) if task_id else None

    def activate_branch(self, branch: Any) -> None:
        """Retain a verified candidate for operator review after proof."""
        task_id = getattr(branch, "task_id", None)
        if task_id:
            self._active[task_id] = branch

    def active_branches(self) -> tuple[Any, ...]:
        return tuple(self._active.values())

    def ephemeral_branch(self, call_id: str | None):
        """Return a per-call isolated branch, if one was opened for it."""
        return self._ephemeral.get(call_id) if call_id else None

    def checkpoint_id(self, task_id: str | None) -> str | None:
        """Return the task's transactional checkpoint id, if any."""
        return self._checkpoint_by_task.get(task_id) if task_id else None

    def transaction_fingerprint(self, task_id: str | None) -> str | None:
        """Return the exact post-state last produced by a transaction."""
        if task_id is None:
            return None
        value = self._transaction_records.get(task_id, {}).get("last_owned_fingerprint")
        return str(value) if value else None

    def mark_transaction_recovery_required(
        self,
        task_id: str | None,
        *,
        error: str,
    ) -> None:
        """Freeze a transaction after an unverifiable reality transition."""
        if task_id is None or task_id not in self._checkpoint_by_task:
            return
        record = self._transaction_records.setdefault(task_id, {})
        record["state"] = "RECOVERY_REQUIRED"
        record["error"] = str(error)
        record["updated_at"] = utcnow().isoformat()
        self._persist_transaction_state()

    def _rehydrate_active_branches(self) -> None:
        """Reattach durable candidate branches after process restart.

        A branch that was already committing is deliberately not reattached:
        startup reconciliation must first classify that uncertain commit as
        recovery-required.  Reattaching it here could route fresh work into a
        branch whose real-world outcome is unknown.
        """
        list_branches = getattr(self._shadow, "list_branches", None)
        if list_branches is None:
            return
        for branch in list_branches():
            if getattr(branch, "status", None) not in {
                "PROPOSED",
                "EXECUTING",
                "VERIFIED",
            }:
                continue
            task_id = getattr(branch, "task_id", None)
            if task_id:
                self._active[task_id] = branch

    def _load_transaction_state(self) -> None:
        """Restore transactional checkpoint bindings before new work routes."""
        if self._transaction_state is None:
            return
        try:
            records = json.loads(self._transaction_state.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return
        if not isinstance(records, dict):
            return
        for task_id, record in records.items():
            if not isinstance(record, dict):
                continue
            checkpoint_id = record.get("checkpoint_id")
            workspace_root = record.get("workspace_root")
            if checkpoint_id and workspace_root:
                self._checkpoint_by_task[str(task_id)] = str(checkpoint_id)
                self._checkpoint_root_by_task[str(task_id)] = str(workspace_root)
                self._transaction_records[str(task_id)] = {
                    "transaction_id": str(record.get("transaction_id") or task_id),
                    "checkpoint_id": str(checkpoint_id),
                    "workspace_root": str(workspace_root),
                    "base_fingerprint": record.get("base_fingerprint"),
                    "last_owned_fingerprint": record.get("last_owned_fingerprint"),
                    "base_manifest": dict(record.get("base_manifest") or {}),
                    "postconditions": dict(record.get("postconditions") or {}),
                    "resources": [
                        str(resource)
                        for resource in record.get("resources") or ()
                        if isinstance(resource, str)
                    ],
                    "mutation_ids": [
                        str(mutation_id)
                        for mutation_id in record.get("mutation_ids") or ()
                        if isinstance(mutation_id, str)
                    ],
                    "started_at": record.get("started_at"),
                    "updated_at": record.get("updated_at"),
                    "state": str(record.get("state") or "ACTIVE"),
                }

    def _persist_transaction_state(self) -> None:
        """Atomically persist checkpoint bindings used for restart recovery."""
        if self._transaction_state is None:
            return
        self._transaction_state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        records = {
            task_id: {
                "transaction_id": self._transaction_records.get(task_id, {}).get(
                    "transaction_id", task_id
                ),
                "checkpoint_id": checkpoint_id,
                "workspace_root": self._checkpoint_root_by_task.get(task_id),
                "base_fingerprint": self._transaction_records.get(task_id, {}).get(
                    "base_fingerprint"
                ),
                "last_owned_fingerprint": self._transaction_records.get(task_id, {}).get(
                    "last_owned_fingerprint"
                ),
                "base_manifest": self._transaction_records.get(task_id, {}).get(
                    "base_manifest", {}
                ),
                "postconditions": self._transaction_records.get(task_id, {}).get(
                    "postconditions", {}
                ),
                "resources": self._transaction_records.get(task_id, {}).get("resources", []),
                "mutation_ids": self._transaction_records.get(task_id, {}).get("mutation_ids", []),
                "started_at": self._transaction_records.get(task_id, {}).get("started_at"),
                "updated_at": self._transaction_records.get(task_id, {}).get("updated_at"),
                "state": self._transaction_records.get(task_id, {}).get("state", "ACTIVE"),
            }
            for task_id, checkpoint_id in self._checkpoint_by_task.items()
        }
        tmp = self._transaction_state.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(records, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self._transaction_state)
        try:
            directory_fd = os.open(self._transaction_state.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    async def route(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        effects: Mapping[EffectClass, Any]
        | frozenset[EffectClass]
        | tuple[EffectClass, ...]
        | set[EffectClass],
        descriptor: CapabilityDescriptor,
        tier: str | None = None,
    ) -> RealityRoute:
        """Resolve the execution workspace for a concrete request.

        ``effects`` has already passed descriptor and policy resolution.  The
        gate only decides the reality target; it never expands capability
        authority or overrides policy.  ``tier`` may force a specific
        disposition; when omitted a conservative default is chosen.
        """
        mode = _mutation_mode(workspace.mutation_mode)

        # Already on a sticky candidate branch: every workspace-bound action
        # must observe it, regardless of the requested tier, so reads and
        # writes see one coherent reality.
        active = self._active.get(request.task_id) if request.task_id else None
        if active is not None and self._is_workspace_bound(request, descriptor):
            # A generated tool may re-enter the dispatcher through the host
            # bridge while its outer call is already routed to this shadow.
            # Treat that routed workspace as the same candidate instead of
            # rejecting it as a workspace switch.
            if _same_root(workspace.root, active.shadow_workspace.root):
                return RealityRoute(
                    active.shadow_workspace,
                    ExecutionDisposition.SPECULATIVE,
                    transaction_id=active.id,
                )
            if not _same_root(workspace.root, active.base_workspace.root):
                raise PermissionError(
                    "task workspace changed while a speculative transaction is active"
                )
            _translate_workspace_arguments(request, workspace.root, active.shadow_workspace.root)
            return RealityRoute(
                active.shadow_workspace,
                ExecutionDisposition.SPECULATIVE,
                transaction_id=active.id,
            )

        # A transactional task survives a process restart through the durable
        # checkpoint binding. Keep subsequent workspace operations on the real
        # in-place candidate until the task is either accepted or compensated.
        task_id = request.task_id
        transaction_record = self._transaction_records.get(task_id) if task_id else None
        if (
            transaction_record is not None
            and transaction_record.get("state") == "RECOVERY_REQUIRED"
        ):
            raise PermissionError(
                "transaction requires operator recovery before more workspace operations"
            )
        if transaction_record is not None and transaction_record.get("state") == "COMMIT_PROVEN":
            raise PermissionError("transaction completion is pending durable task finalization")
        checkpoint_id = self._checkpoint_by_task.get(task_id) if task_id else None
        if checkpoint_id is not None and self._is_workspace_bound(request, descriptor):
            assert task_id is not None
            checkpoint_root = self._checkpoint_root_by_task.get(task_id)
            if checkpoint_root is not None and not _same_root(workspace.root, checkpoint_root):
                raise PermissionError(
                    "task workspace changed while a transactional checkpoint is active"
                )
            return RealityRoute(
                workspace,
                ExecutionDisposition.TRANSACTIONAL,
                checkpoint_id=checkpoint_id,
            )

        if mode is MutationMode.DIRECT:
            return RealityRoute(workspace, ExecutionDisposition.DIRECT)

        sensitive = self._is_project_sensitive(request, effects, descriptor)
        if not sensitive:
            return RealityRoute(workspace, ExecutionDisposition.DIRECT)
        if mode is MutationMode.READ_ONLY:
            raise PermissionError(
                f"project mutation denied by read-only workspace: {request.capability_id}"
            )
        if request.task_id is None:
            raise PermissionError("speculative project mutations require a task-scoped invocation")

        classification = self.classify(
            request,
            tier,
            effects,
            descriptor,
            workspace=workspace,
        )
        disposition = classification.disposition
        if disposition is ExecutionDisposition.ISOLATED:
            route = await self._route_isolated(request, workspace, descriptor)
            return RealityRoute(
                route.workspace,
                route.disposition,
                route.transaction_id,
                route.checkpoint_id,
                classification,
            )
        if disposition is ExecutionDisposition.TRANSACTIONAL:
            route = await self._route_transactional(request, workspace)
            return RealityRoute(
                route.workspace,
                route.disposition,
                route.transaction_id,
                route.checkpoint_id,
                classification,
            )
        if disposition is ExecutionDisposition.DIRECT:
            return RealityRoute(
                workspace, ExecutionDisposition.DIRECT, classification=classification
            )
        route = await self._route_speculative(request, workspace)
        return RealityRoute(
            route.workspace,
            route.disposition,
            route.transaction_id,
            route.checkpoint_id,
            classification,
        )

    def classify(
        self,
        request: CapabilityRequest,
        tier: str | None,
        effects: Any,
        descriptor: CapabilityDescriptor,
        *,
        workspace: WorkspaceSpec | None = None,
    ) -> RealityClassification:
        """Classify a concrete request using only deterministic facts."""
        effect_set = set(effects or ())
        args = request.arguments or {}
        operation = str(args.get("operation") or args.get("action") or "").casefold()
        capability_id = str(request.capability_id)
        origin = getattr(descriptor.origin, "value", descriptor.origin)
        resources = tuple(
            str(args[key])
            for key in ("path", "destination", "cwd", "workdir")
            if isinstance(args.get(key), str) and args[key]
        )
        path_count = len(resources)
        opaque = (
            capability_id in self._PROCESS_CAPABILITIES
            or EffectClass.EXECUTE in effect_set
            or EffectClass.SPAWN_PROCESS in effect_set
            or origin in {"generated", "project", "user"}
        )
        dangerous = bool(
            effect_set
            & {
                EffectClass.DELETE,
                EffectClass.NETWORK_WRITE,
                EffectClass.PRIVILEGED,
                EffectClass.SECRET_READ,
                EffectClass.EXTERNAL_MESSAGE,
                EffectClass.EXTERNAL_PUBLISH,
                EffectClass.COMPUTER_INPUT,
                EffectClass.FINANCIAL,
            }
        )
        reversible = (
            not dangerous
            and operation in self._ISOLATABLE_OPERATIONS
            and EffectClass.WRITE_LOCAL in effect_set
            and EffectClass.EXTERNAL_MESSAGE not in effect_set
        )
        blast_radius = "localized" if path_count <= 1 else "multi-resource"
        if capability_id in {"workspace", "database"} or path_count > 1:
            blast_radius = "broad"
        facts = RealityClassificationInput(
            capability_id=capability_id,
            operation=operation,
            effects=frozenset(effect_set),
            origin=str(origin),
            persistent_mutation=bool(
                effect_set
                & {
                    EffectClass.WRITE_LOCAL,
                    EffectClass.DELETE,
                    EffectClass.NETWORK_WRITE,
                    EffectClass.EXTERNAL_MESSAGE,
                    EffectClass.EXTERNAL_PUBLISH,
                    EffectClass.COMPUTER_INPUT,
                    EffectClass.FINANCIAL,
                }
            )
            or opaque,
            reversible=reversible and not dangerous,
            target_resources=resources,
            target_breadth=blast_radius,
            command_opacity=opaque,
            process_execution=bool(
                effect_set
                & {
                    EffectClass.EXECUTE,
                    EffectClass.SPAWN_PROCESS,
                }
            ),
            verification_strength="deferred-to-completion",
            prior_failure_signal=False,
            environment_effects=bool(
                effect_set
                & {
                    EffectClass.NETWORK_WRITE,
                    EffectClass.EXTERNAL_MESSAGE,
                    EffectClass.EXTERNAL_PUBLISH,
                    EffectClass.COMPUTER_INPUT,
                    EffectClass.FINANCIAL,
                }
            ),
            task_mode=(
                _mutation_mode(workspace.mutation_mode).value
                if workspace is not None
                else MutationMode.DIRECT.value
            ),
            checkpoint_available=self._checkpoints is not None,
            forced_tier=(str(tier) if tier is not None else None),
        )
        return self._classifier.classify(facts)

    # Compatibility for integrations that used the old private hook.
    def _classify(self, request, tier, effects, descriptor):
        return self.classify(request, tier, effects, descriptor).disposition

    async def _route_isolated(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
        descriptor: CapabilityDescriptor,
    ) -> RealityRoute:
        """Open a per-call ephemeral shadow, discarded independently."""
        call_id = request.call_id
        if call_id is None:
            return await self._route_speculative(request, workspace)
        lock = self._locks.setdefault(call_id, asyncio.Lock())
        async with lock:
            branch = self._ephemeral.get(call_id)
            if branch is None:
                branch = await self._shadow.open_branch(
                    task_id=request.task_id,
                    base_workspace=workspace,
                    proposal=[],
                )
                self._ephemeral[call_id] = branch
            target = branch.shadow_workspace
            _translate_workspace_arguments(request, workspace.root, target.root)
            return RealityRoute(
                target,
                ExecutionDisposition.ISOLATED,
                transaction_id=branch.id,
            )

    async def _route_transactional(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
    ) -> RealityRoute:
        """Capture a real-workspace checkpoint, then mutate in place.

        The real workspace is used directly; the checkpoint id is recorded so
        ``compensate()`` can roll the project back to the exact pre-op state.
        """
        task_id = request.task_id
        if task_id is None:
            return await self._route_speculative(request, workspace)
        if self._checkpoints is None:
            # No checkpoint backend: fall back to the full speculative branch
            # rather than silently performing an unrecoverable in-place change.
            return await self._route_speculative(request, workspace)
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            ckpt_id = self._checkpoint_by_task.get(task_id)
            if ckpt_id is None:
                ckpt = await self._checkpoints.capture(
                    task_id=task_id,
                    workspace_root=workspace.root,
                    label=f"transactional:{task_id}",
                )
                ckpt_id = ckpt["id"]
                self._checkpoint_by_task[task_id] = ckpt_id
                self._checkpoint_root_by_task[task_id] = workspace.root
                base_fingerprint = str(ckpt.get("workspace_fingerprint") or "")
                base_manifest_result = await _run_checkpoint_worker(
                    "manifest",
                    root=workspace.root,
                    workspace_root=workspace.root,
                )
                self._transaction_records[task_id] = {
                    "transaction_id": task_id,
                    "checkpoint_id": ckpt_id,
                    "workspace_root": workspace.root,
                    "base_fingerprint": base_fingerprint,
                    "last_owned_fingerprint": base_fingerprint,
                    "base_manifest": dict(base_manifest_result.get("manifest") or {}),
                    "postconditions": _planned_postconditions(request, workspace.root),
                    "resources": _transaction_resources(request, workspace.root),
                    "mutation_ids": [],
                    "started_at": utcnow().isoformat(),
                    "state": "ACTIVE",
                }
                self._persist_transaction_state()
        return RealityRoute(
            workspace,
            ExecutionDisposition.TRANSACTIONAL,
            checkpoint_id=ckpt_id,
        )

    async def _route_speculative(
        self,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
    ) -> RealityRoute:
        """Lazily open the task's sticky candidate branch."""
        task_id = request.task_id
        if task_id is None:
            # Should be unreachable: route() rejects None task_id before
            # reaching speculative routing. Fall back to the real workspace
            # rather than raising mid-dispatch.
            return RealityRoute(workspace, ExecutionDisposition.DIRECT)
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            branch = self._active.get(task_id)
            if branch is None:
                branch = await self._shadow.open_branch(
                    task_id=task_id,
                    base_workspace=workspace,
                    proposal=[],
                )
                self._active[task_id] = branch
            target = branch.shadow_workspace
            _translate_workspace_arguments(request, workspace.root, target.root)
            return RealityRoute(
                target,
                ExecutionDisposition.SPECULATIVE,
                transaction_id=branch.id,
            )

    async def discard_ephemeral(self, call_id: str | None) -> None:
        """Drop a per-call isolated branch if one was opened for it."""
        branch = self._ephemeral.get(call_id) if call_id else None
        if branch is None:
            return
        try:
            await self._shadow.discard(branch, reason="isolated call complete")
        except Exception:  # noqa: BLE001 - best-effort cleanup
            return
        if call_id is not None and self._ephemeral.get(call_id) is branch:
            self._ephemeral.pop(call_id, None)

    async def deactivate_branch(self, task_id: str | None) -> None:
        """Clear the task's active branch from the gate's routing map.

        Called after the shadow engine has committed or discarded the branch
        so subsequent operations don't route to a stale workspace.
        """
        if task_id is not None:
            self._active.pop(task_id, None)

    async def compensate(self, task_id: str | None) -> bool:
        """Roll a transactional task back to its pre-mutation checkpoint.

        Returns True if a checkpoint existed and was restored, False if there
        was nothing to roll back.  Only the exact captured revision is
        restored; concurrent changes are refused by the checkpoint manager.
        """
        if task_id is None:
            return False
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            ckpt_id = self._checkpoint_by_task.get(task_id)
            if ckpt_id is None or self._checkpoints is None:
                return False
            root = self._base_root(task_id)
            record = self._transaction_records.setdefault(task_id, {})
            owned = record.get("last_owned_fingerprint")
            expected = await self._checkpoints.fingerprint(root)
            if owned is None or expected != owned:
                # Compensation must compare against the exact post-state
                # produced by this transaction.  Accepting a freshly observed
                # or partially planned state could restore over an unrelated
                # edit made while the transaction was active.
                record["state"] = "RECOVERY_REQUIRED"
                record["updated_at"] = utcnow().isoformat()
                self._persist_transaction_state()
                raise TransactionRecoveryRequired(
                    "transaction workspace differs from its last owned revision"
                )
            # The checkpoint manager performs the expected-revision check in
            # the same worker operation as restore. A concurrent edit is
            # therefore refused instead of silently overwritten.
            try:
                await self._checkpoints.restore(
                    ckpt_id,
                    root,
                    expected_fingerprint=expected,
                )
            except Exception:
                record["state"] = "RECOVERY_REQUIRED"
                record["updated_at"] = utcnow().isoformat()
                self._persist_transaction_state()
                raise
            await self._checkpoints.release(ckpt_id, owner=task_id)
            self._checkpoint_by_task.pop(task_id, None)
            self._checkpoint_root_by_task.pop(task_id, None)
            self._transaction_records.pop(task_id, None)
            self._persist_transaction_state()
            return True

    async def note_transaction_progress(
        self,
        task_id: str | None,
        workspace_root: str,
        *,
        mutation: bool,
        mutation_id: str | None = None,
        resource: str | None = None,
    ) -> None:
        """Record the complete post-state owned by a transactional mutation."""
        if not mutation or task_id is None or self._checkpoints is None:
            return
        if self._checkpoint_by_task.get(task_id) is None:
            return
        fingerprint = await self._checkpoints.fingerprint(workspace_root)
        record = self._transaction_records.setdefault(task_id, {})
        record["last_owned_fingerprint"] = fingerprint
        record["state"] = "ACTIVE"
        record["postconditions"] = {}
        if mutation_id:
            mutation_ids = record.setdefault("mutation_ids", [])
            if mutation_id not in mutation_ids:
                mutation_ids.append(mutation_id)
        if resource:
            resources = record.setdefault("resources", [])
            if resource not in resources:
                resources.append(resource)
        record["updated_at"] = utcnow().isoformat()
        self._persist_transaction_state()

    def mark_transaction_proven(self, task_id: str | None) -> None:
        """Durably freeze a proven transaction until task finalization."""
        if task_id is None or task_id not in self._checkpoint_by_task:
            return
        record = self._transaction_records.setdefault(task_id, {})
        record["state"] = "COMMIT_PROVEN"
        record["updated_at"] = utcnow().isoformat()
        self._persist_transaction_state()

    async def reconcile_startup(self) -> int:
        """Reconcile durable transaction ownership before new work routes.

        An ACTIVE transaction is safe to resume only when the workspace still
        equals the exact revision last owned by the transaction.  A crash
        between the filesystem effect and ``note_transaction_progress`` (or
        an external edit) therefore becomes an explicit recovery boundary
        instead of an implicit authorization to keep mutating.
        """
        if self._checkpoints is None:
            return 0
        recovered = 0
        for task_id, record in self._transaction_records.items():
            if record.get("state") not in {"ACTIVE", "COMMIT_PROVEN"}:
                continue
            root = str(record.get("workspace_root") or "")
            owned = record.get("last_owned_fingerprint")
            if not root or not owned:
                continue
            try:
                current = await self._checkpoints.fingerprint(root)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                record["state"] = "RECOVERY_REQUIRED"
                record["error"] = f"transaction workspace unavailable: {exc}"
            else:
                if current == owned:
                    continue
                record["state"] = "RECOVERY_REQUIRED"
                record["error"] = (
                    "transaction workspace differs from its last owned revision after restart"
                )
            record["updated_at"] = utcnow().isoformat()
            recovered += 1
        if recovered:
            self._persist_transaction_state()
        return recovered

    async def finalize_transaction(self, task_id: str | None) -> None:
        """Release a successful in-place transaction's recovery binding."""
        if task_id is None:
            return
        checkpoint_id = self._checkpoint_by_task.get(task_id)
        if checkpoint_id is not None and self._checkpoints is not None:
            mark_terminal = getattr(self._checkpoints, "mark_terminal", None)
            if mark_terminal is not None:
                mark_terminal(checkpoint_id, state="FINALIZED")
            await self._checkpoints.release(checkpoint_id, owner=task_id)
        self._checkpoint_by_task.pop(task_id, None)
        self._checkpoint_root_by_task.pop(task_id, None)
        self._transaction_records.pop(task_id, None)
        self._persist_transaction_state()

    def _base_root(self, task_id: str) -> str:
        # The transactional tier mutated the real workspace in place; use the
        # root recorded when its checkpoint was captured.  The speculative
        # branch's base is the only other known root.
        stored = self._checkpoint_root_by_task.get(task_id)
        if stored is not None:
            return stored
        branch = self._active.get(task_id)
        if branch is not None:
            return branch.base_workspace.root
        return os.getcwd()

    @staticmethod
    def _is_project_sensitive(
        request: CapabilityRequest,
        effects,
        descriptor: CapabilityDescriptor,
    ) -> bool:
        """Classify mutation risk without relying on operation names alone."""
        capability_id = str(request.capability_id)
        operation = str(
            (request.arguments or {}).get("operation")
            or (request.arguments or {}).get("action")
            or ""
        ).casefold()
        effect_set = set(effects or ())

        # Arbitrary process/code execution is opaque: it can rewrite a project
        # regardless of whether the source happens to contain a write command.
        if capability_id in RealityGate._PROCESS_CAPABILITIES:
            return operation not in RealityGate._READ_ONLY_OPERATIONS

        # Generated/project affordances execute code supplied by the task and
        # inherit the same boundary even when their declared envelope only
        # contains EXECUTE/READ_LOCAL.
        origin = getattr(descriptor.origin, "value", descriptor.origin)
        if origin in {"generated", "project", "user"} and (
            EffectClass.EXECUTE in effect_set
            or EffectClass.WRITE_LOCAL in effect_set
            or EffectClass.DELETE in effect_set
        ):
            return True

        if capability_id in RealityGate._PROJECT_CAPABILITIES:
            if operation in RealityGate._READ_ONLY_OPERATIONS:
                return False
            return bool(
                effect_set
                & {
                    EffectClass.WRITE_LOCAL,
                    EffectClass.DELETE,
                    EffectClass.EXECUTE,
                    EffectClass.SPAWN_PROCESS,
                }
            )

        # A native capability with an explicit local mutation contract is
        # project-sensitive unless it is clearly a read-only operation. This
        # keeps new mutation-capable capabilities safe by default.
        return (
            bool(effect_set & {EffectClass.WRITE_LOCAL, EffectClass.DELETE})
            and operation not in RealityGate._READ_ONLY_OPERATIONS
        )

    @staticmethod
    def _is_workspace_bound(
        request: CapabilityRequest,
        descriptor: CapabilityDescriptor,
    ) -> bool:
        capability_id = str(request.capability_id)
        if capability_id in RealityGate._PROJECT_CAPABILITIES | RealityGate._PROCESS_CAPABILITIES:
            return True
        origin = getattr(descriptor.origin, "value", descriptor.origin)
        return origin in {"generated", "project", "user"}


def _mutation_mode(value: MutationMode | str | None) -> MutationMode:
    if isinstance(value, MutationMode):
        return value
    try:
        return MutationMode(str(value or MutationMode.DIRECT.value))
    except ValueError:
        return MutationMode.DIRECT


def _planned_postconditions(
    request: CapabilityRequest,
    workspace_root: str,
) -> dict[str, str]:
    """Describe a directly requested fs post-state before dispatch executes."""
    if request.capability_id != "fs":
        return {}
    args = request.arguments or {}
    operation = str(args.get("operation") or "").casefold()
    raw_path = args.get("path")
    if not isinstance(raw_path, str) or operation not in {"write", "patch", "delete"}:
        return {}
    path = raw_path if os.path.isabs(raw_path) else os.path.join(workspace_root, raw_path)
    path = os.path.realpath(os.path.abspath(path))
    if operation == "delete":
        return {path: "<missing>"}
    content = args.get("content") if operation == "write" else args.get("new_content")
    if content is None:
        encoded = (
            args.get("content_base64") if operation == "write" else args.get("new_content_base64")
        )
        if isinstance(encoded, str):
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                return {}
        else:
            return {}
    elif isinstance(content, str):
        payload = content.encode("utf-8")
    else:
        return {}
    return {path: hashlib.sha256(payload).hexdigest()}


def _transaction_resources(
    request: CapabilityRequest,
    workspace_root: str,
) -> list[str]:
    """Record the workspace resources named by a transaction request."""
    args = request.arguments or {}
    resources: list[str] = []
    for key in ("path", "destination", "cwd", "workdir"):
        value = args.get(key)
        if not isinstance(value, str):
            continue
        path = value if os.path.isabs(value) else os.path.join(workspace_root, value)
        resources.append(os.path.realpath(os.path.abspath(path)))
    return list(dict.fromkeys(resources))


def _translate_workspace_arguments(
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


def _same_root(first: str, second: str) -> bool:
    return os.path.realpath(os.path.abspath(first)) == os.path.realpath(os.path.abspath(second))


__all__ = ["ExecutionDisposition", "RealityGate", "RealityRoute"]
