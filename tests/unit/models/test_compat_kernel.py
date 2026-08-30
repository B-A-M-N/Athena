"""Inference Compatibility Kernel tests.

Covers the spec's verification requirements: valid calls unchanged,
deterministic idempotent repair, adversarial/prohibited repairs rejected,
interrupted streams never repaired, MCP strictness, and cache accounting.
"""

from __future__ import annotations

import pytest

from athena.capabilities.registry import validate_schema
from athena.models.compat.caching import (
    InferenceReceipt,
    PrefixTracker,
    PromptEnvelope,
    UsageRecord,
)
from athena.models.compat.profiles import (
    PRESETS,
    CompatibilityCandidates,
    ModelProfile,
    resolve_profile,
    schema_fingerprint,
)
from athena.models.compat.toolrepair import RepairOutcome, ToolInputRepairer

EXEC_SCHEMA = {
    "type": "object",
    "required": ["language", "code"],
    "properties": {
        "language": {"type": "string"},
        "code": {"type": "string"},
        "timeout": {"type": "number"},
        "session": {"type": "string"},
    },
}


@pytest.fixture
def repairer():
    return ToolInputRepairer(mode="safe")


def _fix(repairer, args, schema=EXEC_SCHEMA, tool="execute", **kw):
    return repairer.repair(
        call_id="c1",
        tool_name=tool,
        arguments=args,
        input_schema=schema,
        validate_fn=validate_schema,
        **kw,
    )


# -- valid input untouched ---------------------------------------------------


def test_valid_input_unchanged(repairer):
    args = {"language": "python", "code": "print(1)", "timeout": 30}
    out, r = _fix(repairer, args)
    assert out == args
    assert r.outcome == RepairOutcome.UNCHANGED
    assert r.rules == []


# -- alias rename ------------------------------------------------------------


@pytest.mark.athena_scenario("COMPAT-003")
def test_alias_repair_builtin(repairer):
    # fs family: file_path -> path
    fs_schema = {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}
    out, r = _fix(repairer, {"file_path": "foo.py"}, schema=fs_schema, tool="fs")
    assert out == {"path": "foo.py"}
    assert r.outcome == RepairOutcome.REPAIRED
    assert any("file_path->path" in rule for rule in r.rules)


@pytest.mark.athena_scenario("COMPAT-003")
def test_numeric_string_coercion(repairer):
    out, r = _fix(repairer, {"language": "sh", "code": "ls", "timeout": "30"})
    assert out["timeout"] == 30
    assert isinstance(out["timeout"], int)
    assert r.outcome == RepairOutcome.REPAIRED


@pytest.mark.athena_scenario("COMPAT-003")
def test_double_encoded_json(repairer):
    import json

    inner = json.dumps({"language": "python", "code": "print(2)"})
    out, r = _fix(repairer, inner)
    assert out == json.loads(inner)
    assert r.outcome == RepairOutcome.REPAIRED
    assert "json_string_parse" in r.rules or "json_double_decode" in r.rules


@pytest.mark.athena_scenario("COMPAT-003")
def test_repair_is_idempotent(repairer):
    bad = {"file_path": "/tmp/x.py"}
    fs_schema = {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}
    once, _r1 = _fix(repairer, dict(bad), schema=fs_schema, tool="fs")
    twice, r2 = _fix(repairer, dict(once), schema=fs_schema, tool="fs")
    assert twice == once
    assert r2.outcome == RepairOutcome.UNCHANGED


# -- prohibited repairs -------------------------------------------------------


@pytest.mark.athena_scenario("COMPAT-004")
def test_unknown_required_field_is_invalid_not_invented(repairer):
    out, r = _fix(repairer, {"language": "python"})  # code missing entirely
    assert out is None
    assert r.outcome == RepairOutcome.INVALID


def test_ambiguous_aliases_rejected(repairer):
    # Both 'cmd' and 'script' present -> ambiguous -> not selected.
    shell_schema = {
        "type": "object",
        "required": ["command"],
        "properties": {"command": {"type": "string"}},
    }
    out, r = _fix(
        repairer, {"cmd": "a", "script": "b"}, schema=shell_schema, tool="terminal_session"
    )
    assert out is None
    assert r.outcome == RepairOutcome.INVALID


@pytest.mark.athena_scenario("COMPAT-004")
def test_interrupted_stream_never_repaired(repairer):
    out, r = _fix(repairer, {"file_path": "foo.py"}, completion_state="INTERRUPTED")
    assert out is None
    assert r.outcome == RepairOutcome.INVALID
    assert "stream_interrupted" in r.issue_codes


def test_off_mode_diagnostic_only():
    repairer_off = ToolInputRepairer(mode="off")
    out, r = repairer_off.repair(
        call_id="c",
        tool_name="fs",
        arguments={"file_path": "x.py"},
        input_schema={
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
        validate_fn=validate_schema,
    )
    assert out == {"file_path": "x.py"}  # returned as-is for diagnostics
    assert r.outcome == RepairOutcome.INVALID  # flagged invalid, not repaired


def test_control_char_escape_inside_strings():
    raw = '{"code": "print(\n1)"}'  # literal newline inside JSON string
    from athena.models.compat.toolrepair import _escape_controls_in_strings

    repaired = _escape_controls_in_strings(raw)
    import json

    parsed = json.loads(repaired)
    assert "\n" in parsed["code"]


# -- telemetry candidates ------------------------------------------------------


def test_candidates_record_and_propose():
    cands = CompatibilityCandidates()
    for _ in range(12):
        cands.record_failure(
            model="minimax-m3", capability="terminal_session", rule="alias:cmd->command"
        )
    proposals = cands.proposals(min_count=10)
    assert len(proposals) == 1
    assert proposals[0]["count"] == 12
    assert proposals[0]["ambiguity"] == 0
    # Below threshold: not proposed (never auto-promotes).
    assert not cands.proposals(min_count=100)


# -- profiles / presets --------------------------------------------------------


def test_presets_keyless_local():
    p = PRESETS["ollama"]
    assert p.auth_mode == "keyless"
    resolved = resolve_profile("ollama", model_id="qwen3:8b")
    assert resolved.model_id == "qwen3:8b"
    assert resolved.base_url == "http://127.0.0.1:11434/v1"


def test_hosted_openai_compatible_profile_enables_prefix_cache_key():
    hosted = resolve_profile(
        "openai-compat",
        base_url="https://freeinference.org/v1",
        model_id="glm-5.2",
    )
    local = resolve_profile("ollama", model_id="qwen3:8b")

    assert hosted.cache_mode == "automatic-prefix"
    assert local.cache_mode == "none"
    assert resolve_profile("openai-compat", cache_mode="none").cache_mode == "none"


def test_profile_fingerprint_stable_and_sensitive():
    a = resolve_profile("ollama", model_id="m1")
    b = resolve_profile("ollama", model_id="m1")
    c = resolve_profile("ollama", model_id="m2")
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()

    # Route identity is a replay/cache boundary even when the wire settings
    # are otherwise equal.
    from dataclasses import replace

    assert a.fingerprint() != replace(a, id="ollama-reviewed").fingerprint()


def test_provider_registry_keeps_route_and_model_profiles_together():
    from athena.models.registry import ProviderRegistry

    class Provider:
        async def list_models(self):
            return []

        async def complete(self, request):
            if False:
                yield request

    registry = ProviderRegistry()
    registry.register("ollama", Provider())
    route = resolve_profile("ollama", model_id="qwen3:8b")
    model = ModelProfile(model_pattern="qwen3:8b", malformed_json_tendency=True)
    registry.set_profile("ollama", route)
    registry.set_model_profile("ollama", "qwen3:8b", model)
    assert registry.profile_for("ollama") is route
    assert registry.model_profile_for("ollama", "qwen3:8b") is model


def test_schema_fingerprint_changes_with_schema():
    s1 = {"type": "object", "properties": {"a": {"type": "string"}}}
    s2 = {"type": "object", "properties": {"a": {"type": "number"}}}
    assert schema_fingerprint(s1) != schema_fingerprint(s2)


# -- cache coordinator ---------------------------------------------------------


@pytest.mark.athena_scenario("COMPAT-005")
def test_prefix_tracker_stable_then_boundary():
    tracker = PrefixTracker()
    env = PromptEnvelope(stable_prefix=["system-prompt"], append_history=["t1"])
    r1 = tracker.observe(env, components={"system_prompt": "system-prompt"})
    assert r1["stable"] is True

    # Append-only history: prefix stays stable.
    env.append_history.append("r1")
    r2 = tracker.observe(env, components={"system_prompt": "system-prompt"})
    assert r2["stable"] is True

    # Change the prefix component -> explicit boundary with reason.
    env.stable_prefix[0] = "NEW system prompt"
    r3 = tracker.observe(env, components={"system_prompt": "NEW system prompt"})
    assert r3["stable"] is False
    assert r3["boundary"]["reason"] == "system_prompt_changed"


def test_prefix_tracker_marks_model_and_profile_changes_as_boundaries():
    tracker = PrefixTracker()
    env = PromptEnvelope(stable_prefix=["system"], append_history=[])
    tracker.observe(
        env,
        components={
            "model": "m1",
            "provider_profile": "profile-fingerprint-1",
        },
    )
    result = tracker.observe(
        env,
        components={
            "model": "m2",
            "provider_profile": "profile-fingerprint-2",
        },
    )
    assert result["stable"] is False
    assert result["boundary"]["reason"] == "model_changed"


@pytest.mark.athena_scenario("COMPAT-005")
def test_usage_record_openai_cached_tokens():
    u = UsageRecord.from_openai_compat(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    )
    d = u.to_dict()
    assert d["cache_read_tokens"] == 800
    assert d["uncached_prompt_tokens"] == 200
    assert d["cache_rate"] == 0.8


def test_usage_record_anthropic_cache_write():
    u = UsageRecord.from_anthropic(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
        }
    )
    d = u.to_dict()
    assert d["cache_read_tokens"] == 900
    assert d["cache_write_tokens"] == 50
    assert d["prompt_tokens"] == 1050


@pytest.mark.athena_scenario("COMPAT-005")
def test_no_cache_hit_inferred_without_telemetry():
    # Spec: never infer a hit from low cost/latency; unknown stays unknown.
    u = UsageRecord(prompt_tokens=500, completion_tokens=10)
    assert u.cache_read_tokens == 0
    assert u.cache_rate == 0.0


# -- replay fidelity -------------------------------------------------------------


def test_inference_receipt_preserves_provider_fields():
    r = InferenceReceipt(
        call_id="call_1",
        provider_profile_id="anthropic-hosted",
        model_id="claude-sonnet-4",
        response_id="resp_9",
        reasoning_signature="sig-abc",
        encrypted_reasoning=b"\x00binary",
        tool_ids=("toolu_1", "toolu_2"),
        continuation_token="tok-77",
    )
    d = r.to_dict()
    assert d["response_id"] == "resp_9"
    assert d["reasoning_signature"] == "sig-abc"
    assert d["tool_ids"] == ["toolu_1", "toolu_2"]
    assert d["continuation_token"] == "tok-77"
    assert d["has_encrypted_reasoning"] is True
