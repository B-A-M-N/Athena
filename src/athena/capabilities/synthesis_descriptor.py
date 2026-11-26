"""Static descriptor and input contract for the synthesis capability."""

from __future__ import annotations

import re

from athena.capabilities.operations import native_descriptor
from athena.protocol.capabilities import CapabilityOrigin, EffectClass, ResourceClass

__all__ = ["NAME_PATTERN", "build_synthesis_descriptor"]

NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_EFFECTS = {effect.value for effect in EffectClass}
_SYNTHESIS_EFFECTS = frozenset(
    {
        EffectClass.READ_LOCAL,
        EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS,
        EffectClass.WRITE_LOCAL,
    }
)


def build_synthesis_descriptor():
    """Build the immutable model-facing synthesis contract."""
    return native_descriptor(
        id="synthesis",
        description=(
            "Create a task-local deterministic capability from Python source, "
            "or explicitly repair, migrate, promote, or deprecate a validated tool. "
            "The source is sandbox-validated before registration. Operations: "
            "create/repair/revalidate/migrate_contract/promote_scratch/candidates/inspect/promote/deprecate. Generated run(args) code may compose "
            "governed native tools with athena.call(capability_id, arguments); "
            "those calls remain policy- and RealityGate-checked."
        ),
        tags=frozenset({"synthesis", "create", "generate", "repair", "construct"}),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "create",
                        "repair",
                        "revalidate",
                        "migrate_contract",
                        "promote_scratch",
                        "candidates",
                        "inspect",
                        "promote",
                        "deprecate",
                    ],
                },
                "name": {"type": "string", "pattern": NAME_PATTERN.pattern},
                "description": {"type": "string", "minLength": 1, "maxLength": 1000},
                "code": {"type": "string", "minLength": 1, "maxLength": 200_000},
                "runtime": {
                    "type": "string",
                    "enum": ["python", "python_persistent"],
                },
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "effects": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(_EFFECTS)},
                    "uniqueItems": True,
                },
                "validation_cases": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "args": {"type": "object"},
                            "workspace_files": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "workspace": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "expect_output": {},
                            "expect_output_contains": {},
                            "expected_error": {},
                            "expect_failure": {"type": "boolean"},
                            "expect_invalid_input": {"type": "boolean"},
                            "source": {"type": "string", "maxLength": 64},
                            "expect_error_contains": {"type": "string"},
                            "expect_effect": {},
                            "expect_effects": {"type": "array"},
                            "expect_no_effects": {"type": "array"},
                            "expected_changed_resources": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "expected_unchanged_resources": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "invariants": {"type": "array"},
                            "verification_requirements": {"type": "array"},
                        },
                        "additionalProperties": False,
                    },
                },
                "validation_tier": {
                    "type": "string",
                    "enum": ["scratch", "task", "candidate", "project", "user"],
                    "default": "task",
                },
                "capability_id": {"type": "string", "minLength": 1},
                "scratch_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "scope": {"type": "string", "enum": ["project", "user"]},
                "required_dependencies": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 128},
                            "manager": {"type": "string", "enum": ["python"]},
                            "version": {"type": "string", "maxLength": 64},
                            "reason": {"type": "string", "maxLength": 1000},
                            "required_for": {"type": "string", "maxLength": 256},
                        },
                        "additionalProperties": False,
                    },
                },
                "required_capabilities": {
                    "type": "array",
                    "maxItems": 64,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "evidence_dependencies": {
                    "type": "array",
                    "maxItems": 128,
                    "items": {
                        "type": "object",
                        "required": ["requirement"],
                        "properties": {
                            "requirement": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                            "evidence_id": {"type": "string", "minLength": 1},
                            "source_id": {"type": "string", "minLength": 1},
                            "content_hash": {"type": "string", "minLength": 1},
                            "invalidation": {"type": "string", "maxLength": 256},
                        },
                        "additionalProperties": False,
                    },
                },
                "provenance": {"type": "object", "maxProperties": 32},
                "compatibility": {
                    "type": "string",
                    "enum": ["backward_compatible", "breaking"],
                    "default": "backward_compatible",
                },
                "operator_confirmation": {"type": "boolean"},
            },
            "oneOf": [
                {
                    "properties": {"operation": {"const": "create"}},
                    "required": ["name", "description", "code", "validation_cases"],
                },
                {
                    "properties": {"operation": {"const": "repair"}},
                    "required": [
                        "capability_id",
                        "name",
                        "description",
                        "code",
                        "validation_cases",
                    ],
                },
                {
                    "properties": {"operation": {"const": "revalidate"}},
                    "required": ["capability_id"],
                },
                {
                    "properties": {"operation": {"const": "migrate_contract"}},
                    "required": [
                        "capability_id",
                        "name",
                        "description",
                        "code",
                        "input_schema",
                        "output_schema",
                        "validation_cases",
                    ],
                },
                {"properties": {"operation": {"const": "candidates"}}},
                {"properties": {"operation": {"const": "inspect"}}, "required": ["capability_id"]},
                {
                    "properties": {"operation": {"const": "promote"}},
                    "required": ["capability_id", "scope"],
                },
                {
                    "properties": {"operation": {"const": "deprecate"}},
                    "required": ["capability_id"],
                },
                {
                    "properties": {"operation": {"const": "promote_scratch"}},
                    "required": ["scratch_id"],
                },
            ],
            "additionalProperties": False,
        },
        effects=_SYNTHESIS_EFFECTS,
        resources=frozenset({ResourceClass.SYNTHESIS}),
        origin=CapabilityOrigin.NATIVE,
    )
