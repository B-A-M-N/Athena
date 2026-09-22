"""Shared pure helpers for capability dispatch.

These functions are owned by neither the dispatcher nor its mechanisms:
they are pure classification/normalization utilities.  Moving them to a
neutral sibling removes the dynamic ``_mod()`` owner-backimport cycle
(review item 15).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from typing import Any

from athena.concurrency import ReferenceCountedKeyedLocks
from athena.protocol.capabilities import (
    CachePolicy,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.policy import PolicyVerdict
from athena.protocol.tasks import WorkspaceSpec

__all__ = ["ReferenceCountedKeyedLocks"]


def bounded_diagnostics(values: list[Any] | tuple[Any, ...]) -> list[Any]:
    """Keep diagnostic events useful without turning them into an output log."""
    bounded: list[Any] = []
    for value in values[:64]:
        if isinstance(value, dict):
            item: dict[str, Any] = {}
            for key, raw in list(value.items())[:24]:
                if isinstance(raw, (str, int, float, bool)) or raw is None:
                    clean = str(raw)[:4096] if isinstance(raw, str) else raw
                    item[str(key)] = clean
            bounded.append(item)
        else:
            bounded.append(str(value)[:4096])
    return bounded


def is_execution(effects: tuple[EffectClass, ...]) -> bool:
    return bool(set(effects) & {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS})


def is_ordering_sensitive(effects: tuple[EffectClass, ...]) -> bool:
    """Whether a resource-less call must serialize against sibling mutations."""
    return bool(
        set(effects)
        & {
            EffectClass.WRITE_LOCAL,
            EffectClass.DELETE,
            EffectClass.NETWORK_WRITE,
            EffectClass.EXTERNAL_PUBLISH,
            EffectClass.EXTERNAL_MESSAGE,
            EffectClass.FINANCIAL,
            EffectClass.PRIVILEGED,
            EffectClass.COMPUTER_INPUT,
            EffectClass.EXECUTE,
            EffectClass.SPAWN_PROCESS,
        }
    )


def resource_key(workspace: WorkspaceSpec, value: str) -> str:
    """Normalize a capability path for conflict locking."""
    raw = os.path.expanduser(value)
    root = os.path.realpath(os.path.abspath(workspace.root))
    target = os.path.realpath(
        os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    )
    try:
        inside = os.path.commonpath((root, target)) == root
    except ValueError:
        inside = False
    return target if inside else f"external:{target}"


def primary_effect(available) -> EffectClass | None:
    for candidate in (
        EffectClass.WRITE_LOCAL,
        EffectClass.DELETE,
        EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS,
        EffectClass.READ_LOCAL,
        EffectClass.PRIVILEGED,
        EffectClass.SECRET_READ,
        EffectClass.FINANCIAL,
    ):
        if candidate in available:
            return candidate
    for eff in available:
        return eff
    return None


EXEC_CAPABILITIES = frozenset(
    {"execute", "terminal_session", "process", "debugger", "shell", "bash"}
)

HIGH_RISK_EFFECTS = frozenset(
    {
        EffectClass.NETWORK_WRITE,
        EffectClass.EXTERNAL_MESSAGE,
        EffectClass.EXTERNAL_PUBLISH,
        EffectClass.COMPUTER_INPUT,
        EffectClass.SECRET_READ,
        EffectClass.FINANCIAL,
        EffectClass.PRIVILEGED,
    }
)


_SECRET_VALUE = re.compile(
    r"(?i)"
    r"(Bearer\s+\S+)"  # Authorization header value
    r"|(\b(?:sk|pk|rk|ghp|gho|ghu|github_pat)(?:[_\-\s]?)[A-Za-z0-9_\-]{8,}\b)"
    r"|(\bAKIA[0-9A-Z]{16}\b)"
    r"|(\b[a-zA-Z0-9]{40,}\b)"  # long opaque token
)

_SECRET_KEY = re.compile(
    r"(?i)(?:authorization|access[_-]?token|refresh[_-]?token|password|"
    r"secret|api[_-]?key|credential|private[_-]?key|cookie|passphrase)"
)


def redact(value: str) -> str:
    """Redact secret-shaped text from diagnostics."""
    return _SECRET_VALUE.sub("[REDACTED]", value)


def redact_value(value: Any, *, key: str = "") -> Any:
    """Redact values by secret-like key names or string shape."""
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return value


def redact_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Scrub argument snapshots while preserving event metadata readability."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"arguments", "original_arguments", "canonical_arguments"}:
            out[key] = redact_value(value, key=key)
        else:
            out[key] = value
    return out


def _cacheable_effects(effects: tuple[EffectClass, ...]) -> bool:
    return bool(effects) and set(effects).issubset({EffectClass.READ_LOCAL})


def _health_state_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Detect circuit transitions, excluding routine closed-counter updates."""
    return str(before.get("status") or "closed") != str(after.get("status") or "closed")


def _wrap_exception(exc, request):
    call_id = getattr(request, "call_id", None) or ""
    return CapabilityResult(
        call_id,
        getattr(request, "capability_id", ""),
        CapabilityResultStatus.FAILED,
        error=f"dispatch failed: {exc}",
    )


async def _result_cache_key(
    request: CapabilityRequest,
    workspace: WorkspaceSpec,
    effects: tuple[EffectClass, ...],
    *,
    profile: str | None,
    descriptor=None,
) -> tuple[str | None, str, str, str, str | None] | None:
    cache_policy = (
        descriptor.resolve_cache_policy(request.arguments or {})
        if descriptor is not None
        else CachePolicy.NONE
    )
    if descriptor is None or cache_policy is CachePolicy.NONE or not _cacheable_effects(effects):
        return None
    try:
        arguments = json.dumps(
            {
                "workspace_id": workspace.id,
                "session_id": request.session_id,
                "descriptor_version": descriptor.version,
                "arguments": dict(request.arguments or {}),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return None
    if cache_policy is CachePolicy.CONTENT_ADDRESS:
        resolver = descriptor.cache_key_resolver
        if resolver is None:
            return None
        try:
            await asyncio.sleep(0)
            content_key = resolver(request.arguments or {}, workspace)
        except (OSError, TypeError, ValueError):
            return None
        if not content_key:
            return None
        arguments += ":content:" + content_key
    elif cache_policy is CachePolicy.WORKSPACE_REVISION:
        # A caller that owns a persisted workspace revision can put it on the
        # scoped workspace object. Do not synchronously hash a whole repo on
        # the event loop as a fallback; without a revision this policy is off.
        revision = getattr(workspace, "revision", None)
        if not revision:
            return None
        arguments += ":revision:" + str(revision)
    return (
        request.task_id,
        os.path.realpath(os.path.abspath(workspace.root)),
        request.capability_id,
        arguments,
        getattr(profile, "value", profile),
    )


def _cache_ttl(descriptor, cache_policy: CachePolicy | None = None) -> float:
    cache_policy = cache_policy or descriptor.cache_policy
    value = descriptor.cache_ttl_seconds
    if value is None and cache_policy is CachePolicy.TTL:
        return 2.0  # CapabilityDispatcher._RESULT_CACHE_TTL
    if value is None:
        # Revision/content-addressed entries are valid until their key changes
        # or a mutation invalidates the workspace.  Bound the in-memory
        # lifetime so a long-lived service never accumulates stale entries.
        return 3600.0
    return max(0.01, min(float(value), 3600.0))


_STRICTNESS = {
    PolicyVerdict.ALLOW.value: 0,
    PolicyVerdict.ASK.value: 1,
    PolicyVerdict.DENY.value: 2,
}
