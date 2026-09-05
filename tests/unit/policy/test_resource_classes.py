"""Typed resource classes at the policy boundary (P1-23).

The pathless-write exception list is gone: policy asks whether a call
touches FILESYSTEM resources, from the descriptor's typed declaration —
not from a capability-name allow-list. Undeclared write-bearing
capabilities infer FILESYSTEM so the fail direction never loosens.
"""

from __future__ import annotations

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    EffectClass,
    ResourceClass,
)
from athena.protocol.policy import PolicyRequest, Principal
from athena.protocol.tasks import WorkspaceSpec


def _request(capability_id, resources, **kwargs) -> PolicyRequest:
    return PolicyRequest(
        principal=Principal("agent", "athena"),
        task_id=None,
        capability_id=capability_id,
        arguments=kwargs.pop("arguments", {"operation": "persist"}),
        workspace=WorkspaceSpec(id="w", root="/tmp/ws"),
        effects=frozenset({EffectClass.WRITE_LOCAL}),
        resources=resources,
        **kwargs,
    )


def test_pathless_write_with_filesystem_resource_is_denied():
    """A FILESYSTEM write without a resolved path is uncontainable."""
    decision = PolicyEngine().evaluate(_request("fs", frozenset({ResourceClass.FILESYSTEM})))
    assert decision.decision.value == "deny"
    assert "missing resolved path" in (decision.reason or "")


def test_pathless_write_on_state_resource_is_not_a_path_deny():
    """STATE/SCHEDULE/MEMORY resources are addressed by identity, not path."""
    for resources in (
        frozenset({ResourceClass.STATE}),
        frozenset({ResourceClass.SCHEDULE}),
        frozenset({ResourceClass.MEMORY}),
    ):
        decision = PolicyEngine().evaluate(_request("maintain", resources))
        assert "missing resolved path" not in (decision.reason or "")


def test_unresolved_resources_fall_back_to_filesystem_deny():
    """Direct engine callers with no resource typing fail closed: a write
    bearing call is assumed FILESYSTEM until a descriptor declares it."""
    decision = PolicyEngine().evaluate(_request("opaque", frozenset()))
    assert decision.decision.value == "deny"
    assert "missing resolved path" in (decision.reason or "")


def test_descriptor_inference_defaults_to_filesystem():
    """A write-bearing descriptor that declares nothing infers FILESYSTEM."""
    desc = CapabilityDescriptor(
        id="third_party_tool",
        description="",
        input_schema={},
        effects=frozenset({EffectClass.WRITE_LOCAL}),
    )
    assert desc.resolve_resources() == frozenset({ResourceClass.FILESYSTEM})


def test_declared_resources_win_over_inference():
    desc = CapabilityDescriptor(
        id="third_party_store",
        description="",
        input_schema={},
        effects=frozenset({EffectClass.WRITE_LOCAL}),
        resources=frozenset({ResourceClass.STATE}),
    )
    assert desc.resolve_resources() == frozenset({ResourceClass.STATE})


async def test_dispatcher_threads_descriptor_resources_into_policy_request(tmp_path):
    """The canonical dispatch path resolves resources from the descriptor."""
    import pytest

    from athena.capabilities.dispatcher import CapabilityDispatcher
    from athena.capabilities.registry import CapabilityRegistry
    from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus

    captured = {}

    class _CapturePolicy(PolicyEngine):
        def evaluate(self, request, autonomy=None):
            captured["resources"] = request.resources
            return super().evaluate(request, autonomy=autonomy)

    class _StoreCap:
        descriptor = CapabilityDescriptor(
            id="third_party_store",
            description="write task-scoped state by identity",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.WRITE_LOCAL}),
            resources=frozenset({ResourceClass.STATE}),
        )

        async def invoke(self, request, **kwargs):
            from athena.capabilities.dispatcher import CapabilityResult

            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output="ok",
            )

    registry = CapabilityRegistry()
    registry.register(_StoreCap())
    dispatcher = CapabilityDispatcher(registry, _CapturePolicy())

    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="third_party_store",
            task_id="t-1",
            call_id="c-1",
            arguments={"operation": "persist"},
        ),
        workspace=WorkspaceSpec(id="w", root=str(tmp_path)),
        profile="coding",
    )
    assert result.status is CapabilityResultStatus.OK
    assert captured["resources"] == frozenset({ResourceClass.STATE})


def test_native_descriptors_declare_expected_resource_classes():
    """The formerly-magic pathless capabilities declare typed resources."""
    from athena.capabilities.maintain import MaintenanceCapability
    from athena.capabilities.memory import MemoryCapability
    from athena.capabilities.research import ResearchCapability
    from athena.capabilities.schedule import ScheduleCapability
    from athena.capabilities.synthesis import SynthesisCapability
    from athena.capabilities.workflow import WorkflowCapability

    assert MemoryCapability.descriptor.resolve_resources() == frozenset(
        {ResourceClass.MEMORY}
    )
    assert MaintenanceCapability.descriptor.resolve_resources() == frozenset(
        {ResourceClass.STATE}
    )
    assert ScheduleCapability.descriptor.resolve_resources() == frozenset(
        {ResourceClass.SCHEDULE}
    )
    assert ResearchCapability.descriptor.resolve_resources() == frozenset(
        {ResourceClass.RESEARCH}
    )
    # workflow/synthesis write workflow/synthesis STATE, not workspace files.
    assert WorkflowCapability.descriptor.resolve_resources() == frozenset(
        {ResourceClass.WORKFLOW}
    )
    assert SynthesisCapability.descriptor.resolve_resources() == frozenset(
        {ResourceClass.SYNTHESIS}
    )
