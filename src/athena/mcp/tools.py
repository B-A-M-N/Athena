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
import json
from typing import Any, Mapping

from athena.protocol.capabilities import EffectClass

# Canonical id prefix per §91.
MCP_ID_PREFIX = "mcp:"

_SERVER_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.+-]")

_MUTATING_VERBS = frozenset(
    {
        "create",
        "insert",
        "update",
        "set",
        "put",
        "write",
        "add",
        "delete",
        "remove",
        "drop",
        "patch",
        "modify",
        "save",
        "append",
        "post",
        "push",
        "send",
        "publish",
        "upsert",
        "edit",
        "destroy",
        "revoke",
        "grant",
        "start",
        "stop",
        "kill",
        "restart",
        "upload",
        "archive",
        "move",
        "copy",
        "merge",
        "replace",
    }
)
_NETWORK_TOKENS = (
    "http",
    "https",
    "url",
    "web",
    "api",
    "network",
    "remote",
    "github",
    "slack",
    "gmail",
    "twitter",
    "fetch",
    "request",
    "socket",
)

_MAX_MCP_SCHEMA_BYTES = 256 * 1024
# Keep the MCP boundary aligned with the canonical registry validator. Local
# recursive $refs are valid; only the concrete schema tree is depth-bounded.
_MAX_MCP_SCHEMA_DEPTH = 32


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
        elif _name_has_mutating_verb(name):
            # Server hints can be forged. A mutating tool name is enough to
            # retain the conservative network-write floor; readOnlyHint may
            # never downgrade it.
            effects.add(EffectClass.NETWORK_WRITE)
        elif read_only:
            effects.add(EffectClass.NETWORK_READ)
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
    """Preserve a bounded local MCP JSON Schema for Athena validation.

    Athena's registry validates Draft 2020-12 schemas directly, so flattening
    nested objects and combinators would only reduce interoperability. The
    boundary still rejects external references and pathological size/depth;
    remote content must never turn schema validation into network access or a
    recursion/DoS primitive.
    """
    if not isinstance(input_schema, Mapping):
        return _default_schema()
    normalized = _normalize_schema(input_schema)
    if normalized.get("type") is None and not any(
        key in normalized for key in ("oneOf", "anyOf", "allOf", "$ref")
    ):
        normalized["type"] = "object"
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized["properties"] = {}
    if "additionalProperties" not in normalized and "allow_extra" not in normalized:
        # MCP's JSON Schema follows the JSON Schema default: extra properties
        # are valid unless the server explicitly closes the object.
        normalized["additionalProperties"] = True
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_MCP_SCHEMA_BYTES:
        raise ValueError("MCP tool schema exceeds the maximum allowed size")
    return normalized


def effect_note(annotations: Mapping[str, Any] | None) -> str:
    """Human note summarizing advisory effect metadata (never authorization)."""
    if not annotations:
        return ""
    hints = sorted(str(k) for k in annotations if annotations.get(k))
    return f"server-asserted hints: {', '.join(hints)}" if hints else ""


def _normalize_schema(node: Any, depth: int = 0) -> Any:
    if depth > _MAX_MCP_SCHEMA_DEPTH:
        raise ValueError(f"MCP tool schema exceeds maximum nesting depth {_MAX_MCP_SCHEMA_DEPTH}")
    if isinstance(node, Mapping):
        out: dict[str, Any] = {}
        for raw_key, value in node.items():
            key = str(raw_key)
            if key == "$ref":
                ref = str(value)
                if not ref.startswith("#/"):
                    raise ValueError("MCP tool schema may only use local $ref values")
                out[key] = ref
            elif key in {
                "properties",
                "$defs",
                "definitions",
                "patternProperties",
                "dependentSchemas",
            }:
                if not isinstance(value, Mapping):
                    raise ValueError(f"MCP schema field {key!r} must be an object")
                out[key] = {
                    str(child_key): _normalize_schema(child_value, depth + 1)
                    for child_key, child_value in value.items()
                }
            elif key in {"oneOf", "anyOf", "allOf", "prefixItems", "items", "contains"}:
                out[key] = _normalize_schema(value, depth + 1)
            else:
                out[key] = _normalize_schema(value, depth + 1)
        return out
    if isinstance(node, list):
        return [_normalize_schema(item, depth + 1) for item in node]
    if isinstance(node, tuple):
        return [_normalize_schema(item, depth + 1) for item in node]
    return node


def _default_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": True,
    }


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


def _name_has_mutating_verb(name: str) -> bool:
    """Recognize mutating words in snake/kebab/dotted MCP tool names."""
    return bool(set(re.findall(r"[a-z0-9]+", name)) & _MUTATING_VERBS)


__all__ = [
    "canonical_capability_id",
    "friendly_alias",
    "sanitize_server_name",
    "infer_effects",
    "tool_schema_to_descriptor_input",
    "effect_note",
]
