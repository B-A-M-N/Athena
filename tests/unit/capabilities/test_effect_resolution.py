from __future__ import annotations

import pytest

from athena.capabilities.operations import CapabilityEffectError, resolve_operation_effects
from athena.protocol.capabilities import CapabilityDescriptor, EffectClass


def _descriptor(resolver):
    return CapabilityDescriptor(
        id="fixture.effects",
        description="fixture",
        input_schema={"type": "object"},
        effects=frozenset({EffectClass.READ_LOCAL}),
        effect_resolver=resolver,
    )


def test_dynamic_effects_cannot_escape_declared_envelope():
    descriptor = _descriptor(lambda _args: frozenset({EffectClass.READ_LOCAL}))
    assert resolve_operation_effects(descriptor, {}) == (EffectClass.READ_LOCAL,)

    widening = _descriptor(lambda _args: frozenset({EffectClass.NETWORK_WRITE}))
    with pytest.raises(CapabilityEffectError, match="exceed the descriptor envelope"):
        resolve_operation_effects(widening, {})


def test_effect_resolver_failure_is_rejected_without_heuristic_fallback():
    descriptor = _descriptor(lambda _args: (_ for _ in ()).throw(RuntimeError("resolver failed")))
    with pytest.raises(CapabilityEffectError, match="resolver failed"):
        resolve_operation_effects(descriptor, {})
