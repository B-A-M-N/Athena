"""ToolCallCandidate boundary: raw malformed tool arguments survive to repair."""

import json

from athena.models.compat.candidates import (
    ToolCallCandidate,
    clear_raw_candidates,
    get_raw_candidate,
    record_raw_candidate,
)
from athena.models.compat.toolrepair import RepairOutcome, ToolInputRepairer


def _validate(schema, args):
    """Minimal required/type validator for tests."""
    errors = []
    if not isinstance(args, dict):
        return ["root_not_object"]
    for key in schema.get("required", []):
        if key not in args:
            errors.append(f"missing:{key}")
    for key, spec in (schema.get("properties") or {}).items():
        if key in args and spec.get("type") == "string" \
                and not isinstance(args[key], str):
            errors.append(f"type:{key}")
    return errors


# -- parse round trip -------------------------------------------------------

def test_parse_success_populates_parsed_arguments():
    raw = json.dumps({"path": "/tmp/x"})
    c = ToolCallCandidate.parse("call-1", "fs", raw)
    assert c.parsed_arguments == {"path": "/tmp/x"}
    assert c.raw_arguments == raw
    assert c.completion_state == "CLEAN"


def test_parse_failure_keeps_raw_and_none():
    raw = '{"path": "a\nb"}'  # invalid JSON: literal control char
    c = ToolCallCandidate.parse("call-2", "fs", raw)
    assert c.parsed_arguments is None
    assert c.raw_arguments == raw  # exactly as sent


def test_parse_never_manufactures_empty_dict():
    # non-dict valid JSON must NOT become parsed_arguments
    c = ToolCallCandidate.parse("call-3", "fs", "[1,2]")
    assert c.parsed_arguments is None
    assert c.raw_arguments == "[1,2]"
    # empty string also fails parse
    c2 = ToolCallCandidate.parse("call-4", "fs", "")
    assert c2.parsed_arguments is None


def test_parse_carries_optional_ids_and_metadata():
    c = ToolCallCandidate.parse(
        "call-5", "fs", "{}",
        provider_profile_id="p1", model_id="m1", stream="openai-compat",
    )
    assert c.provider_profile_id == "p1"
    assert c.model_id == "m1"
    assert c.provider_metadata == {"stream": "openai-compat"}


# -- registry ---------------------------------------------------------------

def test_record_get_registry_roundtrip():
    clear_raw_candidates()
    c = ToolCallCandidate.parse("call-r", "fs", "{bad json")
    record_raw_candidate(c)
    got = get_raw_candidate("call-r")
    assert got is c
    assert get_raw_candidate("missing") is None
    clear_raw_candidates()
    assert get_raw_candidate("call-r") is None


# -- repair of a raw double-encoded string ----------------------------------

def test_repair_double_encoded_raw_string_yields_repaired():
    # Arguments arrived as a JSON *string* wrapping the object — exactly what
    # the dispatcher forwards when a raw candidate is on file.
    raw = json.dumps({"command": "ls -la"})
    assert isinstance(raw, str)
    repairer = ToolInputRepairer(mode="safe")
    args, receipt = repairer.repair(
        call_id="call-d",
        tool_name="terminal_session",
        arguments=raw,
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        validate_fn=_validate,
    )
    assert receipt.outcome == RepairOutcome.REPAIRED
    assert receipt.rules  # some deterministic decode/wrap rule fired
    assert args == {"command": "ls -la"}
    # idempotent: repairing the repaired output changes nothing
    args2, receipt2 = repairer.repair(
        call_id="call-d", tool_name="terminal_session",
        arguments=args,
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        validate_fn=_validate,
    )
    assert receipt2.outcome == RepairOutcome.UNCHANGED
    assert args2 == args


def test_repair_control_char_string_via_candidate_path():
    # Simulates what the dispatcher does with a recorded raw candidate.
    raw = '{"command": "echo a\nb"}'  # literal newline inside the string value
    c = ToolCallCandidate.parse("call-e", "terminal_session", raw)
    assert c.parsed_arguments is None

    repairer = ToolInputRepairer(mode="safe")
    args, receipt = repairer.repair(
        call_id=c.call_id,
        tool_name=c.capability_id,
        arguments=c.raw_arguments,
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        validate_fn=_validate,
    )
    assert receipt.outcome == RepairOutcome.REPAIRED
    assert args == {"command": "echo a\nb"}


def test_empty_candidate_is_not_rewritten_as_an_empty_object():
    candidate = ToolCallCandidate.parse("call-empty", "fs.read", "")
    repairer = ToolInputRepairer(mode="safe")
    args, receipt = repairer.repair(
        call_id=candidate.call_id,
        tool_name=candidate.capability_id,
        arguments=candidate.raw_arguments,
        input_schema={
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
        validate_fn=_validate,
    )
    assert args is None
    assert receipt.outcome == RepairOutcome.INVALID
