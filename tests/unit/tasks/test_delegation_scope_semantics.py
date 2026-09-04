"""Delegated capability-narrowing semantics (P0-3).

The audit found two defects in the delegated-scope boundary:

* ``_scope_policy()`` computed ``child_requested - parent_allow - parent_ask``
  even when the parent ceiling was UNRESTRICTED (empty allow/ask), turning a
  valid child narrowing into a deny.
* ``CapabilityPolicy(allow={"fs"}, ask={"execute"})`` made ``execute``
  non-permitted: the ask list only mattered when allow was empty.

Required semantics: callable = ``allow ∪ ask - deny``; an empty parent
ceiling means open, so the child's explicit requested set is a valid
narrowing.
"""

from __future__ import annotations

from athena.protocol.tasks import CapabilityPolicy, capability_id_permitted


# ---------------------------------------------------------------------- #
# CapabilityPolicy allow/ask/deny semantics
# ---------------------------------------------------------------------- #


def test_ask_entries_are_callable_alongside_allow():
    """ask must be callable-with-approval even when allow is non-empty."""
    policy = CapabilityPolicy(allow=("fs",), ask=("execute",))
    assert capability_id_permitted("execute", policy) is True
    assert capability_id_permitted("fs", policy) is True
    assert capability_id_permitted("git", policy) is False


def test_deny_removes_even_when_allowed():
    policy = CapabilityPolicy(allow=("fs", "git"), deny=("fs",))
    assert capability_id_permitted("fs", policy) is False
    assert capability_id_permitted("git", policy) is True


def test_empty_policy_means_unrestricted():
    policy = CapabilityPolicy()
    assert capability_id_permitted("anything", policy) is True


def test_ask_only_policy_is_callable():
    policy = CapabilityPolicy(ask=("execute",))
    assert capability_id_permitted("execute", policy) is True


# ---------------------------------------------------------------------- #
# Delegated scope intersection
# ---------------------------------------------------------------------- #


def _scope_policy(parent_policy, child_policy):
    from athena.protocol.tasks import TaskSpec
    from athena.tasks.delegation import _scope_policy

    parent = TaskSpec(id="p", objective="", capability_policy=parent_policy)
    return _scope_policy(parent, child_policy)


def test_unrestricted_parent_honors_child_narrowing():
    """Parent with no ceiling + child allow={'fs'} must NOT deny fs."""
    scoped = _scope_policy(CapabilityPolicy(), CapabilityPolicy(allow=("fs",)))
    assert capability_id_permitted("fs", scoped) is True


def test_restricted_parent_intersects_with_child_request():
    scoped = _scope_policy(
        CapabilityPolicy(allow=("fs", "git")),
        CapabilityPolicy(allow=("fs", "execute")),
    )
    assert capability_id_permitted("fs", scoped) is True
    # execute is outside the parent ceiling: narrowed away
    assert capability_id_permitted("execute", scoped) is False
    # git was parent-allowed but the child did not request it
    assert capability_id_permitted("git", scoped) is False


def test_parent_deny_is_preserved_under_child_narrowing():
    scoped = _scope_policy(
        CapabilityPolicy(allow=("fs", "execute"), deny=("execute",)),
        CapabilityPolicy(allow=("execute",)),
    )
    assert capability_id_permitted("execute", scoped) is False


def test_parent_ask_entry_survives_intersection():
    scoped = _scope_policy(
        CapabilityPolicy(ask=("execute",)),
        CapabilityPolicy(allow=("execute",)),
    )
    assert capability_id_permitted("execute", scoped) is True


def test_child_request_outside_unrestricted_parent_is_not_denied():
    """With no parent ceiling, every child-requested capability stays callable."""
    scoped = _scope_policy(
        CapabilityPolicy(),
        CapabilityPolicy(allow=("fs", "git", "execute")),
    )
    for cap in ("fs", "git", "execute"):
        assert capability_id_permitted(cap, scoped) is True, cap
