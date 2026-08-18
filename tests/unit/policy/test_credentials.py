import pytest

from athena.policy.credentials import SecretError, SecretManager


def _manager() -> SecretManager:
    return SecretManager(sources=[])


def test_can_use_returns_false_with_no_context():
    mgr = _manager()
    # No owner context, no delegation -> NOT permissive.
    assert mgr.can_use("child", "db_password") is False


def test_can_use_true_when_owner_and_task_match():
    mgr = _manager()
    assert mgr.can_use("task-1", "key", owner_task="task-1") is True


def test_can_use_false_for_another_task_even_when_someone_is_owner():
    mgr = _manager()
    assert mgr.can_use("task-2", "key", owner_task="task-1") is False


def test_issue_lease_without_owner_is_denied():
    mgr = _manager()
    with pytest.raises(SecretError):
        mgr.issue_lease("api_key", task_id="task-1")


def test_issue_lease_denied_for_unowned_even_when_resolvable():
    mgr = SecretManager(sources=[])
    with pytest.raises(SecretError):
        mgr.issue_lease("gh_token", task_id="child-1")