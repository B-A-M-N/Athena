"""Deterministic tool-input repair (validate -> one ordered pass -> revalidate).

Philosophy inherited from the compatibility spec, verbatim:

* validate-then-repair: valid input returns UNCHANGED byte-for-byte.
* ONE finite ordered pass; result is strictly revalidated.
* Deterministic and idempotent: repair(repair(x)) == repair(x).
* NO fuzzy mutation: never invent paths, commands, URLs, approval flags,
  recipients, destructive targets, or select among ambiguous aliases.
* Interrupted/truncated streams are NEVER repaired into executable calls.

Repair rules come from the capability CONTRACT itself (declared
compatibility.aliases / compatibility.coercions in the descriptor's
input_schema) plus a small built-in catalog for Athena's own families.
MCP-origin tools receive only unambiguous case/underscore normalization.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUILTIN_ALIASES",
    "RepairOutcome",
    "RepairReceipt",
    "ToolInputRepairer",
]

_REPAIR_POLICY_VERSION = "athena-repair-1"


class RepairOutcome:
    UNCHANGED = "UNCHANGED"
    REPAIRED = "REPAIRED"
    INVALID = "INVALID"


@dataclass
class RepairReceipt:
    call_id: str
    tool_name: str
    outcome: str
    rules: list[str] = field(default_factory=list)
    issue_codes: list[str] = field(default_factory=list)
    original_shape_hash: str | None = None
    repaired_shape_hash: str | None = None
    schema_hash: str | None = None
    repair_policy_version: str = _REPAIR_POLICY_VERSION
    provider_profile_id: str | None = None
    model_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id, "tool_name": self.tool_name,
            "outcome": self.outcome, "rules": self.rules,
            "issue_codes": self.issue_codes,
            "original_shape_hash": self.original_shape_hash,
            "repaired_shape_hash": self.repaired_shape_hash,
            "schema_hash": self.schema_hash,
            "repair_policy_version": self.repair_policy_version,
            "provider_profile_id": self.provider_profile_id,
            "model_id": self.model_id,
        }


def _shape_hash(args: Any) -> str:
    import hashlib

    if isinstance(args, Mapping):
        keys = ",".join(sorted(args)) if args else ""
        types = ",".join(type(v).__name__
                         for _, v in sorted((args or {}).items()))
        payload = f"keys={keys};types={types}"
    else:
        payload = type(args).__name__
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# Built-in alias catalog for Athena's own capability families.
# Canonical capability/field -> accepted alternative names.
BUILTIN_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "execute": {
        "code": ("script", "source", "program"),
        "language": ("lang",),
        "timeout": ("timeout_seconds", "timeoutSec"),
    },
    "terminal_session": {
        "command": ("cmd", "shell", "bash", "script"),
        "session": ("session_id", "sid"),
        "pattern": ("expect",),
        "text": ("input", "line"),
    },
    "process": {
        "pid": ("process_id", "process"),
        "signal": ("sig",),
        "timeout": ("timeout_seconds",),
    },
    "fs": {
        "path": ("file_path", "filepath", "file", "target"),
        "content": ("body", "data", "contents", "fileContent"),
    },
    "debugger": {
        "session": ("session_id",),
        "script": ("file", "path"),
        "expression": ("expr",),
    },
    "database": {
        "sql": ("query", "statement"),
        "path": ("database", "db", "db_path"),
    },
    "watch": {"path": ("directory", "dir", "folder")},
    "workspace": {"checkpoint_id": ("snapshot_id",)},
}

# Fields where a bare-scalar root gets wrapped when the schema has exactly
# one required property of matching type.
_ROOT_WRAP_FIELDS = ("command", "code", "path", "query", "sql", "task")


@dataclass
class _Ctx:
    tool_name: str
    schema: Mapping[str, Any]
    rules: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


class ToolInputRepairer:
    """One bounded deterministic repair pass at the model boundary."""

    def __init__(self, *, mode: str = "safe",
                 candidates=None) -> None:
        self.mode = mode                    # safe | strict | off
        self.candidates = candidates        # CompatibilityCandidates telemetry

    # -- public entry -----------------------------------------------------
    def repair(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Any,
        input_schema: Mapping[str, Any],
        validate_fn,
        mcp_origin: bool = False,
        completion_state: str = "CLEAN",
        provider_profile_id: str | None = None,
        model_id: str | None = None,
        mode: str | None = None,
    ) -> tuple[Any, RepairReceipt]:
        """Returns (canonical_arguments, receipt). Never raises."""
        receipt = RepairReceipt(
            call_id=call_id, tool_name=tool_name,
            outcome=RepairOutcome.UNCHANGED,
            original_shape_hash=_shape_hash(arguments),
            schema_hash=_schema_fp(input_schema),
            provider_profile_id=provider_profile_id,
            model_id=model_id,
        )

        # Interrupted streams are never repaired into executable calls.
        if completion_state != "CLEAN":
            receipt.outcome = RepairOutcome.INVALID
            receipt.issue_codes.append("stream_interrupted")
            return None, receipt

        # A provider may emit a tool-call envelope before it emits any
        # argument bytes. An empty string is incomplete input, not a scalar
        # that can safely be wrapped into a required field such as ``path``.
        if isinstance(arguments, str) and not arguments.strip():
            receipt.outcome = RepairOutcome.INVALID
            receipt.issue_codes.append("empty_arguments")
            return None, receipt

        selected_mode = mode or self.mode
        if selected_mode == "off":
            ok = not validate_fn(input_schema, arguments)
            receipt.outcome = (RepairOutcome.UNCHANGED if ok
                               else RepairOutcome.INVALID)
            if not ok:
                receipt.issue_codes.append("invalid_unrepaired")
            return arguments, receipt

        errors = validate_fn(input_schema, arguments)
        if not errors:
            receipt.outcome = RepairOutcome.UNCHANGED
            return arguments, receipt

        ctx = _Ctx(tool_name=tool_name, schema=input_schema,
                   rules=[], issues=list(errors))
        repaired, changed = _repair_pass(arguments, input_schema, ctx,
                                         mcp_origin=mcp_origin)
        if repaired is None:
            receipt.outcome = RepairOutcome.INVALID
            receipt.issue_codes.extend(ctx.issues[:5])
            return None, receipt

        # Strict revalidation against the exact advertised schema.
        remaining = validate_fn(input_schema, repaired)
        if remaining:
            receipt.outcome = RepairOutcome.INVALID
            receipt.issue_codes.extend(remaining[:5])
            return None, receipt

        if changed:
            receipt.outcome = RepairOutcome.REPAIRED
            receipt.rules = list(ctx.rules)
            receipt.repaired_shape_hash = _shape_hash(repaired)
            if self.candidates is not None:
                for rule in ctx.rules:
                    rule_name = rule.split(":", 1)[0]
                    self.candidates.record_failure(
                        model=model_id or "unknown", capability=tool_name,
                        rule=rule_name, detail=str(sorted(args2_keys(arguments))))
            return repaired, receipt

        receipt.outcome = RepairOutcome.INVALID
        receipt.issue_codes.extend(remaining[:5])
        return None, receipt


def _schema_fp(schema) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(
        dict(schema), sort_keys=True).encode()).hexdigest()[:16]


def args2_keys(args):
    return args if isinstance(args, Mapping) else {}


def _repair_pass(args, schema: Mapping[str, Any], ctx: _Ctx,
                 *, mcp_origin: bool) -> tuple[dict | None, bool]:
    """Apply the ordered safe rules once. Returns (obj, changed)."""
    obj, changed = args, False

    # Rule 1a: double-encoded JSON string.
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    obj = parsed
                    changed = True
                    ctx.rules.append("json_double_decode")
            except (ValueError, TypeError):
                pass

    # Rule 1b: raw control characters inside JSON strings (lexical proof).
    if (isinstance(obj, str) and not changed
            and '"' in obj and re.search(r'[\n\r\t]', obj)):
        fixed = _escape_controls_in_strings(obj)
        if fixed != obj:
            try:
                parsed = json.loads(fixed)
                if isinstance(parsed, dict):
                    obj = parsed
                    changed = True
                    ctx.rules.append("control_char_escape")
            except (ValueError, TypeError):
                pass

    # Rule 1c: direct parse of non-dict (e.g. model sent a JSON array wrapper).
    if isinstance(obj, str):
        try:
            parsed = json.loads(obj)
            if isinstance(parsed, dict):
                obj = parsed
                changed = True
                ctx.rules.append("json_string_parse")
        except (ValueError, TypeError):
            pass

    if not isinstance(obj, dict):
        # Rule 3: root wrapping for single-primary-field tools.
        wrapped = _root_wrap(obj, schema, ctx)
        return wrapped, wrapped is not args

    obj = dict(obj)
    props: dict = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    # Rule 4: explicit alias rename (canonical absent, exactly one alias).
    aliases = BUILTIN_ALIASES.get(ctx.tool_name, {})
    declared = schema.get("x-athena-aliases") or {}
    for canonical in props:
        if canonical in obj:
            continue
        sources: list[str] = []
        declared_aliases = declared.get(canonical) or []
        sources.extend(str(a) for a in declared_aliases)
        sources.extend(aliases.get(canonical, ()))
        present = [s for s in sources if s in obj]
        if len(present) == 1:
            obj[canonical] = obj.pop(present[0])
            changed = True
            ctx.rules.append(f"alias:{present[0]}->{canonical}")
        elif len(present) > 1:
            ctx.issues.append(f"ambiguous_alias:{canonical}")
            # Prohibited: selecting among multiple interpretations.

    # MCP origin: unambiguous case/underscore variants ONLY.
    if mcp_origin:
        lowered = {k.lower().replace("-", "_"): k for k in list(obj)}
        for prop in props:
            if prop in obj:
                continue
            variant = lowered.get(prop.lower())
            if variant is not None and variant != prop:
                obj[prop] = obj.pop(variant)
                changed = True
                ctx.rules.append(f"mcp_case:{variant}->{prop}")

    # Rule 7: exact coercions where target type makes it unambiguous.
    for prop, spec in props.items():
        if prop not in obj:
            continue
        value = obj[prop]
        expected = spec.get("type")
        if expected == "number" and isinstance(value, str):
            try:
                num = float(value)
                obj[prop] = int(num) if num.is_integer() else num
                changed = True
                ctx.rules.append(f"numeric_string:{prop}")
            except ValueError:
                pass
        elif expected == "integer" and isinstance(value, str):
            if re.fullmatch(r"-?\d+", value.strip()):
                obj[prop] = int(value)
                changed = True
                ctx.rules.append(f"numeric_string:{prop}")
        elif expected == "boolean" and isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "false"):
                obj[prop] = low == "true"
                changed = True
                ctx.rules.append(f"bool_string:{prop}")
        elif (expected == "array" and not isinstance(value, list)
              and isinstance(value, (str, int, float, bool))):
            obj[prop] = [value]
            changed = True
            ctx.rules.append(f"scalar_to_array:{prop}")

    # Rule 5/6: optional null / placeholder removal.
    for prop, spec in props.items():
        if prop in obj and obj[prop] is None and prop not in required:
            nullable = spec.get("nullable") or spec.get("type") == "null"
            if not nullable:
                del obj[prop]
                changed = True
                ctx.rules.append(f"null_removal:{prop}")

    return obj, changed


def _root_wrap(value, schema: Mapping[str, Any], ctx: _Ctx) -> dict | None:
    """Wrap a bare scalar only for one unambiguous primary field."""
    props = schema.get("properties") or {}
    required = [r for r in (schema.get("required") or []) if r in props]
    if len(required) != 1:
        return None
    primary = required[0]
    if primary not in _ROOT_WRAP_FIELDS:
        return None
    expected = props[primary].get("type")
    match = ((expected == "string" and isinstance(value, str))
             or (expected == "number" and isinstance(value, (int, float)))
             or (expected == "integer" and isinstance(value, int)))
    if not match:
        return None
    ctx.rules.append(f"root_wrap:{primary}")
    return {primary: value}


_CTRL_MAP = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_controls_in_strings(text: str) -> str:
    """Escape raw control chars ONLY where lexical analysis proves they are
    inside a JSON string literal (between unescaped quotes)."""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in _CTRL_MAP:
            out.append(_CTRL_MAP[ch])
        else:
            out.append(ch)
    return "".join(out)
