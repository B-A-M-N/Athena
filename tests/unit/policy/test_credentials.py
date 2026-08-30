import pytest

from athena.policy.credentials import (
    EnvSource,
    FileSource,
    SecretError,
    SecretManager,
    write_user_secret,
)


def _manager() -> SecretManager:
    return SecretManager(sources=[])


def test_user_secret_writer_is_owner_only_and_resolvable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = write_user_secret("HERMES_REFEREE_API_KEY", "test-only-secret")

    assert path.read_text() == "test-only-secret"
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
    assert FileSource(str(path.parent), require_private=True).resolve(
        "HERMES_REFEREE_API_KEY"
    ) == "test-only-secret"


def test_private_file_source_rejects_loose_secret(tmp_path):
    path = tmp_path / "secret"
    path.write_text("test-only-secret")
    path.chmod(0o644)

    assert FileSource(str(tmp_path), require_private=True).resolve("secret") is None


def test_env_source_supports_provider_standard_name(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-secret")

    assert EnvSource().resolve("OPENROUTER_API_KEY") == "test-only-secret"


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
