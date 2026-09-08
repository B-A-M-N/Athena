"""Compiled policy snapshots — a constant-cost accelerator, never authority.

A ``PolicySnapshot`` precomputes everything about one
``(autonomy level, workspace, task capability policy)`` triple that
``PolicyEngine.evaluate`` would otherwise re-derive on every call: the
profile RuleSet (already priority-ordered), the workspace root's canonical
path, and the canonical workspace path rules. The snapshot carries NO
verdicts of its own — every decision is still computed by the engine's
monotonic composition over resolved effects at call time.

Invalidation is by revision guard, per the accelerator contract:

* ``policy_revision`` — bump when the rule source changes (profile builders
  in-process never change; an external rulesource must bump this).
* ``task_authority_revision`` — bump when the task's CapabilityPolicy or
  autonomy level changes.
* ``workspace_revision`` — the persisted workspace index revision; taken
  from ``WorkspaceSpec.revision`` when present.

The engine validates a snapshot's guards against the live values on every
use. A stale snapshot is discarded and rebuilt — never consulted — so the
compiled object can never authorize what the live authority would refuse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from athena.policy.profiles import profile_ruleset
from athena.policy.rules import RuleSet
from athena.protocol.tasks import AutonomyLevel, CapabilityPolicy, WorkspaceSpec

_SEP = os.sep


@dataclass(frozen=True)
class SnapshotKey:
    """Identity of a compiled snapshot's authority inputs.

    ``task_ceiling`` is the task's CapabilityPolicy used ONLY as a cache-key
    and revision guard (its hash): the compiled snapshot contains no
    task-derived rules — profile rules and canonical workspace paths only.
    """

    level: AutonomyLevel
    workspace_id: str
    task_ceiling: CapabilityPolicy | None

    def __post_init__(self) -> None:
        # CapabilityPolicy is a frozen dataclass of tuples/frozensets, so it
        # is hashable already; normalize None to a stable absent marker.
        if self.task_ceiling is None:
            object.__setattr__(self, "task_ceiling", None)


@dataclass(frozen=True)
class PolicySnapshot:
    """Precompiled, read-only evaluation inputs for one authority triple."""

    key: SnapshotKey
    rules: RuleSet
    # Raw and canonical workspace identity; canonical forms are computed
    # once here, never per evaluation.
    workspace_root_raw: str
    workspace_root: str
    writable_rules: tuple[str, ...]
    readable_rules: tuple[str, ...]
    policy_revision: str
    task_authority_revision: str
    workspace_revision: str | None
    network_policy: object = None
    execution_backend: str | None = None

    def guards_match(
        self,
        *,
        level: AutonomyLevel,
        workspace: WorkspaceSpec | None,
        task_policy: CapabilityPolicy | None,
        policy_revision: str,
    ) -> bool:
        """Revalidate the snapshot against live authority values.

        Any mismatch means the compiled object is stale: the caller must
        rebuild. Guard comparison is raw-string equality only — no path
        canonicalization on the hot path (canonical forms live on the
        snapshot for the engine to consume). A rebuilt-but-equivalent
        WorkspaceSpec still hits the cache.
        """
        if self.key.level != level or self.policy_revision != policy_revision:
            return False
        if self.task_authority_revision != _task_authority_revision(task_policy):
            return False
        if workspace is None:
            return self.key.workspace_id == "" and self.workspace_revision is None
        return (
            self.key.workspace_id == workspace.id
            and self.workspace_revision == workspace.revision
            and self.workspace_root_raw == workspace.root
            and self.network_policy == workspace.network_policy
            and self.writable_rules == _rule_paths(workspace.writable)
            and self.readable_rules == _rule_paths(workspace.readable or workspace.writable)
        )


_SNAPSHOT_CACHE: dict[SnapshotKey, PolicySnapshot] = {}
_CACHE_MAX = 64


def get_snapshot(
    *,
    level: AutonomyLevel,
    workspace: WorkspaceSpec | None,
    task_policy: CapabilityPolicy | None,
    policy_revision: str = "1",
) -> PolicySnapshot:
    """Return the compiled snapshot for this authority triple, building it
    on first use and rebuilding whenever a revision guard says stale."""
    key = SnapshotKey(level, workspace.id if workspace else "", task_policy)
    snap = _SNAPSHOT_CACHE.get(key)
    if snap is not None and snap.guards_match(
        level=level,
        workspace=workspace,
        task_policy=task_policy,
        policy_revision=policy_revision,
    ):
        return snap
    snap = _build(level, workspace, task_policy, policy_revision)
    if len(_SNAPSHOT_CACHE) >= _CACHE_MAX:
        _SNAPSHOT_CACHE.clear()
    _SNAPSHOT_CACHE[key] = snap
    return snap


def clear_snapshot_cache() -> None:
    """Test/ops hook: drop all compiled snapshots."""
    _SNAPSHOT_CACHE.clear()


def _build(
    level: AutonomyLevel,
    workspace: WorkspaceSpec | None,
    task_policy: CapabilityPolicy | None,
    policy_revision: str,
) -> PolicySnapshot:
    writable: tuple[str, ...] = ()
    readable: tuple[str, ...] = ()
    root = ""
    network_policy = None
    backend = None
    ws_revision = None
    if workspace is not None:
        root = _canonical(workspace.root)
        writable = tuple(_canonical_rule(r.path) for r in (workspace.writable or ()))
        readable = tuple(
            _canonical_rule(r.path) for r in (workspace.readable or workspace.writable or ())
        )
        network_policy = workspace.network_policy
        backend = workspace.execution_backend
        ws_revision = workspace.revision
    return PolicySnapshot(
        key=SnapshotKey(level, workspace.id if workspace else "", task_policy),
        rules=profile_ruleset(level),
        workspace_root_raw=workspace.root if workspace else "",
        workspace_root=root,
        writable_rules=writable,
        readable_rules=readable,
        policy_revision=policy_revision,
        task_authority_revision=_task_authority_revision(task_policy),
        workspace_revision=ws_revision,
        network_policy=network_policy,
        execution_backend=backend,
    )


def _task_authority_revision(task_policy: CapabilityPolicy | None) -> str:
    """A stable identity for the task's capability ceiling.

    CapabilityPolicy is a frozen dataclass of hashable fields, so its hash
    is the revision. ``None`` (no ceiling) is its own revision.
    """
    if task_policy is None:
        return "none"
    return str(hash(task_policy))


def _canonical(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def _rule_paths(rules) -> tuple[str, ...]:
    return tuple(_canonical_rule(r.path) for r in (rules or ()))


def _canonical_rule(pattern: str) -> str:
    """Canonicalize a rule path at build time, preserving glob metacharacters.

    Globs canonicalize on their literal prefix (``/tmp/ws/*.log`` resolves
    to ``/tmp/ws`` + ``/*.log``) so fnmatch sees an absolute pattern without
    re-realpathing on the hot path. The separator before the first glob
    metacharacter is preserved: ``/tmp/ws/**`` -> ``/tmp/ws`` + ``/**``, not
    ``/tmp/ws**``.
    """
    text = os.path.expanduser(str(pattern))
    if "*" in text or "?" in text or "[" in text:
        # Take the literal dirname preceding the first glob metacharacter,
        # canonicalize it, then re-append the glob (which begins at the
        # separator it originally followed). "/tmp/ws/**" -> "/tmp/ws" + "/**".
        for i, ch in enumerate(text):
            if ch in "*?[":
                # The glob starts at the separator that precedes the first
                # metacharacter: "/tmp/ws/**" -> the literal prefix
                # "/tmp/ws/" canonicalized as the directory "/tmp/ws", then
                # + "/**".
                cut_at = i - 1 if (i > 0 and text[i - 1] == "/") else i
                canonical_dir = os.path.realpath(os.path.abspath(text[:cut_at] or "/")).rstrip(
                    "/\\"
                )
                return canonical_dir + text[cut_at:]
        return text
    return os.path.realpath(os.path.abspath(text))


__all__ = [
    "PolicySnapshot",
    "SnapshotKey",
    "get_snapshot",
    "clear_snapshot_cache",
]
