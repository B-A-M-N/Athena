"""Pure persistence/codec and conflict helpers for the Shadow domain.

These functions own exact branch/workspace serialization semantics, conflict
classification, and commit-plan digests. They hold no state and no policy:
``ShadowEngine`` remains the single durable branch authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from athena.execution.environment import ProjectEnvironmentFingerprint
from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.tasks import MutationMode, NetworkPolicy, PathRule, WorkspaceSpec

if TYPE_CHECKING:
    from athena.shadow.engine import ShadowBranch

__all__ = [
    "branch_record",
    "commit_plan_digest",
    "commit_plan_record",
    "environment_fingerprint",
    "environment_record",
    "full_preimage_hash",
    "manifest_conflicts",
    "manifest_fingerprint",
    "rebase_rules",
    "resource_kind",
    "unsupported_commit_resources",
    "workspace_from_record",
    "workspace_record",
]


def manifest_conflicts(
    expected: dict[str, str],
    current: dict[str, str],
) -> list[dict[str, str]]:
    """Describe every resource that differs between two complete manifests."""
    conflicts: list[dict[str, str]] = []
    for rel in sorted(set(expected) | set(current)):
        before = expected.get(rel, "<missing>")
        after = current.get(rel, "<missing>")
        if before == after:
            continue
        if before == "<missing>":
            reason = "created_elsewhere"
        elif after == "<missing>":
            reason = "deleted_elsewhere"
        else:
            reason = "modified_elsewhere"
        conflicts.append({"resource": rel, "reason": reason})
    return conflicts


def unsupported_commit_resources(
    branch: ShadowBranch,
    changes: Mapping[str, list[str]],
    *,
    shadow_root: str,
) -> list[dict[str, str]]:
    """Identify changed entries the canonical ``fs`` commit cannot represent."""
    resources: list[dict[str, str]] = []
    candidates = list(changes.get("modified", ())) + list(changes.get("added", ()))
    for relative in candidates:
        path = os.path.join(shadow_root, relative)
        kind = resource_kind(path)
        if kind != "regular_file":
            resources.append({"resource": relative, "kind": kind})
    for relative in changes.get("deleted", ()):
        path = os.path.join(branch.base_workspace.root, relative)
        kind = resource_kind(path)
        if kind != "regular_file":
            resources.append({"resource": relative, "kind": kind})
    return resources


def resource_kind(path: str) -> str:
    if os.path.islink(path):
        return "symlink"
    if os.path.isfile(path):
        return "regular_file"
    if os.path.isdir(path):
        return "directory"
    return "special"


def full_preimage_hash(path: str, root: str) -> str:
    """Return the filesystem capability's full-content preimage hash."""
    real_root = os.path.realpath(os.path.abspath(root))
    real_path = os.path.realpath(os.path.abspath(path))
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        raise ValueError(f"commit resource escapes workspace: {path}")
    if not os.path.exists(path):
        return "<missing>"
    if os.path.islink(path) or os.path.isdir(path):
        raise ValueError(f"commit resource is not a regular file: {path}")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_record(workspace: WorkspaceSpec) -> dict:
    return {
        "id": workspace.id,
        "root": workspace.root,
        "readable": [{"path": rule.path, "allow": rule.allow} for rule in workspace.readable],
        "writable": [{"path": rule.path, "allow": rule.allow} for rule in workspace.writable],
        "temp_root": workspace.temp_root,
        "execution_backend": workspace.execution_backend,
        "network_policy": workspace.network_policy.value,
        "mutation_mode": workspace.mutation_mode.value,
        "revision": workspace.revision,
    }


def workspace_from_record(record: dict) -> WorkspaceSpec:
    return WorkspaceSpec(
        id=str(record["id"]),
        root=str(record["root"]),
        readable=tuple(PathRule(**dict(rule)) for rule in record.get("readable") or ()),
        writable=tuple(PathRule(**dict(rule)) for rule in record.get("writable") or ()),
        temp_root=record.get("temp_root"),
        execution_backend=str(record.get("execution_backend") or "local"),
        network_policy=NetworkPolicy(str(record.get("network_policy") or "allow")),
        mutation_mode=MutationMode(str(record.get("mutation_mode") or MutationMode.DIRECT.value)),
        revision=record.get("revision"),
    )


def rebase_rules(
    rules: tuple[PathRule, ...],
    base_root: str,
    shadow_root: str,
) -> tuple[PathRule, ...]:
    """Move workspace-local path rules from the base tree to its clone."""
    base = os.path.realpath(os.path.abspath(base_root))
    shadow = os.path.realpath(os.path.abspath(shadow_root))
    rebased: list[PathRule] = []
    for rule in rules:
        raw = str(rule.path)
        if os.path.isabs(raw):
            normalized = os.path.realpath(os.path.abspath(raw))
            if normalized == base or normalized.startswith(base + os.sep):
                raw = shadow + normalized[len(base) :]
        else:
            raw = os.path.join(shadow, raw)
        rebased.append(PathRule(path=raw, allow=rule.allow))
    return tuple(rebased)


def manifest_fingerprint(manifest: dict[str, str]) -> str:
    encoded = json.dumps(
        sorted(manifest.items()),
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def environment_record(
    workspace: WorkspaceSpec,
    policy_profile: str | None = None,
) -> dict[str, Any]:
    return ProjectEnvironmentFingerprint().describe(
        workspace,
        extras={"policy_profile": policy_profile},
    )


def environment_fingerprint(
    workspace: WorkspaceSpec,
    policy_profile: str | None = None,
) -> str:
    return ProjectEnvironmentFingerprint().fingerprint(
        workspace,
        extras={"policy_profile": policy_profile},
    )


def branch_record(branch: ShadowBranch) -> dict:
    return {
        "id": branch.id,
        "task_id": branch.task_id,
        "base_workspace": workspace_record(branch.base_workspace),
        "shadow_workspace": workspace_record(branch.shadow_workspace),
        "proposal": branch.proposal,
        "status": branch.status,
        "verification": branch.verification,
        "verification_started_at": branch.verification_started_at,
        "unsupported_resources": branch.unsupported_resources,
        # ``dict`` also keeps restart compatibility with legacy branches that
        # were constructed before certificates became immutable mappings.
        "verification_certificate": dict(branch.verification_certificate),
        "mutations": branch.mutations,
        "base_manifest": branch.base_manifest,
        "base_preimages": branch.base_preimages,
        "error": branch.error,
        "commit_plan": branch.commit_plan,
        "commit_outcome": branch.commit_outcome,
        "commit_state": branch.commit_state,
        "commit_started_at": branch.commit_started_at,
        "commit_completed_at": branch.commit_completed_at,
        "checkpoint_id": branch.checkpoint_id,
        "policy_profile": branch.policy_profile,
        "created_at": branch.created_at,
    }


def commit_plan_record(
    request: CapabilityRequest,
    *,
    expected_preimage: str,
    mode: int | None,
) -> dict:
    """Serialize one exact, restart-checkable canonical commit operation."""
    arguments = request.arguments
    content_base64 = arguments.get("content_base64")
    content_sha256 = None
    if content_base64 is not None:
        try:
            content_sha256 = hashlib.sha256(
                base64.b64decode(str(content_base64), validate=True)
            ).hexdigest()
        except (ValueError, TypeError):
            content_sha256 = None
    return {
        "call_id": request.call_id,
        "capability_id": request.capability_id,
        "operation": arguments.get("operation"),
        "path": arguments.get("path"),
        "content_sha256": content_sha256,
        "expected_preimage": expected_preimage,
        "mode": mode,
    }


def commit_plan_digest(plan: list[dict]) -> str:
    """Digest plan semantics while ignoring regenerated transport call IDs."""
    stable = [{key: value for key, value in record.items() if key != "call_id"} for record in plan]
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
