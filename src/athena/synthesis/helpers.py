"""Pure synthesis command/inspection helpers.

Owned by neither the engine nor its extracted mechanisms. Extracted from
engine.py so validation/promotion/child_runtime no longer need dynamic
owner-backimports (review item 15).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from athena.affordances.validation import ValidationTier  # noqa: F401
from athena.schema import validate_schema
from athena.synthesis.runtime import GeneratedToolHost

if TYPE_CHECKING:
    from athena.synthesis.models import SyntheticCapability

_GENERATED_EFFECTIVE_AUTHORITY = frozenset(
    {
        "READ_LOCAL",
        "EXECUTE",
    }
)

_MISSING = object()


def _child_code(cap_code_repr: str, *, persistent: bool = False) -> str:
    """Build the sandboxed child-process program for one capability.

    ``athena`` is a deliberately tiny global API, backed by framed IPC. The
    generated source still has the strict ``run(args)`` contract; it does not
    receive a dispatcher or any host object directly.
    """
    execution = (
        (
            "while True:\n"
            "    raw = sys.stdin.readline()\n"
            "    if not raw:\n"
            "        break\n"
            "    try:\n"
            "        ARGS = json.loads(raw or '{}')\n"
            "        result = NS['run'](ARGS)\n"
            "        sys.stdout.write('__RESULT__' + json.dumps(result) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    except Exception as exc:\n"
            "        sys.stdout.write('__ERROR__' + json.dumps({'error': str(exc)}) + '\\n')\n"
            "        sys.stdout.flush()\n"
        )
        if persistent
        else (
            'ARGS = json.loads(sys.stdin.readline() or "{}")\n'
            'result = NS["run"](ARGS)\n'
            'sys.stdout.write("__RESULT__" + json.dumps(result) + "\\n")\n'
            "sys.stdout.flush()\n"
        )
    )
    return (
        "import json, sys\n"
        "class _GeneratedHost:\n"
        "    def call(self, capability_id, arguments):\n"
        "        request = {'capability_id': capability_id, 'arguments': arguments}\n"
        "        sys.stdout.write('__HOST__' + json.dumps(request) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "        response = sys.stdin.readline()\n"
        "        if not response:\n"
        "            raise RuntimeError('generated host closed without a response')\n"
        "        envelope = json.loads(response)\n"
        "        if not envelope.get('ok'):\n"
        "            raise RuntimeError(str(envelope.get('error') or 'host call failed'))\n"
        "        return envelope.get('value')\n"
        "NS = {}\n"
        "NS['athena'] = _GeneratedHost()\n"
        f"exec({cap_code_repr}, NS)\n" + execution
    )


def _candidate_ready(cap: SyntheticCapability) -> bool:
    """Require proof scaled to the capability's actual effect risk."""
    if cap.validation.get("all_passed") is not True or cap.failures != 0:
        return False
    risk_tier = _risk_tier(cap)
    minimum_uses = {"low": 3, "medium": 4, "high": 5}[risk_tier]
    minimum_contexts = 2 if risk_tier in {"medium", "high"} else 1
    return bool(
        cap.uses >= minimum_uses
        and cap.successes >= minimum_uses
        and len(cap.input_signatures) >= (2 if risk_tier != "low" else 1)
        and len(cap.task_context_signatures) >= minimum_contexts
    )


def _admit_generated_input(cap: SyntheticCapability, arguments: object) -> list[str]:
    """Apply the same input admission boundary used by live executors."""
    return validate_schema(cap.input_schema, arguments)


def _promotion_proof_error(
    cap: SyntheticCapability,
    tier: ValidationTier,
) -> str | None:
    """Return the missing proof required to widen a capability's lifetime."""
    risk_tier = _risk_tier(cap)
    if cap.validation.get("all_passed") is not True:
        return "target-tier behavioral validation did not pass"
    if cap.failures:
        return "unresolved live failures remain"
    minimum_uses = {"low": 1, "medium": 2, "high": 3}[risk_tier]
    if cap.uses < minimum_uses or cap.successes < minimum_uses:
        return f"{risk_tier}-risk promotion requires {minimum_uses} verified uses"
    minimum_contexts = 2 if risk_tier in {"medium", "high"} else 1
    if len(cap.task_context_signatures) < minimum_contexts:
        return f"{risk_tier}-risk promotion requires {minimum_contexts} task contexts"
    if risk_tier != "low" and len(cap.input_signatures) < 2:
        return f"{risk_tier}-risk promotion requires two distinct inputs"
    if cap.validation.get("tier") != tier.value:
        return f"behavioral validation at {tier.value} tier is required"
    if cap.validation.get("all_passed") is not True:
        return "target-tier behavioral validation did not pass"
    if cap.failures:
        return "unresolved live failures remain"
    negative_total = int(cap.validation.get("negative_cases_total") or 0)
    negative_passed = int(cap.validation.get("negative_cases_passed") or 0)
    if negative_total < 1 or negative_passed != negative_total:
        return "service-generated negative input cases are incomplete"
    minimum_verifications = {"low": 1, "medium": 2, "high": 3}[risk_tier]
    if cap.downstream_verifications < minimum_verifications:
        return (
            f"{risk_tier}-risk promotion requires {minimum_verifications} canonical "
            "passing VerificationCompleted events"
        )
    risk_tier = str(cap.validation.get("risk_tier") or risk_tier)
    if risk_tier in {"medium", "high"}:
        invariant_cases = sum(
            1
            for case in (cap.validation_cases or [])
            if case.get("invariants") or case.get("verification_requirements")
        )
        if invariant_cases < 1:
            return f"{risk_tier}-risk promotion requires an invariant verification case"
    if (
        tier in {ValidationTier.PROJECT, ValidationTier.USER}
        and len(cap.task_context_signatures) < 2
    ):
        return f"{tier.value} promotion requires two distinct task contexts"
    if tier is ValidationTier.USER and len(cap.environment_fingerprints) < 2:
        return "user promotion requires portability across two environments"
    return None


def _input_signature(arguments: object) -> str:
    """Return a stable bounded identity for live-use diversity evidence."""
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _service_negative_cases(schema: Mapping[str, object]) -> list[dict[str, object]]:
    """Generate deterministic contract-negative inputs from JSON Schema.

    These are service-owned checks, not model-authored examples.  They never
    execute generated code: the admission boundary must prove that malformed
    input is rejected by the same validator used by the dispatcher.  The
    root-type mutation gives every object contract at least one negative case;
    constrained properties add representative required/type/bounds cases.
    """
    schema_type = schema.get("type")
    invalid_root: object | None = None
    if schema_type == "object":
        invalid_root = []
    elif schema_type == "array":
        invalid_root = {}
    elif schema_type == "string":
        invalid_root = 1
    elif schema_type in {"integer", "number"}:
        invalid_root = "not-a-number"
    elif schema_type == "boolean":
        invalid_root = "not-a-boolean"
    elif schema_type == "null":
        invalid_root = {}

    candidates: list[dict[str, object]] = []
    if invalid_root is not None:
        candidates.append(
            {
                "input": invalid_root,
                "source": "service_negative",
                "expect_invalid_input": True,
            }
        )
    if schema_type != "object":
        return candidates

    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    required = schema.get("required")
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        required_names = [str(name) for name in required]
        if required_names:
            candidates.append(
                {
                    "input": {},
                    "source": "service_negative",
                    "expect_invalid_input": True,
                    "mutator": "remove_required",
                    "field": required_names[0],
                }
            )
    if schema.get("additionalProperties") is False:
        candidates.append(
            {
                "input": {"__athena_unknown_field__": True},
                "source": "service_negative",
                "expect_invalid_input": True,
                "mutator": "add_unknown_property",
            }
        )

    for raw_name, raw_spec in properties.items():
        name = str(raw_name)
        if not isinstance(raw_spec, Mapping):
            continue
        value: object | None = None
        has_value = True
        value_type = raw_spec.get("type")
        if value_type == "string":
            value = 1
            if isinstance(raw_spec.get("minLength"), int) and raw_spec["minLength"] > 0:
                value = ""
        elif value_type in {"integer", "number"}:
            value = "not-a-number"
        elif value_type == "boolean":
            value = "not-a-boolean"
        elif value_type == "array":
            value = {}
        elif value_type == "object":
            value = []
        elif value_type == "null":
            value = True
        elif isinstance(raw_spec.get("enum"), Sequence) and raw_spec["enum"]:
            value = "__athena_invalid_enum__"
        else:
            has_value = False
        if has_value:
            candidates.append(
                {
                    "input": {name: value},
                    "source": "service_negative",
                    "expect_invalid_input": True,
                    "mutator": "wrong_type_or_enum",
                    "field": name,
                }
            )

    # Schemas expressed through composition (oneOf/anyOf/not/const, or a
    # schema without an explicit root type) may not expose a property-level
    # mutation. Probe a small fixed corpus and retain the first value that the
    # same validator rejects. This keeps negative proof service-owned without
    # inventing a case that is actually valid for the contract.
    probe_values: tuple[object, ...] = (None, [], {}, "", 0, False)
    for value in probe_values:
        if validate_schema(dict(schema), value):
            candidates.append(
                {
                    "input": value,
                    "source": "service_negative",
                    "expect_invalid_input": True,
                    "mutator": "schema_constraint",
                }
            )
            break

    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for case in candidates:
        identity = json.dumps(case, sort_keys=True, separators=(",", ":"), default=str)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(case)
    return unique


def _apply_workspace_fixture(case: Mapping[str, object], root: str) -> None:
    """Materialize bounded fixture files inside one disposable workspace."""
    fixture = case.get("workspace_files")
    if fixture is None:
        # ``workspace`` is accepted as a compact alias for callers that use
        # the validation vocabulary directly. It is never the live workspace.
        fixture = case.get("workspace")
    if fixture is None:
        fixture = case.get("workspace_fixture")
    if fixture is None:
        return
    if not isinstance(fixture, Mapping):
        raise TypeError("workspace_files must be an object mapping paths to content")
    root_real = os.path.realpath(os.path.abspath(root))
    for raw_path, content in fixture.items():
        path = str(raw_path)
        if os.path.isabs(path):
            raise ValueError(f"workspace fixture path must be relative: {path}")
        normalized = os.path.normpath(path)
        if normalized in {"", ".", ".."} or normalized.startswith(".." + os.sep):
            raise ValueError(f"workspace fixture path escapes workspace: {path}")
        target = os.path.realpath(os.path.join(root_real, normalized))
        if target != root_real and not target.startswith(root_real + os.sep):
            raise ValueError(f"workspace fixture path escapes workspace: {path}")
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)


def _check_effect_expectations(
    case: Mapping[str, object],
    host: GeneratedToolHost | None,
) -> str | None:
    """Check which governed native calls a validation fixture exercised."""
    raw = case.get("expect_effects", case.get("expect_effect", _MISSING))
    forbidden = case.get("expect_no_effects")
    if raw is not _MISSING and host is None:
        return "effect expectations require a governed host context"
    calls = list(host.calls) if host is not None else []
    if raw is not _MISSING:
        expected = raw if isinstance(raw, (list, tuple)) else [raw]
        for item in expected:
            if isinstance(item, str):
                matched = any(call.get("capability_id") == item for call in calls)
            elif isinstance(item, Mapping):
                capability = item.get("capability_id") or item.get("capability")
                operation = item.get("operation")
                matched = any(
                    (not capability or call.get("capability_id") == capability)
                    and (
                        operation is None or call.get("arguments", {}).get("operation") == operation
                    )
                    for call in calls
                )
            else:
                matched = False
            if not matched:
                return f"expected governed effect was not observed: {item}"
    if forbidden is not None:
        values = forbidden if isinstance(forbidden, (list, tuple)) else [forbidden]
        for item in values:
            if any(
                call.get("capability_id") == item
                or (
                    isinstance(item, Mapping)
                    and call.get("capability_id")
                    == (item.get("capability_id") or item.get("capability"))
                    and (
                        item.get("operation") is None
                        or call.get("arguments", {}).get("operation") == item.get("operation")
                    )
                )
                for call in calls
            ):
                return f"forbidden governed effect was observed: {item}"
    return None


def _workspace_snapshot(root: str | None) -> dict[str, str] | None:
    if root is None:
        return None
    snapshot: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in {".git", "__pycache__"})
        for name in sorted(filenames):
            path = os.path.join(directory, name)
            try:
                data = open(path, "rb").read(2_000_000)
            except OSError:
                continue
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            snapshot[relative] = hashlib.sha256(data).hexdigest()
    return snapshot


def _check_resource_expectations(
    case: Mapping[str, object],
    root: str | None,
    before: dict[str, str] | None,
) -> tuple[str | None, list[str]]:
    expected_changed = case.get("changed_resources", case.get("expected_changed_resources"))
    expected_unchanged = case.get("unchanged_resources", case.get("expected_unchanged_resources"))
    if expected_changed is None and expected_unchanged is None:
        return None, []
    if root is None or before is None:
        return "resource expectations require a workspace fixture", []
    after = _workspace_snapshot(root) or {}
    changed = sorted(
        {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    )
    if expected_changed is not None:
        expected = sorted(
            str(value)
            for value in (
                expected_changed
                if isinstance(expected_changed, (list, tuple))
                else [expected_changed]
            )
        )
        if changed != expected:
            return (
                f"expected changed resources {expected}, observed {changed}",
                changed,
            )
    if expected_unchanged is not None:
        unchanged = [
            str(value)
            for value in (
                expected_unchanged
                if isinstance(expected_unchanged, (list, tuple))
                else [expected_unchanged]
            )
        ]
        violated = sorted(path for path in unchanged if path in changed)
        if violated:
            return f"forbidden resource changes: {violated}", changed
    return None, changed


async def _check_invariants(
    case: Mapping[str, object],
    host: GeneratedToolHost | None,
) -> str | None:
    """Run postconditions through the same host boundary as the tool."""
    raw = case.get("invariants") or ()
    if not raw:
        return None
    if host is None:
        return "invariants require a governed host context"
    if not isinstance(raw, (list, tuple)):
        return "invariants must be a list"
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            return f"invariant {index} must be an object"
        capability_id = str(
            item.get("capability_id")
            or item.get("capability")
            or ("execute" if item.get("command") else "")
        )
        arguments = dict(item.get("arguments") or item.get("args") or {})
        if item.get("command") is not None:
            arguments = {
                "language": str(item.get("language") or "shell"),
                "code": str(item["command"]),
                **arguments,
            }
        if not capability_id:
            return f"invariant {index} has no capability_id or command"
        try:
            value = await host.call(capability_id, arguments)
        except Exception as exc:  # noqa: BLE001 - fixture failure is evidence
            return f"invariant {index} failed: {exc}"
        expected = item.get("expect_output", _MISSING)
        if expected is not _MISSING and value != expected:
            return f"invariant {index} output mismatch"
        contains = item.get("expect_output_contains")
        if (
            contains is not None
            and str(contains).casefold() not in json.dumps(value, sort_keys=True).casefold()
        ):
            return f"invariant {index} output did not contain {contains!r}"
    return None


def _schema_for_values(values: list[object]) -> dict:
    """Infer a conservative JSON Schema from observed JSON results."""
    schemas = [_schema_for_value(value) for value in values]
    unique = {json.dumps(schema, sort_keys=True) for schema in schemas}
    if len(unique) == 1:
        return schemas[0]
    return {"anyOf": [schemas[index] for index in _first_schema_indexes(schemas)]}


def _first_schema_indexes(schemas: list[dict]) -> list[int]:
    """Select the first occurrence of each distinct schema shape."""
    unique: list[dict] = []
    indexes: list[int] = []
    for index, schema in enumerate(schemas):
        marker = json.dumps(schema, sort_keys=True, default=str)
        if not any(json.dumps(item, sort_keys=True, default=str) == marker for item in unique):
            unique.append(schema)
            indexes.append(index)
    return indexes


def _risk_tier(cap: SyntheticCapability) -> str:
    """Classify promotion proof by the capability's declared native effects."""
    effects = {getattr(effect, "value", str(effect)) for effect in cap.effects}
    if effects == {"READ_LOCAL"}:
        return "low"
    if effects and effects <= {"READ_LOCAL", "EXECUTE"} and "EXECUTE" in effects:
        return "medium"
    return "high"


def canonical_generated_identity(cap: SyntheticCapability) -> str:
    """Serialize the resolved generated-capability identity canonically.

    This is evaluated after source formatting, schema inference, dependency
    resolution, effect resolution, and lineage assignment. Request fields are
    not sufficient identity because repair/merge operations can change the
    effective contract.
    """
    effects = sorted(getattr(effect, "value", str(effect)) for effect in cap.effects)
    dependencies = [
        {
            "name": dependency.name,
            "manager": dependency.manager,
            "version": dependency.version,
            "reason": dependency.reason,
            "required_for": dependency.required_for,
        }
        for dependency in cap.required_dependencies
    ]
    evidence = [dependency.to_record() for dependency in cap.evidence_dependencies]
    dependency_lock = {
        key: value for key, value in dict(cap.dependency_lock or {}).items() if key != "target"
    }
    return json.dumps(
        {
            "source": cap.code,
            "runtime": cap.runtime,
            "input_schema": cap.input_schema,
            "output_schema": cap.output_schema or {},
            "effects": effects,
            "effective_authority": sorted(cap.effective_effects),
            "required_capabilities": sorted(cap.required_capabilities),
            "packages": dependencies,
            "dependency_lock": dependency_lock,
            "evidence": evidence,
            "family_id": cap.family_id,
            "revision": cap.revision,
            "parent_revision": cap.parent_revision,
            "active_revision": cap.active_revision,
            "supersedes": sorted(cap.supersedes),
            "superseded_by": cap.superseded_by,
            "compatibility": cap.provenance.get("compatibility"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _schema_for_value(value: object) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {
            "type": "array",
            "items": _schema_for_values(value) if value else {},
        }
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {str(key): _schema_for_values([item]) for key, item in value.items()},
            "required": [str(key) for key in value],
            "additionalProperties": False,
        }
    return {}
