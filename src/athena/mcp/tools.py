"""MCP tool translation helpers (§91 MCP Namespacing, §92 MCP Security).

This module maps MCP tool schemas to Athena :class:`CapabilityDescriptor`
input schemas, infers :class:`EffectClass` sets from tool annotations and
names, and provides namespacing/sanitization helpers.

Namespacing invariants (§91, BHV-110):

* The canonical, collision-free identity is ``mcp:<connection-id>:<tool>``.
* A friendly alias ``<server>.<tool>`` may be shown to models but MUST NOT be
  used as the registry id on its own (two servers could share a display name).

Trust invariants (§92, BHV-111): server annotations are advisory metadata, never
authorization. MCP tools are REMOTE (network) operations, so :func:`infer_effects`
never classifies an MCP call as ``READ_LOCAL``; server ``readOnlyHint`` etc. may
add risk metadata but MUST NEVER lower Athena's inferred minimum. The adapter
marks every MCP-served capability as untrusted so policy stays authoritative.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from athena.protocol.capabilities import EffectClass

# Canonical id prefix per §91.
MCP_ID_PREFIX = "mcp:"

_SERVER_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.+-]")

_MUTATING_VERBS = frozenset(
    {
        "create", "insert", "update", "set", "put", "write", "add", "delete",
        "remove", "drop", "patch", "modify", "save", "append", "post", "push",
        "send", "publish", "upsert", "edit", "destroy", "revoke", "grant",
        "start", "stop", "kill", "restart", "upload", "archive", "move", "copy",
        "merge", "replace",
    }
)
_NETWORK_TOKENS = (
    "http", "https", "url", "web", "api", "network", "remote", "github",
    "slack", "gmail", "twitter", "fetch", "request", "socket",
)

_KNOWN_TYPES = frozenset(
    {"string", "boolean", "number", "integer", "object", "array", "null"}
)


def sanitize_server_name(name: str) -> str:
    """Return a stable, safe server slug used in friendly aliases."""
    slug = _SERVER_SANITIZE_RE.sub("_", (name or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "server"


def canonical_capability_id(connection_id: str, tool_name: str) -> str:
    """Build the collision-free canonical id ``mcp:<connection>:<tool>``.

    ``connection_id`` is the MCPClient's unique connection identity, which is
    what makes the id collision-free even when two servers expose the same
    tool name (BHV-110).
    """
    conn = str(connection_id or "").strip()
    tool = str(tool_name or "").strip()
    if not conn or not tool:
        raise ValueError("connection_id and tool_name are required")
    safe_conn = sanitize_server_name(conn) or conn
    return f"{MCP_ID_PREFIX}{safe_conn}:{tool}"


def friendly_alias(server_name: str, tool_name: str) -> str:
    """Model-visible alias ``<server>.<tool>`` (display only, not unique)."""
    return f"{sanitize_server_name(server_name)}.{tool_name}"


def infer_effects(
    tool_name: str,
    annotations: Mapping[str, Any] | None = None,
    input_schema: Mapping[str, Any] | None = None,
    *,
    remote: bool = False,
) -> frozenset[EffectClass]:
    """Infer a conservative effect set from annotations and verb.

    Server annotations are advisory (§92 / BHV-111); they refine the inferred
    class but NEVER bypass policy. When ``remote`` is true (an MCP tool call),
    the operation is a NETWORK action: the result always carries at least
    ``NETWORK_READ`` and is NEVER ``READ_LOCAL``. ``readOnlyHint`` and other
    advisory hints may only ADD risk metadata, never lower the inferred minimum;
    unknown/unclassifiable remote tools default conservatively to
    ``NETWORK_WRITE``. ``destructiveHint`` yields WRITE/DELETE effects that
    policy must explicitly allow.
    """
    annotations = dict(annotations or {})
    name = str(tool_name or "").lower()
    verb = _first_word(name) or ""

    destructive = bool(annotations.get("destructiveHint", False))
    read_only = bool(annotations.get("readOnlyHint", False))
    networky = _looks_networky(name, input_schema)

    effects: set[EffectClass] = set()

    if remote:
        if destructive:
            effects.add(EffectClass.DELETE)
            effects.add(EffectClass.NETWORK_WRITE)
        elif read_only:
            effects.add(EffectClass.NETWORK_READ)
        elif verb in _MUTATING_VERBS:
            effects.add(EffectClass.NETWORK_WRITE)
        else:
            effects.add(EffectClass.NETWORK_WRITE)
        return _frozenset_or_default(effects)

    if destructive:
        effects.add(EffectClass.DELETE)
    if read_only:
        effects.add(EffectClass.READ_LOCAL)
        return _frozenset_or_default(effects)

    if networky:
        if verb in _MUTATING_VERBS or destructive:
            effects.add(EffectClass.NETWORK_WRITE)
        else:
            effects.add(EffectClass.NETWORK_READ)
    elif verb in _MUTATING_VERBS:
        effects.add(EffectClass.WRITE_LOCAL)

    return _frozenset_or_default(effects)


def tool_schema_to_descriptor_input(
    input_schema: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Translate an MCP JsonSchema to the registry's JSON-schema subset.

    The registry validator (:mod:`athena.capabilities.registry.validate_schema`)
    understands ``type``, ``properties``, ``required``, ``enum`` and
    ``allow_extra``. This translation keeps those keys and the type-strings the
    validator needs, folding unknown MCP schema features away safely.
    """
    if not isinstance(input_schema, Mapping):
        return _default_schema()
    raw_props = input_schema.get("properties")
    properties: dict[str, Any] = {}
    if isinstance(raw_props, Mapping):
        for prop_name, spec in raw_props.items():
            properties[str(prop_name)] = _normalize_property(spec)
    required_raw = input_schema.get("required")
    required = [str(r) for r in required_raw] if isinstance(required_raw, list) else []
    extra = bool(input_schema.get("additionalProperties", False))
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "allow_extra": extra,
    }


def effect_note(annotations: Mapping[str, Any] | None) -> str:
    """Human note summarizing advisory effect metadata (never authorization)."""
    if not annotations:
        return ""
    hints = sorted(str(k) for k in annotations if annotations.get(k))
    return f"server-asserted hints: {', '.join(hints)}" if hints else ""


def _normalize_property(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        return {"type": "string"}
    out: dict[str, Any] = {}
    ptype = spec.get("type")
    if ptype in _KNOWN_TYPES:
        out["type"] = ptype
    among = spec.get("enum")
    if isinstance(among, list):
        out["enum"] = list(among)
    return out or {"type": "string"}


def _default_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "required": [], "allow_extra": True}


def _first_word(name: str) -> str:
    parts = re.split(r"[._:\-/\s]+", name)
    for p in parts:
        if p:
            return p.lower()
    return ""


def _looks_networky(name: str, input_schema: Mapping[str, Any] | None) -> bool:
    text = name.lower()
    if any(tok and tok in text for tok in _NETWORK_TOKENS):
        return True
    if isinstance(input_schema, Mapping):
        props = input_schema.get("properties")
        if isinstance(props, Mapping):
            joined = " ".join(str(k).lower() for k in props.keys())
            if any(tok and tok in joined for tok in ("url", "host", "endpoint", "token")):
                return True
    return False


def _frozenset_or_default(effects: set[EffectClass]) -> frozenset[EffectClass]:
    return frozenset(effects) if effects else frozenset({EffectClass.READ_LOCAL})


__all__ = [
    "canonical_capability_id",
    "friendly_alias",
    "sanitize_server_name",
    "infer_effects",
    "tool_schema_to_descriptor_input",
    "effect_note",
]