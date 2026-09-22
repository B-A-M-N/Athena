"""Model-facing contract for the durable research capability.

The schema and effect envelope are kept separate from the implementation so
the capability's public contract can be reviewed without traversing its
storage, acquisition, and workflow code.
"""

from __future__ import annotations

from athena.capabilities.operations import native_descriptor
from athena.protocol.capabilities import CapabilityOrigin, EffectClass, ResourceClass

_SOURCE_TYPES = ("web", "paper", "documentation", "dataset", "code", "local")
_EVIDENCE_TYPES = ("quote", "measurement", "observation", "derivation", "execution")
_GAP_KINDS = (
    "unsupported_claim",
    "conflict",
    "stale_source",
    "source_quality",
    "unanswered_question",
)

RESEARCH_DESCRIPTOR = native_descriptor(
    id="research",
    description=(
        "Durable evidence-backed research records: record and list source "
        "snapshots, discover bounded candidates, extract exact evidence, link evidence to Athena claims, "
        "track research gaps, search the local corpus, verify excerpts, and "
        "plan/assess/bundle explicit research requirements. Planning is "
        "deterministic and local; external fetching is a separate "
        "allowlisted operation."
    ),
    tags=frozenset(
        {
            "research",
            "evidence",
            "source",
            "sources",
            "verify",
            "verification",
            "web",
            "latest",
            "release",
        }
    ),
    input_schema={
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "fetch",
                    "discover",
                    "record_source",
                    "sources",
                    "search",
                    "record_evidence",
                    "evidence",
                    "record_gap",
                    "gaps",
                    "close_gap",
                    "verify",
                    "plan",
                    "assess",
                    "critique",
                    "bundle",
                    "run",
                ],
            },
            "uri": {"type": "string", "minLength": 1, "maxLength": 4096},
            "title": {"type": "string", "maxLength": 1000},
            "source_type": {"type": "string", "enum": list(_SOURCE_TYPES)},
            "content": {"type": "string", "maxLength": 10_000_000},
            "artifact_uri": {"type": "string", "maxLength": 4096},
            "published_at": {"type": "string", "maxLength": 128},
            "source_id": {"type": "string", "maxLength": 128},
            "gap_id": {"type": "string", "maxLength": 128},
            "evidence_id": {"type": "string", "maxLength": 128},
            "claim_id": {"type": "string", "maxLength": 128},
            "claim": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "excerpt": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "locator": {"type": "object", "additionalProperties": True},
            "evidence_type": {"type": "string", "enum": list(_EVIDENCE_TYPES)},
            "extraction_method": {"type": "string", "maxLength": 128},
            "extraction_model": {"type": "string", "maxLength": 256},
            "receipt": {"type": "object", "additionalProperties": True},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence_calibration": {"type": "object", "additionalProperties": True},
            "corroborates": {"type": "array", "items": {"type": "string"}},
            "contradicts": {"type": "array", "items": {"type": "string"}},
            "objective": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "question": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "requirements": {
                "type": "array",
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question"],
                    "properties": {
                        "id": {"type": "string", "maxLength": 128},
                        "question": {"type": "string", "minLength": 1, "maxLength": 20_000},
                        "claim_id": {"type": "string", "maxLength": 128},
                        "kind": {"type": "string", "enum": list(_GAP_KINDS)},
                        "required": {"type": "boolean"},
                        "queries": {
                            "type": "array",
                            "maxItems": 5,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                        },
                    },
                },
            },
            "queries": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "uris": {
                "type": "array",
                "maxItems": 50,
                "items": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
            "source_specs": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["uri"],
                    "properties": {
                        "uri": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "title": {"type": "string", "maxLength": 1000},
                        "source_type": {"type": "string", "enum": list(_SOURCE_TYPES)},
                        "content": {"type": "string", "maxLength": 10_000_000},
                        "artifact_uri": {"type": "string", "maxLength": 4096},
                        "published_at": {"type": "string", "maxLength": 128},
                        "metadata": {"type": "object", "additionalProperties": True},
                    },
                },
            },
            "extractions": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["claim", "excerpt"],
                    "properties": {
                        "source_id": {"type": "string", "maxLength": 128},
                        "uri": {"type": "string", "maxLength": 4096},
                        "claim_id": {"type": "string", "maxLength": 128},
                        "claim": {"type": "string", "minLength": 1, "maxLength": 20_000},
                        "excerpt": {"type": "string", "minLength": 1, "maxLength": 20_000},
                        "locator": {"type": "object", "additionalProperties": True},
                        "evidence_type": {"type": "string", "enum": list(_EVIDENCE_TYPES)},
                        "extraction_method": {"type": "string", "maxLength": 128},
                        "extraction_model": {"type": "string", "maxLength": 256},
                        "receipt": {"type": "object", "additionalProperties": True},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "confidence_calibration": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                        "corroborates": {"type": "array", "items": {"type": "string"}},
                        "contradicts": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object", "additionalProperties": True},
                    },
                },
            },
            "gap_ids": {"type": "array", "items": {"type": "string", "maxLength": 128}},
            "kind": {"type": "string", "enum": list(_GAP_KINDS)},
            "required": {"type": "boolean"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "claim_ids": {"type": "array", "items": {"type": "string", "maxLength": 128}},
            "min_independent_groups": {"type": "integer", "minimum": 1, "maximum": 10},
            "query": {"type": "string", "maxLength": 2000},
            "status": {"type": "string", "enum": ["OPEN", "CLOSED"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "autonomous": {"type": "boolean"},
            "max_research_rounds": {"type": "integer", "minimum": 1, "maximum": 3},
            "max_sources": {"type": "integer", "minimum": 1, "maximum": 20},
            "max_queries": {"type": "integer", "minimum": 1, "maximum": 20},
            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
            "max_research_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50_000_000,
            },
            "metadata": {"type": "object", "additionalProperties": True},
        },
        "additionalProperties": False,
    },
    effects=frozenset(
        {
            EffectClass.READ_LOCAL,
            EffectClass.WRITE_LOCAL,
            EffectClass.NETWORK_READ,
        }
    ),
    resources=frozenset({ResourceClass.RESEARCH}),
    origin=CapabilityOrigin.NATIVE,
)

__all__ = ["RESEARCH_DESCRIPTOR"]
