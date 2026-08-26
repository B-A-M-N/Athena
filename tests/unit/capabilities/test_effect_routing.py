"""Effect-resolution routing: exec capabilities must not be misrouted to write."""

from __future__ import annotations

import pytest

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
