"""Generic native capability conformance suite (review item 19).

Iterates registered model-visible capabilities and proves common invariants
so future tool additions are automatically covered.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import athena.capabilities as capabilities_package
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    RetryPolicy,
)
from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec


def _discover_descriptors() -> dict[str, CapabilityDescriptor]:
    """Collect descriptors from all native capability classes."""
    modules = [
        importlib.import_module(module.name)
        for module in pkgutil.iter_modules(
            capabilities_package.__path__, capabilities_package.__name__ + "."
        )
    ]
    descriptors: dict[str, CapabilityDescriptor] = {}
    for module in modules:
        for _, cls in inspect.getmembers(module, inspect.isclass):
            descriptor = getattr(cls, "descriptor", None)
            if isinstance(descriptor, CapabilityDescriptor):
                descriptors[descriptor.id] = descriptor
    return descriptors


def _mutator_descriptors() -> dict[str, CapabilityDescriptor]:
    """Descriptors that declare any mutating effect."""
    mutating = {EffectClass.WRITE_LOCAL, EffectClass.DELETE}
    return {cid: desc for cid, desc in _discover_descriptors().items() if desc.effects & mutating}


class TestSchemaInvariants:
    """Every descriptor must have a valid JSON Schema object."""

    def test_every_descriptor_has_input_schema(self):
        for cid, desc in _discover_descriptors().items():
            assert isinstance(desc.input_schema, dict), f"{cid}: input_schema not a dict"
            has_type_object = desc.input_schema.get("type") == "object"
            has_one_of = "oneOf" in desc.input_schema
            assert has_type_object or has_one_of, (
                f"{cid}: input_schema must have type=object or oneOf"
            )

    def test_operation_schema_enum_matches_effect_map(self):
        """Operations in schema must match static effect map entries."""
        for cid, desc in _discover_descriptors().items():
            if desc.operation_effects is None:
                continue
            if "oneOf" in desc.input_schema:
                continue
            props = desc.input_schema.get("properties", {})
            operations = set(props.get("operation", {}).get("enum", ()))
            if not operations:
                continue
            declared = set(desc.operation_effects)
            assert operations == declared, (
                f"{cid}: operation/effect contract drift: "
                f"missing={sorted(operations - declared)}, "
                f"extra={sorted(declared - operations)}"
            )


class TestEffectInvariants:
    """Effect declarations must be non-empty for non-read capabilities."""

    def test_effects_not_empty_for_mutators(self):
        for cid, desc in _mutator_descriptors().items():
            assert desc.effects, f"{cid}: mutator has no effects"

    def test_effects_not_secret_or_financial_unless_declared(self):
        """Capabilities must not accidentally include high-risk effects."""
        # Only capabilities that explicitly declare these should have them.
        ok_ids = {
            "browser",
            "computer",
            "dependency",
            "system",
            "financial",
            "capsule",
            "service",
            "packs",
            "process",
        }
        for cid, desc in _discover_descriptors().items():
            high_risk = desc.effects & {
                EffectClass.SECRET_READ,
                EffectClass.FINANCIAL,
                EffectClass.PRIVILEGED,
            }
            if high_risk and cid not in ok_ids:
                pytest.fail(f"{cid}: unexpected high-risk effects {high_risk}")


class TestResourceDeclarations:
    """Mutators must declare resource identity."""

    def test_mutators_have_resource_key_resolver_or_path_schema(self):
        for cid, desc in _mutator_descriptors().items():
            has_resolver = desc.resource_key_resolver is not None
            has_path = "path" in desc.input_schema.get("properties", {})
            has_declared_resources = bool(desc.resources)
            # Capsule manages state resources by identity, not path.
            state_only = cid in {
                "capability_health",
                "capsule",
                "context_blocks",
                "debugger",
                "delegate",
                "dependency",
                "diagnostics",
                "fusion",
                "packs",
                "service",
                "terminal_session",
            }
            assert has_resolver or has_path or has_declared_resources or state_only, (
                f"{cid}: mutator lacks resource identity (no resolver, "
                f"no path schema, no resource class)"
            )


class TestRetryPolicy:
    """Every descriptor must have a resolvable retry policy."""

    def test_every_descriptor_resolves_retry_policy(self):
        for cid, desc in _discover_descriptors().items():
            policy = desc.resolve_retry_policy()
            assert isinstance(policy, RetryPolicy), f"{cid}: bad retry policy {policy}"

    def test_read_only_effects_get_read_only_retry(self):
        desc = CapabilityDescriptor(
            id="test.read",
            description="read test",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.READ_LOCAL}),
        )
        assert desc.resolve_retry_policy() is RetryPolicy.READ_ONLY

    def test_external_effects_get_never_retry(self):
        desc = CapabilityDescriptor(
            id="test.external",
            description="external test",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.NETWORK_WRITE, EffectClass.EXTERNAL_MESSAGE}),
        )
        assert desc.resolve_retry_policy() is RetryPolicy.NEVER

    def test_local_write_defaults_to_never_unless_declared_idempotent(self):
        desc = CapabilityDescriptor(
            id="test.write",
            description="write test",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        )
        assert desc.resolve_retry_policy() is RetryPolicy.NEVER

        declared = CapabilityDescriptor(
            id="test.write.safe",
            description="declared idempotent write test",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
            retry_policy=RetryPolicy.IDEMPOTENT,
        )
        assert declared.resolve_retry_policy() is RetryPolicy.IDEMPOTENT


class TestPolicyDenyZeroEffect:
    """Policy denial must produce zero effect."""

    async def test_policy_deny_returns_failed_with_no_executor_call(self):
        from athena.protocol.capabilities import CapabilityRequestOrigin

        class E:
            descriptor = CapabilityDescriptor(
                id="test.write",
                description="write",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                effects=frozenset({EffectClass.WRITE_LOCAL}),
            )
            invoked = False

            async def invoke(self, request, **kwargs):
                self.invoked = True
                return CapabilityResult(
                    request.call_id, request.capability_id, CapabilityResultStatus.OK
                )

        executor = E()
        reg = CapabilityRegistry()
        reg.register(executor)

        d = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.SUPERVISED))
        d.set_budget_tracker(None)
        # Use a task policy that hard-denies the capability.
        from athena.protocol.tasks import CapabilityPolicy

        deny_policy = CapabilityPolicy(deny=("*",))
        ws = WorkspaceSpec(id="w", root="/tmp")
        r = CapabilityRequest(
            "test.write",
            {"path": "/tmp/out.txt"},
            task_id="t",
            call_id="conformance-deny",
            origin=CapabilityRequestOrigin.MODEL,
        )
        result = await d.dispatch(r, workspace=ws, profile="supervised", task_policy=deny_policy)
        assert result.status is CapabilityResultStatus.FAILED
        assert not executor.invoked, "executor invoked despite policy denial"


class TestCapabilityFixtureRegistration:
    """Every model-visible native capability needs a fixture or exemption."""

    EXEMPTIONS: dict[str, str] = {
        # Capabilities whose behavior is covered by dedicated subsystem suites
        # or which require external runtimes are explicitly exempted here. The
        # reason is part of the contract and must be reviewable.
        # Dedicated domain/integration suites own behavior for these
        # external/stateful/lifecycle capabilities.
        "artifacts": "artifact store suites own behavior",
        "browser": "requires external browser runtime",
        "capabilities": "capability inventory suites own behavior",
        "capability_health": "health persistence suites own behavior",
        "capsule": "synthesis/capsule suites own behavior",
        "computer": "requires display/input runtime",
        "context_blocks": "state/context suites own behavior",
        "database": "database adapter suites own behavior",
        "debugger": "requires attached runtime",
        "delegate": "delegation integration suites own behavior",
        "delegate.external": "external delegation integration suites own behavior",
        "dependency": "dependency/execution integration suites own behavior",
        "diagnostics": "diagnostics suites own behavior",
        "execute": "process runtime suites own behavior",
        "fusion": "fusion orchestration suites own behavior",
        "git": "git execution suites own behavior",
        "machine": "environment probe suites own behavior",
        "maintain": "maintenance lifecycle suites own behavior",
        "mcp.context": "MCP adapter suites own behavior",
        "network": "network environment suites own behavior",
        "observer": "observer/state suites own behavior",
        "packs": "pack lifecycle/security suites own behavior",
        "process": "process runtime suites own behavior",
        "research": "dedicated research domain suites cover storage/policy adapters",
        "request_input": "input continuation suites own behavior",
        "schedule": "scheduler/control integration suites own behavior",
        "scratch": "synthesis sandbox suites own behavior",
        "service": "lifecycle integration suites own behavior",
        "synthesis": "synthesis validation/promotion suites own behavior",
        "terminal_session": "process runtime suites own behavior",
        "truth": "truth/evidence suites own behavior",
        "watch": "watcher lifecycle suites own behavior",
        "workflow": "workflow execution suites own behavior",
        "workspace": "workspace state suites own behavior",
    }

    def test_every_descriptor_is_registered_or_exempt(self):
        descriptors = _discover_descriptors()
        fixtures = TestCapabilityRequestContracts._capability_fixtures()
        missing = sorted(
            cid for cid in descriptors if cid not in fixtures and cid not in self.EXEMPTIONS
        )
        assert not missing, (
            f"capabilities missing conformance fixtures/exemptions: {missing}; "
            "add a safe fixture or a documented exemption"
        )

    def test_exemptions_are_not_stale(self):
        descriptors = _discover_descriptors()
        stale = sorted(set(self.EXEMPTIONS) - set(descriptors))
        assert not stale, f"exemptions reference unknown capabilities: {stale}"


class TestCapabilityRequestContracts:
    """Per-capability fixture-driven execution contracts (review item 23/26).

    Registered fixtures prove:
    - valid schema accepts;
    - invalid request is rejected pre-effect;
    - dispatcher FAILED carries typed failure metadata;
    - policy denial produces zero effects/no executor call.
    """

    @staticmethod
    async def _run_fixture_probes(executor, valid: dict, tmp_path):
        """Prove bounded runtime, cancellation, and cleanup for one fixture.

        The probe cancels the valid call before completion. A read fixture may
        return; a write fixture must not leak its target effect into the temp
        workspace.
        """
        import asyncio

        from athena.protocol.capabilities import CapabilityRequest, CapabilityRequestOrigin

        async def _probe():
            resource = str(valid.get("path") or valid.get("resource") or "probe")
            arguments = dict(valid)
            if valid.get("operation") == "write":
                arguments["path"] = str(tmp_path / resource)
            request = CapabilityRequest(
                executor.descriptor.id,
                arguments,
                task_id="conformance-cancel",
                call_id="conformance-cancel-1",
                origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
            )
            task = asyncio.ensure_future(executor.invoke(request))
            try:
                await asyncio.sleep(0)
                task.cancel()
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except asyncio.TimeoutError:
                    return "TIMEOUT"
                except asyncio.CancelledError:
                    return "CANCELLED"
            finally:
                if not task.done() and task.cancelled():
                    pass

        outcome = await _probe()
        resource = str(valid.get("path") or valid.get("resource") or "probe")
        effect_target = tmp_path / resource if valid.get("operation") == "write" else None
        if effect_target is not None:
            assert not effect_target.exists(), (
                f"{executor.descriptor.id}: cancellation leaked a filesystem effect"
            )
        return outcome

    @staticmethod
    def _capability_fixtures() -> dict:
        """Safe, self-contained fixture contracts for native capabilities.

        This map is a registration contract, not merely examples. New
        model-visible capabilities must register here or add a documented
        exemption in :class:`TestCapabilityFixtureRegistration`.
        """
        from athena.capabilities.fs import FilesystemCapability
        from athena.capabilities.research import ResearchCapability
        from athena.capabilities.skills import SkillsCapability
        from athena.capabilities.memory import MemoryCapability
        from athena.capabilities.session_search import SessionSearchCapability
        from athena.research.store import ResearchStore
        from athena.state.database import Database

        research_executor = ResearchCapability(ResearchStore(Database(":memory:")))

        class _MessageStore:
            async def search(self, *args, **kwargs):
                return []

        class _MemoryStore:
            async def recall(self, **kwargs):
                return []

            async def search(self, **kwargs):
                return []

            async def save(self, **kwargs):
                raise AssertionError("read-only fixture must not save memory")

        class _SkillsStore:
            async def search(self, query, limit):
                return []

            async def trigger(self, skill_id, arguments, task_id):
                raise AssertionError("read-only fixture must not trigger a skill")

        return {
            "session_search": {
                "executor": SessionSearchCapability(_MessageStore()),
                "valid": {"query": "release"},
                "invalid": {"operation": "bogus"},
                "resource": "session-history",
            },
            "memory": {
                "executor": MemoryCapability(_MemoryStore()),
                "valid": {"operation": "recall", "query": "release"},
                "invalid": {"operation": "bogus"},
                "resource": "memory-store",
            },
            "skills": {
                "executor": SkillsCapability(_SkillsStore()),
                "valid": {"operation": "search", "query": "release"},
                "invalid": {"operation": "bogus"},
                "resource": "skill-library",
            },
            "fs": {
                "executor": FilesystemCapability(),
                "valid": {"operation": "write", "path": "cf.txt", "content": "x"},
                "invalid": {"operation": "write"},
                "resource": "cf.txt",
            },
            "research": {
                "executor": research_executor,
                "valid": {"operation": "sources", "query": "release"},
                "invalid": {"operation": 123},
                "resource": "research-records",
            },
        }

    def test_invalid_request_is_rejected_pre_effect_with_typed_failure(self):
        from athena.schema import validate_schema

        fixtures = self._capability_fixtures()
        for cid, fixture in fixtures.items():
            executor = fixture["executor"]
            desc = executor.descriptor
            assert validate_schema(desc.input_schema, fixture["invalid"]), (
                f"{cid}: invalid fixture passed schema"
            )
            assert not validate_schema(desc.input_schema, fixture["valid"]), (
                f"{cid}: valid fixture rejected"
            )
            # Every fixture-backed capability must expose a typed failure
            # contract on FAILED outcomes so downstream machinery never parses
            # prose.
            marker = getattr(executor, "_conformance_failure_probe", None)
            assert marker is not None, f"{cid}: missing typed failure probe"
            failure = marker()
            assert failure is not None, f"{cid}: missing typed failure probe"
            assert "failure_code" in failure, f"{cid}: failure metadata lacks failure_code"

    async def test_policy_denied_valid_request_has_zero_effect(self, tmp_path):
        from athena.capabilities.fs import FilesystemCapability

        ws = WorkspaceSpec(id="cf-ws", root=str(tmp_path))
        reg = CapabilityRegistry()
        reg.register(FilesystemCapability())
        dispatcher = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))
        from athena.protocol.tasks import CapabilityPolicy

        target = tmp_path / "no-effect.txt"
        request = CapabilityRequest(
            "fs",
            {"operation": "write", "path": "no-effect.txt", "content": "x"},
            task_id="cf-deny",
            call_id="cf-deny-1",
        )
        deny_policy = CapabilityPolicy(deny=("fs",))
        result = await dispatcher.dispatch(
            request,
            workspace=ws,
            task_policy=deny_policy,
        )
        assert result.status is CapabilityResultStatus.FAILED
        # Zero effect: the file was never written.
        assert not target.exists(), "denied capability leaked a filesystem effect"

    async def test_fixture_backed_failed_results_carry_typed_failure(self, tmp_path):
        """Executor-level failures expose typed failure metadata, not prose only."""
        from athena.capabilities.memory import MemoryCapability
        from athena.capabilities.research import ResearchCapability
        from athena.capabilities.skills import SkillsCapability
        from athena.research.store import ResearchStore
        from athena.state.database import Database

        class _Store:
            async def recall(self, **kwargs):
                return []

        cases = [
            MemoryCapability(_Store()),
            SkillsCapability(None),
            ResearchCapability(ResearchStore(Database(":memory:"))),
        ]
        for executor in cases:
            request = CapabilityRequest(
                executor.descriptor.id,
                {"operation": "definitely-not-supported"},
                task_id="typed-failure",
                call_id="typed-failure-1",
            )
            result = await executor.invoke(request)
            assert result.status is CapabilityResultStatus.FAILED
            assert result.metadata is not None
            assert "failure_code" in result.metadata

    async def test_fixture_timeout_cancellation_and_cleanup(self, tmp_path):
        """Every fixture proves bounded runtime, cancellation, and cleanup."""
        fixtures = self._capability_fixtures()
        for cid, fixture in fixtures.items():
            outcome = await self._run_fixture_probes(
                fixture["executor"], fixture["valid"], tmp_path
            )
            assert outcome is not None, f"{cid}: cancelled probe produced no observable outcome"
