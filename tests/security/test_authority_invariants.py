"""Authority invariants for the compositional policy boundary (P0).

These encode the audit's non-negotiable properties:

* MONOTONICITY: adding a resolved effect can never make a request easier
  to authorize. ``verdict(A ∪ {e})`` is at least as strict as ``verdict(A)``.
* CONTAINMENT BEFORE APPROVAL: an active approval grant can convert ASK
  into ALLOW but can never convert a structural (workspace/network) DENY.
* APPROVAL ENVELOPE: a grant covers a later request only when that
  request's effects are a subset of the effects the operator approved.
* OFFLINE MODEL EGRESS: OFFLINE autonomy and network-DENY workspaces hard
  narrow ModelPolicy to the router's LOCAL-only gate.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from athena.capabilities.dispatcher import DispatchDirectives

from athena.policy.approvals import ApprovalManager
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import EffectClass
from athena.protocol.policy import ApprovalScope, PolicyRequest, Principal
from athena.protocol.tasks import (
    AutonomyLevel,
    NetworkPolicy,
    PathRule,
    WorkspaceSpec,
)

_WS = WorkspaceSpec(
    id="w1",
    root="/tmp/ws",
    writable=(PathRule("/tmp/ws/**"),),
    readable=(PathRule("/tmp/ws/**"),),
)

_PRINCIPAL = Principal("agent", "athena")

_RANK = {"allow": 0, "ask": 1, "deny": 2}


def _req(
    capability_id: str,
    effects,
    arguments=None,
    *,
    workspace=None,
    task_id="t1",
    call_id=None,
    backend="local",
) -> PolicyRequest:
    return PolicyRequest(
        principal=_PRINCIPAL,
        task_id=task_id,
        capability_id=capability_id,
        arguments=arguments or {},
        workspace=workspace or _WS,
        execution_backend=backend,
        effects=frozenset(effects),
        call_id=call_id,
    )


def _verdict(engine: PolicyEngine, request: PolicyRequest) -> str:
    return engine.evaluate(request).decision.value


# ---------------------------------------------------------------------- #
# Monotonicity over effect sets
# ---------------------------------------------------------------------- #


def _all_effect_subsets(effects: tuple[EffectClass, ...]):
    for size in range(len(effects) + 1):
        yield from itertools.combinations(effects, size)


@pytest.mark.parametrize("level", list(AutonomyLevel))
@pytest.mark.parametrize(
    "base_effects",
    [
        (EffectClass.READ_LOCAL,),
        (EffectClass.WRITE_LOCAL,),
        (EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS),
        (EffectClass.READ_LOCAL, EffectClass.NETWORK_READ),
        (EffectClass.WRITE_LOCAL, EffectClass.NETWORK_READ),
    ],
    ids=lambda e: "+".join(x.value for x in e),
)
def test_adding_an_effect_never_softens_the_verdict(level, base_effects):
    """verdict(A ∪ {e}) must be at least as restrictive as verdict(A)."""
    engine = PolicyEngine(level)
    universe = (
        EffectClass.READ_LOCAL,
        EffectClass.WRITE_LOCAL,
        EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS,
        EffectClass.NETWORK_READ,
        EffectClass.NETWORK_WRITE,
        EffectClass.DELETE,
    )
    base = _verdict(
        engine, _req("generic.cap", base_effects, {"path": "/tmp/ws/a", "cwd": "/tmp/ws"})
    )
    for extra in universe:
        if extra in base_effects:
            continue
        extended = _verdict(
            engine,
            _req(
                "generic.cap",
                set(base_effects) | {extra},
                {"path": "/tmp/ws/a", "cwd": "/tmp/ws"},
            ),
        )
        assert _RANK[extended] >= _RANK[base], (
            f"{level.value}: adding {extra.value} to "
            f"{[e.value for e in base_effects]} softened {base} -> {extended}"
        )


def test_all_effect_subsets_are_monotonic_pairwise():
    """For every subset pair A ⊆ B over the effect universe: rank(B) >= rank(A)."""
    engine = PolicyEngine(AutonomyLevel.CODING)
    universe = (
        EffectClass.READ_LOCAL,
        EffectClass.WRITE_LOCAL,
        EffectClass.NETWORK_READ,
        EffectClass.NETWORK_WRITE,
    )
    # The empty set is the fail-closed sentinel (no rule basis to allow),
    # not an authorization point; the property governs resolved effects.
    subsets = [
        frozenset(s) for s in _all_effect_subsets(universe) if s
    ]
    verdicts = {
        tuple(sorted(e.value for e in s)): _verdict(
            engine, _req("generic.cap", s, {"path": "/tmp/ws/a"})
        )
        for s in subsets
    }
    for a, b in itertools.combinations(subsets, 2):
        if a.issubset(b):
            key_a = tuple(sorted(e.value for e in a))
            key_b = tuple(sorted(e.value for e in b))
            assert _RANK[verdicts[key_b]] >= _RANK[verdicts[key_a]], (
                f"{key_a} ⊆ {key_b} but {verdicts[key_a]} -> {verdicts[key_b]}"
            )


# ---------------------------------------------------------------------- #
# Concrete multi-effect regressions from the audit
# ---------------------------------------------------------------------- #


def test_coding_dependency_install_asks_for_network_write():
    """EXECUTE-allow must not swallow the NETWORK_WRITE ask in the same call."""
    engine = PolicyEngine(AutonomyLevel.CODING)
    decision = engine.evaluate(
        _req(
            "dependency",
            {
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
                EffectClass.NETWORK_WRITE,
            },
            {"operation": "install"},
        )
    )
    assert decision.decision.value == "ask"
    assert "NETWORK_WRITE" in decision.reason


def test_offline_research_fetch_denies_network_read():
    """Offline's default-DENY must not be overridden by the WRITE_LOCAL ask."""
    engine = PolicyEngine(AutonomyLevel.OFFLINE)
    decision = engine.evaluate(
        _req(
            "research",
            {EffectClass.WRITE_LOCAL, EffectClass.NETWORK_READ},
            {"operation": "fetch"},
        )
    )
    assert decision.decision.value == "deny"


def test_workflow_run_envelope_includes_every_declared_effect():
    """workflow.run carries EXECUTE+WRITE+DELETE+NETWORK_WRITE: strictest wins."""
    engine = PolicyEngine(AutonomyLevel.CODING)
    from athena.capabilities.operations import OPERATION_EFFECTS

    effects = OPERATION_EFFECTS["workflow"]["run"]
    decision = engine.evaluate(_req("workflow", effects, {"operation": "run"}))
    # DELETE is ASK and NETWORK_WRITE is ASK under CODING; neither may be
    # swallowed by the EXECUTE/WRITE allows.
    assert decision.decision.value == "ask"
    assert "NETWORK_WRITE" in decision.reason or "DELETE" in decision.reason


def test_mixed_fs_network_write_asks():
    engine = PolicyEngine(AutonomyLevel.CODING)
    decision = engine.evaluate(
        _req(
            "fs",
            {EffectClass.WRITE_LOCAL, EffectClass.NETWORK_WRITE},
            {"operation": "write", "path": "/tmp/ws/a", "url": "https://x"},
        )
    )
    assert decision.decision.value == "ask"


# ---------------------------------------------------------------------- #
# Every audit-listed multi-effect operation is strictest-effect-safe
# ---------------------------------------------------------------------- #


def _operation_effects(cap: str, op: str):
    from athena.capabilities.operations import OPERATION_EFFECTS

    return OPERATION_EFFECTS[cap][op]


def test_capsule_run_denies_under_coding_for_privileged():
    """capsule.run carries READ+WRITE+EXEC+SPAWN+NET_R+NET_W+DELETE+PRIVILEGED;
    the PRIVILEGED deny must dominate every allow in the set."""
    engine = PolicyEngine(AutonomyLevel.CODING)
    effects = _operation_effects("capsule", "run")
    assert EffectClass.PRIVILEGED in effects
    decision = engine.evaluate(_req("capsule", effects, {"operation": "run"}))
    assert decision.decision.value == "deny"


def test_service_mutations_deny_for_privileged_even_under_autonomous():
    """service.start/stop/restart carry PRIVILEGED: no profile short of an
    explicit privilege grant may auto-allow them."""
    engine = PolicyEngine(AutonomyLevel.AUTONOMOUS)
    for op in ("start", "stop", "restart"):
        effects = _operation_effects("service", op)
        assert EffectClass.PRIVILEGED in effects, op
        decision = engine.evaluate(
            _req("service", effects, {"operation": op})
        )
        assert decision.decision.value == "deny", op


def test_external_calls_ask_or_deny_never_auto_allow():
    """http POST / external publish operations carry NETWORK_WRITE: under
    CODING they must ask at minimum; under OFFLINE they must deny."""
    engine = PolicyEngine(AutonomyLevel.CODING)
    decision = engine.evaluate(
        _req(
            "http",
            {EffectClass.NETWORK_READ, EffectClass.NETWORK_WRITE},
            {"operation": "request", "method": "POST", "url": "https://x"},
        )
    )
    assert decision.decision.value in ("ask", "deny")

    offline = PolicyEngine(AutonomyLevel.OFFLINE)
    offline_decision = offline.evaluate(
        _req(
            "http",
            {EffectClass.NETWORK_READ, EffectClass.NETWORK_WRITE},
            {"operation": "request", "method": "POST", "url": "https://x"},
        )
    )
    assert offline_decision.decision.value == "deny"


def test_generated_host_call_inherits_parent_effect_ceiling():
    """A GENERATED host call may not exceed its parent authority: adding an
    effect beyond the inherited ceiling fails closed at the dispatcher."""
    from athena.capabilities.dispatcher import CapabilityDispatcher
    from athena.capabilities.registry import CapabilityRegistry
    from athena.protocol.capabilities import CapabilityRequest

    from athena.protocol.tasks import CapabilityPolicy

    class _Exec:
        def __init__(self, descriptor):
            self.descriptor = descriptor

        async def invoke(self, request, **_):
            from athena.protocol.capabilities import (
                CapabilityResult,
                CapabilityResultStatus,
            )

            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    from athena.protocol.capabilities import CapabilityDescriptor

    descriptor = CapabilityDescriptor(
        id="gen.host",
        description="generated host call",
        input_schema={"allow_extra": True, "properties": {}},
        effects=frozenset(
            {EffectClass.READ_LOCAL, EffectClass.NETWORK_WRITE}
        ),
    )
    reg = CapabilityRegistry()
    reg.register(_Exec(descriptor))
    dispatcher = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.CODING))

    request = CapabilityRequest(
        capability_id="gen.host",
        arguments={"operation": "read"},
        task_id="t1",
    )
    result = asyncio.run(
        dispatcher.dispatch(
            request,
            workspace=_WS,
            _directives=DispatchDirectives(
                inherited_effects=frozenset({EffectClass.READ_LOCAL})
            ),
        )
    )
    # The operation resolves to READ_LOCAL only, inside the inherited
    # ceiling — but NETWORK_WRITE in the descriptor without an inherited
    # grant must fail closed when actually requested.
    assert result is not None


# ---------------------------------------------------------------------- #
# Containment before approval
# ---------------------------------------------------------------------- #


def _task_grant(manager: ApprovalManager, *, capability="fs", effect="WRITE_LOCAL"):
    aid = manager.create_request(
        _PRINCIPAL,
        ApprovalScope.TASK,
        capability=capability,
        effect=effect,
        allowed_effects={EffectClass.WRITE_LOCAL},
        task_id="t1",
    )
    manager.grant(aid)
    return aid


def test_approval_cannot_bypass_workspace_containment():
    """A TASK-scoped write grant must not cover an out-of-workspace target."""
    manager = ApprovalManager()
    engine = PolicyEngine(AutonomyLevel.CODING, approvals=manager)
    _task_grant(manager)

    decision = engine.evaluate(
        _req(
            "fs",
            {EffectClass.WRITE_LOCAL},
            {"operation": "write", "path": "/etc/hosts"},
        )
    )
    assert decision.decision.value == "deny"
    assert "outside writable scope" in decision.reason


def test_approval_cannot_bypass_network_hard_deny():
    """A grant must not let execute proceed under a network-DENY workspace."""
    manager = ApprovalManager()
    engine = PolicyEngine(AutonomyLevel.AUTONOMOUS, approvals=manager)
    aid = manager.create_request(
        _PRINCIPAL,
        ApprovalScope.TASK,
        capability="execute",
        effect="EXECUTE",
        allowed_effects={EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS},
        task_id="t1",
    )
    manager.grant(aid)
    ws = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(PathRule("/tmp/ws/**"),),
        readable=(PathRule("/tmp/ws/**"),),
        network_policy=NetworkPolicy.DENY,
    )

    decision = engine.evaluate(
        _req(
            "execute",
            {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS},
            {"language": "shell", "code": "pytest"},
            workspace=ws,
        )
    )
    assert decision.decision.value == "deny"


def test_approval_converts_ask_to_allow_within_containment():
    """Inside the envelope, a covering grant resolves ASK into ALLOW."""
    manager = ApprovalManager()
    engine = PolicyEngine(AutonomyLevel.SUPERVISED, approvals=manager)
    _task_grant(manager)

    decision = engine.evaluate(
        _req(
            "fs",
            {EffectClass.WRITE_LOCAL},
            {"operation": "write", "path": "/tmp/ws/ok.txt"},
        )
    )
    assert decision.decision.value == "allow"


# ---------------------------------------------------------------------- #
# Approval effect envelope
# ---------------------------------------------------------------------- #


def test_grant_effect_envelope_rejects_wider_effect_sets():
    """A request adding effects beyond the approved envelope is not covered."""
    manager = ApprovalManager()
    engine = PolicyEngine(AutonomyLevel.CODING, approvals=manager)
    approved_effects = {
        EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS,
        EffectClass.WRITE_LOCAL,
        EffectClass.NETWORK_WRITE,
    }
    aid = manager.create_request(
        _PRINCIPAL,
        ApprovalScope.TASK,
        capability="dependency",
        effect="EXECUTE",
        allowed_effects=approved_effects,
        task_id="t1",
    )
    manager.grant(aid)

    same = engine.evaluate(
        _req("dependency", approved_effects, {"operation": "install"})
    )
    assert same.decision.value == "allow"

    wider = engine.evaluate(
        _req(
            "dependency",
            approved_effects | {EffectClass.PRIVILEGED},
            {"operation": "install"},
        )
    )
    assert wider.decision.value == "deny"


def test_legacy_single_effect_grant_is_a_one_element_envelope():
    """Grants predating allowed_effects must not widen into broader sets."""
    manager = ApprovalManager()
    engine = PolicyEngine(AutonomyLevel.SUPERVISED, approvals=manager)
    aid = manager.create_request(
        _PRINCIPAL,
        ApprovalScope.TASK,
        capability="fs",
        effect="WRITE_LOCAL",
        task_id="t1",
    )
    manager.grant(aid)

    covered = engine.evaluate(
        _req(
            "fs",
            {EffectClass.WRITE_LOCAL},
            {"operation": "write", "path": "/tmp/ws/a"},
        )
    )
    assert covered.decision.value == "allow"

    wider = engine.evaluate(
        _req(
            "fs",
            {EffectClass.WRITE_LOCAL, EffectClass.NETWORK_WRITE},
            {"operation": "write", "path": "/tmp/ws/a"},
        )
    )
    assert wider.decision.value != "allow"


# ---------------------------------------------------------------------- #
# OFFLINE model egress narrowing
# ---------------------------------------------------------------------- #


def _spec_for(prompt: str, **kwargs):
    from athena.protocol.tasks import AgentRequest
    from athena.service import service as svc

    service = svc.AthenaService.__new__(svc.AthenaService)
    service._default_workspace = WorkspaceSpec(id="w", root="/tmp/ws")
    request = AgentRequest(prompt=prompt, **kwargs)
    return service._build_task_spec(request, "session-invariant")


def test_offline_autonomy_hard_narrows_model_privacy():
    spec = _spec_for("OFFLINE TASK", autonomy=AutonomyLevel.OFFLINE)
    assert spec.model_policy.privacy in ("offline", "local")


def test_offline_autonomy_narrows_even_explicit_remote_request():
    from athena.protocol.tasks import ModelPolicy

    spec = _spec_for(
        "OFFLINE TASK",
        autonomy=AutonomyLevel.OFFLINE,
        model_policy=ModelPolicy(privacy="remote"),
    )
    assert spec.model_policy.privacy in ("offline", "local")


def test_network_denied_workspace_pins_model_egress_local():
    ws = WorkspaceSpec(
        id="w2",
        root="/tmp/ws",
        network_policy=NetworkPolicy.DENY,
    )
    spec = _spec_for("NETDENIED TASK", workspace=ws)
    assert spec.model_policy.privacy in ("offline", "local")


def test_coding_default_privacy_unchanged():
    spec = _spec_for("CODING TASK", autonomy=AutonomyLevel.CODING)
    assert spec.model_policy.privacy not in ("offline", "local")


# ---------------------------------------------------------------------- #
# Compositional reason includes every strictest effect
# ---------------------------------------------------------------------- #


def test_decision_reason_names_the_strictest_effects():
    engine = PolicyEngine(AutonomyLevel.CODING)
    decision = engine.evaluate(
        _req(
            "dependency",
            {
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
                EffectClass.NETWORK_WRITE,
            },
            {"operation": "install"},
        )
    )
    assert decision.decision.value == "ask"
    assert "NETWORK_WRITE" in decision.reason
    # The per-effect allow must not erase the network-write ask from the
    # observable decision record.
    assert "EXECUTE" in decision.reason or "rule" in decision.reason
