from concurrent.futures import ThreadPoolExecutor
import stat

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
    assert (
        FileSource(str(path.parent), require_private=True).resolve("HERMES_REFEREE_API_KEY")
        == "test-only-secret"
    )


def test_concurrent_secret_writes_are_complete_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    values = [f"secret-{index}" for index in range(80)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda value: write_user_secret("RACE_KEY", value), values))

    destination = results[-1]
    assert destination.read_text(encoding="utf-8") in values
    assert destination.read_text(encoding="utf-8") in {value for value in values}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_secret_writer_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    write_user_secret("NO_RESIDUE", "complete")

    secret_dir = tmp_path / "config" / "athena" / "secrets"
    assert not list(secret_dir.glob(".*.tmp"))


def test_secret_directory_symlink_is_rejected(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    config_home.joinpath("athena").mkdir(parents=True)
    target = tmp_path / "redirected-secrets"
    target.mkdir()
    config_home.joinpath("athena", "secrets").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked secret directory"):
        write_user_secret("NO_REDIRECT", "secret")

    assert not (target / "NO_REDIRECT").exists()


def test_secret_destination_remains_0600_and_directory_0700(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    path = write_user_secret("PRIVATE_KEY", "secret")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


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
