"""Compiled policy snapshot invariants (P1 accelerator).

The snapshot is an ACCELERATOR, not authority: it must produce verdicts
identical to the uncached engine for every input, and its revision guards
must discard it whenever any authority input changes. A stale snapshot is
rebuilt, never consulted.
"""

from __future__ import annotations

import itertools

import pytest

from athena.policy.engine import PolicyEngine
from athena.policy.snapshot import (
    SnapshotKey,
    clear_snapshot_cache,
    get_snapshot,
)
from athena.protocol.capabilities import EffectClass
from athena.protocol.policy import PolicyRequest, Principal
from athena.protocol.tasks import (
    AutonomyLevel,
    CapabilityPolicy,
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


def _req(
    capability_id: str,
    effects,
    arguments=None,
    *,
    workspace=None,
    backend="local",
) -> PolicyRequest:
    return PolicyRequest(
        principal=_PRINCIPAL,
        task_id="t1",
        capability_id=capability_id,
        arguments=arguments or {},
        workspace=workspace or _WS,
        execution_backend=backend,
        effects=frozenset(effects),
    )


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_snapshot_cache()
    yield
    clear_snapshot_cache()


# ---------------------------------------------------------------------- #
# Accelerator validity: cached == uncached, always
# ---------------------------------------------------------------------- #


def test_snapshot_matches_uncached_ruleset():
    """The compiled snapshot's ruleset IS the profile ruleset."""
    for level in AutonomyLevel:
        snap = get_snapshot(level=level, workspace=None, task_policy=None)
        from athena.policy.profiles import profile_ruleset

        assert snap.rules is profile_ruleset(level)


def test_snapshot_verdicts_identical_to_fresh_engine():
    """A warm-cache engine and a cold engine must agree on every input."""
    universe = (
        EffectClass.READ_LOCAL,
        EffectClass.WRITE_LOCAL,
        EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS,
        EffectClass.NETWORK_READ,
        EffectClass.NETWORK_WRITE,
        EffectClass.DELETE,
    )
    cold = PolicyEngine(AutonomyLevel.CODING)
    warm = PolicyEngine(AutonomyLevel.CODING)
    for size in (1, 2, 4):
        for combo in itertools.combinations(universe, size):
            request = _req("generic.cap", combo, {"path": "/tmp/ws/a"})
            assert (
                cold.evaluate(request).decision
                == warm.evaluate(request).decision
            ), combo


def test_repeated_evaluation_is_stable_across_cache_hits():
    engine = PolicyEngine(AutonomyLevel.SUPERVISED)
    first = engine.evaluate(
        _req("dependency", {EffectClass.EXECUTE, EffectClass.NETWORK_WRITE}, {"operation": "install"})
    ).decision
    for _ in range(5):
        again = engine.evaluate(
            _req(
                "dependency",
                {EffectClass.EXECUTE, EffectClass.NETWORK_WRITE},
                {"operation": "install"},
            )
        ).decision
        assert again is first


# ---------------------------------------------------------------------- #
# Revision guards: stale snapshots are rebuilt, never consulted
# ---------------------------------------------------------------------- #


def test_snapshot_key_captures_authority_triple():
    key_a = SnapshotKey(AutonomyLevel.CODING, "w1", CapabilityPolicy(allow=("fs",)))
    key_b = SnapshotKey(AutonomyLevel.CODING, "w1", CapabilityPolicy(allow=("fs",)))
    key_c = SnapshotKey(AutonomyLevel.OFFLINE, "w1", CapabilityPolicy(allow=("fs",)))
    key_d = SnapshotKey(AutonomyLevel.CODING, "w2", CapabilityPolicy(allow=("fs",)))
    assert key_a == key_b
    assert key_a != key_c
    assert key_a != key_d


def test_task_policy_change_invalidates_snapshot():
    """A changed task capability ceiling changes the task authority revision."""
    p1 = CapabilityPolicy(allow=("fs",))
    p2 = CapabilityPolicy(allow=("git",))
    assert p1 != p2
    s1 = get_snapshot(level=AutonomyLevel.CODING, workspace=_WS, task_policy=p1)
    s2 = get_snapshot(level=AutonomyLevel.CODING, workspace=_WS, task_policy=p2)
    assert s1.task_authority_revision != s2.task_authority_revision


def test_level_change_selects_different_snapshot():
    coding = get_snapshot(level=AutonomyLevel.CODING, workspace=None, task_policy=None)
    offline = get_snapshot(level=AutonomyLevel.OFFLINE, workspace=None, task_policy=None)
    assert coding.rules is not offline.rules
    assert offline.rules.default == "deny"


def test_path_rule_change_with_same_id_root_invalidates_snapshot():
    """Two workspaces with the same id/root/revision but DIFFERENT path rules
    must not share a snapshot — the guard compares rule CONTENT, not just the
    preservation key, or the engine could serve a stale (wider or narrower)
    authorization scope for a real workspace."""
    unrestricted = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(),
    )
    narrow = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(PathRule("/tmp/ws/sub/**"),),
    )
    assert (unrestricted.root, unrestricted.revision) == (
        narrow.root,
        narrow.revision,
    )
    s1 = get_snapshot(level=AutonomyLevel.CODING, workspace=unrestricted, task_policy=None)
    # Unrestricted writable compiles to an empty rule tuple...
    assert s1.writable_rules == ()
    # ...and the guard must REJECT it for the differently-scoped workspace.
    assert not s1.guards_match(
        level=AutonomyLevel.CODING,
        workspace=narrow,
        task_policy=None,
        policy_revision="1",
    )
    # Writing under the narrow scope must now be denied, not served by an
    # unrestricted stale snapshot.
    s2 = get_snapshot(level=AutonomyLevel.CODING, workspace=narrow, task_policy=None)
    assert s2 is not s1
    assert s2.writable_rules == ("/tmp/ws/sub/**",)


def test_workspace_revision_change_invalidates_snapshot():
    ws_v1 = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(PathRule("/tmp/ws/**"),),
        revision="r1",
    )
    ws_v2 = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(PathRule("/tmp/ws/**"),),
        revision="r2",
    )
    s1 = get_snapshot(level=AutonomyLevel.CODING, workspace=ws_v1, task_policy=None)
    assert s1.workspace_revision == "r1"
    # The guard must refuse s1 for ws_v2.
    assert not s1.guards_match(
        level=AutonomyLevel.CODING,
        workspace=ws_v2,
        task_policy=None,
        policy_revision="1",
    )
    s2 = get_snapshot(level=AutonomyLevel.CODING, workspace=ws_v2, task_policy=None)
    assert s2.workspace_revision == "r2"
    assert s2 is not s1


def test_policy_revision_bump_invalidates_snapshot():
    s1 = get_snapshot(
        level=AutonomyLevel.CODING, workspace=None, task_policy=None, policy_revision="1"
    )
    assert not s1.guards_match(
        level=AutonomyLevel.CODING,
        workspace=None,
        task_policy=None,
        policy_revision="2",
    )


def test_get_snapshot_revalidates_guards_on_every_call():
    """A cached snapshot whose guard no longer matches must be rebuilt."""
    ws = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(PathRule("/tmp/ws/**"),),
        revision="r1",
    )
    s1 = get_snapshot(level=AutonomyLevel.CODING, workspace=ws, task_policy=None)
    ws_rev2 = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(PathRule("/tmp/ws/**"),),
        revision="r2",
    )
    s2 = get_snapshot(level=AutonomyLevel.CODING, workspace=ws_rev2, task_policy=None)
    assert s2 is not s1
    assert s2.workspace_revision == "r2"


# ---------------------------------------------------------------------- #
# Never-authority: the snapshot cannot soften a structural deny
# ---------------------------------------------------------------------- #


def test_snapshot_engine_still_denies_out_of_workspace_write():
    engine = PolicyEngine(AutonomyLevel.CODING)
    decision = engine.evaluate(
        _req("fs", {EffectClass.WRITE_LOCAL}, {"operation": "write", "path": "/etc/hosts"})
    )
    assert decision.decision.value == "deny"


def test_snapshot_engine_still_denies_network_deny_execute():
    ws = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(PathRule("/tmp/ws/**"),),
        readable=(PathRule("/tmp/ws/**"),),
        network_policy=NetworkPolicy.DENY,
    )
    engine = PolicyEngine(AutonomyLevel.AUTONOMOUS)
    decision = engine.evaluate(
        _req(
            "execute",
            {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS},
            {"language": "shell", "code": "pytest"},
            workspace=ws,
        )
    )
    assert decision.decision.value == "deny"


def test_snapshot_engine_monotonicity_holds_under_cache():
    """The composition property survives the accelerator: adding an effect
    can never soften the verdict even when every evaluation hits cache."""
    engine = PolicyEngine(AutonomyLevel.CODING)
    base = engine.evaluate(
        _req("generic.cap", (EffectClass.READ_LOCAL,), {"path": "/tmp/ws/a"})
    ).decision.value
    wider = engine.evaluate(
        _req(
            "generic.cap",
            (EffectClass.READ_LOCAL, EffectClass.NETWORK_WRITE),
            {"path": "/tmp/ws/a"},
        )
    ).decision.value
    rank = {"allow": 0, "ask": 1, "deny": 2}
    assert rank[wider] >= rank[base]


def test_snapshot_engine_approval_path_unchanged():
    """Approvals still convert ASK to ALLOW only after containment."""
    from athena.policy.approvals import ApprovalManager
    from athena.protocol.policy import ApprovalScope

    manager = ApprovalManager()
    engine = PolicyEngine(AutonomyLevel.CODING, approvals=manager)
    aid = manager.create_request(
        _PRINCIPAL,
        ApprovalScope.TASK,
        capability="fs",
        effect="WRITE_LOCAL",
        allowed_effects={EffectClass.WRITE_LOCAL},
        task_id="t1",
    )
    manager.grant(aid)

    allowed = engine.evaluate(
        _req("fs", {EffectClass.WRITE_LOCAL}, {"operation": "write", "path": "/tmp/ws/ok"})
    )
    assert allowed.decision.value == "allow"

    denied = engine.evaluate(
        _req("fs", {EffectClass.WRITE_LOCAL}, {"operation": "write", "path": "/etc/hosts"})
    )
    assert denied.decision.value == "deny"
