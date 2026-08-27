"""Effect-resolution routing: exec capabilities must not be misrouted to write."""

from __future__ import annotations

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import CapabilityDescriptor, EffectClass
from athena.protocol.policy import PolicyRequest, Principal
from athena.protocol.tasks import WorkspaceSpec


def test_exec_capabilities_resolve_to_execute_effects():
    desc = CapabilityDescriptor(
        id="terminal_session",
        description="",
        input_schema={},
        effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS,
                           EffectClass.WRITE_LOCAL, EffectClass.READ_LOCAL}),
    )
    effects = CapabilityDispatcher._resolve_effects(
        desc, {"operation": "create", "command": "bash"})
    assert EffectClass.EXECUTE in effects
    assert effects[0] in (EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS)


def test_policy_routes_terminal_session_to_execute_not_write():
    """Regression: 'create' op + WRITE_LOCAL effect used to demand a path."""
    eng = PolicyEngine()
    ws = WorkspaceSpec(id="w", root="/tmp")
    req = PolicyRequest(
        principal=Principal("agent", "athena"),
        task_id=None,
        capability_id="terminal_session",
        arguments={"operation": "create", "command": "bash --norc"},
        workspace=ws,
        effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS,
                           EffectClass.WRITE_LOCAL, EffectClass.READ_LOCAL}),
    )
    d = eng.evaluate(req)
    # Supervised profile -> ASK (approval), never the bogus path-write DENY.
    assert "missing resolved path" not in (d.reason or "")
    assert d.decision.value in ("ask", "allow")


def test_args_digest_ignores_call_id():
    from athena.policy.approvals import args_digest

    base = {"operation": "send", "session": "tty_1", "text": "x"}
    with_call = {**base, "call_id": "call_xyz"}
    assert args_digest(base) == args_digest(with_call)


def test_database_sql_shape_controls_query_effects():
    from athena.capabilities.environment import DatabaseCapability

    read = CapabilityDispatcher._resolve_effects_for(
        DatabaseCapability.descriptor,
        {"operation": "query", "sql": "SELECT * FROM records"},
    )
    mutation = CapabilityDispatcher._resolve_effects_for(
        DatabaseCapability.descriptor,
        {"operation": "query", "sql": "UPDATE records SET ok = 1"},
    )
    assert read == (EffectClass.READ_LOCAL,)
    assert set(mutation) == {EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}


def test_network_http_method_controls_network_authority():
    from athena.capabilities.environment import NetworkCapability

    read = CapabilityDispatcher._resolve_effects_for(
        NetworkCapability.descriptor,
        {"operation": "http", "method": "GET"},
    )
    mutation = CapabilityDispatcher._resolve_effects_for(
        NetworkCapability.descriptor,
        {"operation": "http", "method": "DELETE"},
    )
    assert read == (EffectClass.NETWORK_READ,)
    assert mutation == (EffectClass.NETWORK_WRITE,)


def test_reflection_workflow_and_skill_operations_have_effect_contracts():
    from athena.capabilities.reflection import CapabilityReflection

    descriptor = CapabilityReflection.descriptor
    for operation in ("workflows", "skills"):
        assert CapabilityDispatcher._resolve_effects_for(
            descriptor, {"operation": operation}
        ) == (EffectClass.READ_LOCAL,)


def test_reflection_runtime_permission_and_device_operations_have_contracts():
    from athena.capabilities.reflection import CapabilityReflection

    descriptor = CapabilityReflection.descriptor
    for operation in ("runtimes", "permissions", "devices"):
        assert CapabilityDispatcher._resolve_effects_for(
            descriptor, {"operation": operation}
        ) == (EffectClass.READ_LOCAL,)


def test_artifact_reads_and_service_mutations_have_exact_contracts():
    from athena.capabilities.artifacts import ArtifactCapability
    from athena.capabilities.environment import ServiceCapability

    assert CapabilityDispatcher._resolve_effects_for(
        ArtifactCapability.descriptor, {"operation": "slice"}
    ) == (EffectClass.READ_LOCAL,)
    for operation in ("start", "stop", "restart", "mask", "unmask"):
        effects = CapabilityDispatcher._resolve_effects_for(
            ServiceCapability.descriptor, {"operation": operation}
        )
        expected = {EffectClass.PRIVILEGED, EffectClass.EXECUTE,
                    EffectClass.SPAWN_PROCESS}
        if operation in ("mask", "unmask"):
            expected.add(EffectClass.WRITE_LOCAL)
        assert set(effects) == expected
