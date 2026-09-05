"""Model quality floor (P1-16).

Tier declarations are advisory routing metadata: a policy floor excludes
only models that DECLARE a lower tier, undeclared models survive every
floor, and a run that keeps producing malformed tool calls escalates the
floor one tier for its remaining turns.
"""

from __future__ import annotations

from dataclasses import replace

from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.kernel.kernel import _escalated_quality_floor
from athena.protocol.models import ModelInfo, ModelQualityTier
from athena.protocol.tasks import ModelPolicy


def _info(model_id: str, tier: str | None, provider: str = "prov") -> ModelInfo:
    return ModelInfo(
        id=model_id,
        provider=provider,
        quality_tier=tier if tier is not None else ModelQualityTier.UNDECLARED,
    )


class _StaticRegistry:
    def __init__(self, models: list[ModelInfo]) -> None:
        self._models = models

    async def list_models(self):
        return list(self._models)

    def provider_for(self, name):
        return None

    def readiness(self):
        return {}


class TestFloorFilter:
    async def test_floor_excludes_lower_declared_tiers(self):
        router = ModelRouter(
            _StaticRegistry([_info("cheap", "economy"), _info("strong", "frontier")])
        )
        policy = ModelPolicy(min_quality_tier="standard")
        sel = await router.select(policy=policy)
        assert sel.model == "strong"

    async def test_undeclared_survives_any_floor(self):
        """A model that never declared a tier is not excluded by one."""
        router = ModelRouter(
            _StaticRegistry([_info("mystery", None), _info("cheap", "economy")])
        )
        policy = ModelPolicy(min_quality_tier="frontier")
        sel = await router.select(policy=policy)
        # The declared-economy model is excluded; the undeclared one survives.
        assert sel.model == "mystery"

    async def test_no_floor_routes_as_before(self):
        router = ModelRouter(
            _StaticRegistry([_info("cheap", "economy"), _info("strong", "frontier")])
        )
        sel = await router.select(policy=ModelPolicy())
        assert sel.model == "cheap"  # balanced: same rank, cost tie-break

    async def test_invalid_floor_is_ignored(self):
        router = ModelRouter(_StaticRegistry([_info("cheap", "economy")]))
        sel = await router.select(policy=ModelPolicy(min_quality_tier="mega"))
        assert sel.model == "cheap"


class TestTierParsing:
    def test_string_tier_coerces(self):
        assert _info("m", "economy").quality_tier is ModelQualityTier.ECONOMY

    def test_invalid_string_becomes_undeclared(self):
        assert _info("m", "ultra").quality_tier is ModelQualityTier.UNDECLARED


class TestEscalation:
    def _state(self, corrections: int):
        from types import SimpleNamespace

        return SimpleNamespace(
            tool_correction_counts={"fs": corrections},
        )

    def test_below_threshold_no_escalation(self):
        policy = ModelPolicy()
        assert _escalated_quality_floor(policy, self._state(1)) is policy

    def test_repeated_corrections_raise_floor(self):
        policy = ModelPolicy()
        raised = _escalated_quality_floor(policy, self._state(4))
        assert raised is not policy
        assert raised.min_quality_tier is not None
        assert ModelQualityTier(raised.min_quality_tier).rank >= ModelQualityTier.STANDARD.rank

    def test_higher_corrections_escalate_further(self):
        policy = ModelPolicy()
        mild = _escalated_quality_floor(policy, self._state(2))
        strong = _escalated_quality_floor(policy, self._state(8))
        assert ModelQualityTier(strong.min_quality_tier).rank >= ModelQualityTier(
            mild.min_quality_tier
        ).rank

    def test_escalation_cap_at_frontier(self):
        policy = ModelPolicy()
        raised = _escalated_quality_floor(policy, self._state(100))
        assert ModelQualityTier(raised.min_quality_tier) is ModelQualityTier.FRONTIER

    def test_existing_floor_only_ratchets_up(self):
        policy = ModelPolicy(min_quality_tier="frontier")
        assert _escalated_quality_floor(policy, self._state(4)) is policy

    def test_task_policy_object_never_mutated(self):
        policy = ModelPolicy()
        _escalated_quality_floor(policy, self._state(8))
        assert policy.min_quality_tier is None

    def test_no_state_no_escalation(self):
        policy = ModelPolicy()
        assert _escalated_quality_floor(policy, None) is policy
