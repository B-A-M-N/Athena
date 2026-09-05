"""Generated-capability discipline, adversarial cases (P2-3).

The authority layer is the contract under test: declared effects can
never exceed the sandbox contract, adversarial schemas cannot weaken
validation, and hostile generated sources fail inside the sandbox rather
than at the host. These are deterministic negative cases — the property
being pinned is that every attempt fails closed.
"""

from __future__ import annotations

import json

import pytest

from athena.capabilities.registry import _compile_validator, validate_schema as _validate_schema
from athena.synthesis.engine import _GENERATED_EFFECTIVE_AUTHORITY, SyntheticCapability


# ---------------------------------------------------------------- #
# Adversarial input schemas: an attacker-controlled schema must not
# weaken validation below what the host asked for.
# ---------------------------------------------------------------- #


class TestAdversarialSchemas:
    def test_billion_laughs_expansion_is_not_amplified(self):
        """Deeply nested anyOf fails admission instead of RecursionError
        at validation time (P2-3: bounded schema depth)."""
        schema = {"type": "object"}
        node: dict = {"type": "string"}
        for _ in range(200):
            node = {"anyOf": [node, {"type": "null"}]}
        schema["properties"] = {"x": node}
        with pytest.raises(ValueError, match="nesting depth"):
            _compile_validator(schema)
        # A sane depth still compiles.
        ok = {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}]}}}
        assert _compile_validator(ok).is_valid({"x": "s"}) is True

    def test_numeric_bounds_survive_alias_translation(self):
        """allow_extra alias translation must not drop constraint keys."""
        schema = {
            "type": "object",
            "allow_extra": False,
            "properties": {"n": {"type": "integer", "minimum": 0, "maximum": 10}},
        }
        assert _validate_schema(schema, {"n": 11}) != []
        assert _validate_schema(schema, {"n": -1}) != []
        assert _validate_schema(schema, {"n": 5}) == []
        assert _validate_schema(schema, {"n": 5, "extra": 1}) != []

    def test_invalid_schema_is_rejected_not_accepted(self):
        """A schema that cannot compile must raise, never validate-all."""
        with pytest.raises(Exception):
            _compile_validator({"type": "not-a-real-type"})

    def test_non_object_schema_payloads_rejected(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        for hostile in ([], "str", 3, None):
            assert _validate_schema(schema, hostile) != [], hostile


# ---------------------------------------------------------------- #
# Effect authority: the sandbox contract, not the declaration, is the
# ceiling on what a generated capability can govern.
# ---------------------------------------------------------------- #


class TestEffectAuthority:
    def test_effective_authority_is_fixed_and_narrow(self):
        assert _GENERATED_EFFECTIVE_AUTHORITY == frozenset({"READ_LOCAL", "EXECUTE"})

    def test_default_effective_effects_come_from_the_contract(self):
        cap = _cap(effects=frozenset({"NETWORK_WRITE", "PRIVILEGED"}))
        # Declared effects are honored as an ENVELOPE for inheritance
        # checks, but effective authority — what the sandbox contract
        # reports to the host — never widens past the fixed floor.
        assert cap.effective_effects == _GENERATED_EFFECTIVE_AUTHORITY

    def test_hostile_declaration_cannot_widen_effective_authority(self):
        for hostile in ("DELETE", "SECRET_READ", "FINANCIAL", "EXTERNAL_PUBLISH"):
            cap = _cap(effects=frozenset({hostile}))
            assert set(cap.effective_effects) <= set(_GENERATED_EFFECTIVE_AUTHORITY)


def _cap(effects) -> SyntheticCapability:
    return SyntheticCapability(
        id="gen_hostile",
        name="hostile",
        description="attempts to declare wide effects",
        code="def run(args):\n    return {'ok': True}\n",
        input_schema={"type": "object"},
        effects=frozenset(effects),
        task_id="t1",
        provenance={"task_id": "t1", "call_id": "c1"},
        validation={},
    )


# ---------------------------------------------------------------- #
# Hostile generated sources: the sandbox boundary is the child
# process; everything else is contained by the run(args) contract.
# ---------------------------------------------------------------- #


class TestGeneratedSourceContract:
    def test_missing_run_function_fails_in_child(self):
        from athena.synthesis.engine import _child_code

        code = "def not_run(args):\n    return {}\n"
        program = _child_code(repr(code))
        # The child exec's the source into a fresh namespace and then looks
        # up NS['run'] — a missing run must surface as an error, not as an
        # empty success.
        assert "NS['run']" in program or 'NS["run"]' in program
        NS: dict = {}
        exec(code, NS)  # noqa: S102 - mirrors the child contract
        with pytest.raises(KeyError):
            NS["run"]

    def test_host_call_escalation_attempt_is_framed_not_direct(self):
        """A generated source cannot reach a dispatcher; only the framed
        __HOST__ protocol exists, and the host re-checks authority."""
        from athena.synthesis.engine import _child_code

        program = _child_code(
            repr("def run(args):\n    return athena.call('fs', {'operation': 'read'})\n")
        )
        # The only host surface is the framed IPC protocol; no dispatcher,
        # registry, or store object is in the child namespace.
        for banned in ("dispatcher", "registry", "task_store", "message_store"):
            assert banned not in program

    def test_output_is_json_serializable_envelope(self):
        """Results cross the process boundary as JSON — a source returning
        unserializable objects fails inside the CHILD process, never at
        the host. The persistent child template surfaces that via
        __ERROR__; the single-shot child dies with the process (contained)."""
        from athena.synthesis.engine import _child_code

        code = "def run(args):\n    return {'obj': object()}\n"
        persistent_program = _child_code(repr(code), persistent=True)
        assert "__ERROR__" in persistent_program
        # Simulate the child's serialization step: the failure is local.
        NS: dict = {}
        exec(code, NS)  # noqa: S102
        with pytest.raises(TypeError):
            json.dumps(NS["run"]({}))

    async def test_host_call_guards_hold_under_hostile_inputs(self):
        """The mediated host API refuses malformed, undeclared, cyclic,
        and over-budget calls before anything reaches the dispatcher."""
        import asyncio

        from athena.synthesis.runtime import GeneratedHostError, GeneratedToolHost

        host = GeneratedToolHost(
            dispatcher=None,  # never reached: guards fire first
            workspace=None,
            task_id="t1",
            allowed_capabilities=frozenset({"fs"}),
            call_chain=("fs",),  # 'fs' is already in the chain -> cycle
        )
        with pytest.raises(GeneratedHostError, match="cycle"):
            await asyncio.wait_for(host.call("fs", {}), timeout=5)
        with pytest.raises(GeneratedHostError, match="non-empty"):
            await asyncio.wait_for(host.call("   ", {}), timeout=5)
        with pytest.raises(GeneratedHostError, match="object"):
            await asyncio.wait_for(host.call("fs", "not-a-dict"), timeout=5)
        with pytest.raises(GeneratedHostError, match="not declared"):
            await asyncio.wait_for(host.call("execute", {}), timeout=5)
