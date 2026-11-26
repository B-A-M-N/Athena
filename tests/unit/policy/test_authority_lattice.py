"""Property checks for the shared delegated-authority algebra."""

from __future__ import annotations

from datetime import timedelta

from hypothesis import given, strategies as st

from athena.scheduler.control import _intersect_workspace_records, _workspace_covers
from athena.policy.path_scope import intersect_path_rules, path_rules_cover
from athena.protocol.tasks import (
    CapabilityPolicy,
    ModelPolicy,
    ResourceBudgetCeiling,
    capability_policy_covers,
    intersect_capability_policies,
    intersect_model_policies,
    intersect_resource_budgets,
    model_policy_covers,
    resource_budget_covers,
)


_PATHS = (
    "/workspace",
    "/workspace/a",
    "/workspace/a/file",
    "/workspace/b",
    "/workspace/secret",
    "/workspace/secret/file",
    "/outside",
)
_PATH_PREFIXES = ("/workspace", "/workspace/a", "/workspace/b", "/workspace/secret")
_CAPABILITIES = ("files.read", "files.write", "execute", "network.fetch")


@st.composite
def _path_rules(draw):
    values = draw(
        st.lists(
            st.tuples(st.sampled_from(_PATH_PREFIXES), st.booleans()),
            min_size=0,
            max_size=5,
        )
    )
    return [{"path": path, "allow": allow} for path, allow in values]


@st.composite
def _capability_policies(draw):
    return CapabilityPolicy(
        effects=frozenset(draw(st.sets(st.sampled_from(_CAPABILITIES), max_size=4))),
        allow=tuple(draw(st.lists(st.sampled_from(_CAPABILITIES), unique=True, max_size=4))),
        ask=tuple(draw(st.lists(st.sampled_from(_CAPABILITIES), unique=True, max_size=4))),
        deny=tuple(
            draw(st.lists(st.sampled_from(_CAPABILITIES + ("*",)), unique=True, max_size=4))
        ),
    )


@st.composite
def _budgets(draw):
    values = {}
    for name in (
        "max_agent_iterations",
        "max_input_tokens",
        "max_children",
        "max_parallel_executions",
    ):
        values[name] = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=100)))
    values["max_cost_usd"] = draw(
        st.one_of(st.none(), st.decimals(min_value="0.01", max_value="100", places=2))
    )
    values["max_wall_time"] = draw(
        st.one_of(st.none(), st.integers(min_value=1, max_value=1000).map(timedelta))
    )
    return ResourceBudgetCeiling(**values)


@st.composite
def _model_policies(draw):
    return ModelPolicy(
        allowed=tuple(
            draw(st.lists(st.sampled_from(("local", "remote", "fast")), unique=True, max_size=3))
        ),
        require_tools=draw(st.booleans()),
        privacy=draw(st.sampled_from(("offline", "local", "local-preferred", "remote"))),
        max_cost_usd=draw(
            st.one_of(st.none(), st.decimals(min_value="0.01", max_value="100", places=2))
        ),
        min_quality_tier=draw(
            st.one_of(st.none(), st.sampled_from(("basic", "standard", "advanced")))
        ),
        require_declared_quality=draw(st.booleans()),
        max_model_attempts=draw(st.integers(min_value=1, max_value=8)),
    )


@st.composite
def _workspace_records(draw):
    rules = draw(_path_rules())
    return {
        "root": "/workspace",
        "readable": rules,
        "writable": rules,
        "network_policy": draw(st.sampled_from(("deny", "restricted", "allow"))),
        "mutation_mode": draw(st.sampled_from(("read_only", "speculative", "direct"))),
    }


@given(_path_rules(), _path_rules())
def test_path_intersection_is_a_subset_of_each_side(left, right):
    intersection = intersect_path_rules(left, right, scope="/workspace")
    assert path_rules_cover(left, intersection, upper_base="/workspace", lower_base="/workspace")
    assert path_rules_cover(right, intersection, upper_base="/workspace", lower_base="/workspace")


@given(_path_rules())
def test_path_coverage_is_reflexive(rules):
    assert path_rules_cover(rules, rules)


@given(_path_rules(), _path_rules())
def test_path_intersection_is_commutative_and_idempotent(left, right):
    left_right = intersect_path_rules(left, right, scope="/workspace")
    right_left = intersect_path_rules(right, left, scope="/workspace")
    for path in _PATHS:
        assert _path_allowed(path, left_right) == _path_allowed(path, right_left)
    same = intersect_path_rules(left, left)
    for path in _PATHS:
        assert _path_allowed(path, left) == _path_allowed(path, same)


@given(_path_rules(), _path_rules(), _path_rules())
def test_path_coverage_is_transitive(left, middle, right):
    if path_rules_cover(left, middle) and path_rules_cover(middle, right):
        assert path_rules_cover(left, right)


@given(_capability_policies(), _capability_policies())
def test_capability_intersection_is_reflexive_and_bounded(left, right):
    intersection = intersect_capability_policies(left, right)
    assert capability_policy_covers(left, intersection)
    assert capability_policy_covers(right, intersection)
    assert capability_policy_covers(left, left)


def test_disjoint_capability_intersection_is_represented_as_empty_authority():
    left = CapabilityPolicy(effects=frozenset({"files.read"}), allow=("files.read",))
    right = CapabilityPolicy(effects=frozenset({"files.write"}), allow=("files.write",))
    intersection = intersect_capability_policies(left, right)
    assert capability_policy_covers(left, intersection)
    assert capability_policy_covers(right, intersection)
    assert not capability_policy_covers(left, right)


@given(_capability_policies(), _capability_policies(), _capability_policies())
def test_capability_coverage_is_transitive(left, middle, right):
    if capability_policy_covers(left, middle) and capability_policy_covers(middle, right):
        assert capability_policy_covers(left, right)


@given(_budgets(), _budgets())
def test_budget_intersection_is_reflexive_and_bounded(left, right):
    intersection = intersect_resource_budgets(left, right)
    assert resource_budget_covers(left, intersection)
    assert resource_budget_covers(right, intersection)
    assert resource_budget_covers(left, left)


@given(_budgets(), _budgets(), _budgets())
def test_budget_coverage_is_transitive(left, middle, right):
    if resource_budget_covers(left, middle) and resource_budget_covers(middle, right):
        assert resource_budget_covers(left, right)


@given(_model_policies(), _model_policies())
def test_model_intersection_is_reflexive_and_bounded(left, right):
    intersection = intersect_model_policies(left, right)
    assert model_policy_covers(left, intersection)
    assert model_policy_covers(right, intersection)
    assert model_policy_covers(left, left)


@given(_model_policies(), _model_policies(), _model_policies())
def test_model_coverage_is_transitive(left, middle, right):
    if model_policy_covers(left, middle) and model_policy_covers(middle, right):
        assert model_policy_covers(left, right)


@given(_path_rules(), _path_rules())
def test_workspace_intersection_is_bounded(left_rules, right_rules):
    left = {
        "root": "/workspace",
        "readable": left_rules,
        "writable": left_rules,
        "network_policy": "allow",
        "mutation_mode": "direct",
    }
    right = {
        "root": "/workspace",
        "readable": right_rules,
        "writable": right_rules,
        "network_policy": "restricted",
        "mutation_mode": "read_only",
    }
    intersection = _intersect_workspace_records(left, right)
    assert _workspace_covers(left, intersection)
    assert _workspace_covers(right, intersection)


@given(_workspace_records(), _workspace_records(), _workspace_records())
def test_workspace_coverage_is_transitive(left, middle, right):
    if _workspace_covers(left, middle) and _workspace_covers(middle, right):
        assert _workspace_covers(left, right)


def _path_allowed(path, rules):
    from athena.policy.path_scope import path_allowed

    return path_allowed(path, rules)
