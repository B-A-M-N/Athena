"""Round-trip tests: AthenaConfig -> config_to_dict -> config_from_dict."""

from pathlib import Path
import os
import stat

import pytest

from athena.service.config import (
    MCPConfig,
    AthenaConfig,
    HermesRefereeConfig,
    ProviderConfig,
    config_from_dict,
    config_to_dict,
    project_config_paths,
    save_config,
)


def test_roundtrip_providers_mcp_model_roles():
    config = AthenaConfig(
        providers=(
            ProviderConfig(
                kind="openai",
                name="main",
                model="gpt-4o",
                credential_id="cred-1",
                api_key="sk-test",
                base_url="https://example.com/v1",
                cache_mode="none",
                latency_class="fast",
                extra={"temperature": 0.2},
            ),
        ),
        mcp_servers=(
            MCPConfig(
                name="fs",
                command="npx",
                args=("server-fs", "/tmp"),
                url=None,
                env={"FOO": "bar"},
                secret_env={"TOKEN": "t"},
                connect_timeout=5.0,
            ),
        ),
        model_roles={
            "coder": {
                "allowed": ["gpt-4o"],
                "max_cost_usd": "0.01",
                "routing_preference": "latency",
            }
        },
        cache_namespace="tenant-a",
        memory_embedding_model="intfloat/e5-small-v2",
        memory_embedding_cache_dir="/var/cache/athena/embeddings",
        research_discovery_endpoints=(
            "https://index-a.example.test/search",
            "https://index-b.example.test/search",
        ),
        required_capabilities=("browser", "mcp:tools"),
        capability_profile="release",
        capability_profiles={"release": ("computer", "mcp:tools")},
    )
    d = config_to_dict(config)
    assert "providers" in d and "mcp_servers" in d

    restored = config_from_dict(d)

    assert len(restored.providers) == 1
    p = restored.providers[0]
    assert (p.kind, p.name, p.model) == ("openai", "main", "gpt-4o")
    assert p.credential_id == "cred-1"
    assert p.api_key is None
    assert "api_key" not in d["providers"][0]
    assert p.base_url == "https://example.com/v1"
    assert p.cache_mode == "none"
    assert p.latency_class == "fast"
    assert p.extra == {"temperature": 0.2}

    assert len(restored.mcp_servers) == 1
    m = restored.mcp_servers[0]
    assert (m.name, m.command, m.args) == ("fs", "npx", ("server-fs", "/tmp"))
    assert m.url is None
    assert m.env == {"FOO": "bar"}
    assert m.secret_env == {"TOKEN": "t"}
    assert m.connect_timeout == 5.0

    assert restored.model_roles == {
        "coder": {
            "allowed": ["gpt-4o"],
            "max_cost_usd": "0.01",
            "routing_preference": "latency",
        }
    }
    assert restored.cache_namespace == "tenant-a"
    assert restored.memory_embedding_model == "intfloat/e5-small-v2"
    assert restored.memory_embedding_cache_dir == "/var/cache/athena/embeddings"
    assert restored.research_discovery_endpoints == (
        "https://index-a.example.test/search",
        "https://index-b.example.test/search",
    )
    assert restored.effective_required_capabilities == (
        "browser",
        "mcp:tools",
        "computer",
    )


def test_roundtrip_hermes_referee_config():
    config = AthenaConfig(
        hermes_referee=HermesRefereeConfig(
            enabled=True,
            endpoint="http://127.0.0.1:8642",
            profile="athena-referee",
            timeout_seconds=45.0,
            credential_id="HERMES_API_KEY",
            allow_remote=True,
            allow_insecure_remote=True,
            self_host_supervision="advisory",
        )
    )

    restored = config_from_dict(config_to_dict(config))

    assert restored.hermes_referee == config.hermes_referee


def test_roundtrip_serializable_browser_config():
    config = AthenaConfig(
        browser_enabled=True,
        browser_engine="firefox",
        browser_headless=False,
        browser_launch_args=("--safe-mode",),
        browser_executable_path="/opt/firefox/firefox",
        browser_channel="nightly",
        browser_session_scope="session",
        browser_timeout_ms=12_000,
        browser_viewport=(1024, 768),
    )

    restored = config_from_dict(config_to_dict(config))

    assert restored.browser_enabled is True
    assert restored.browser_engine == "firefox"
    assert restored.browser_headless is False
    assert restored.browser_launch_args == ("--safe-mode",)
    assert restored.browser_executable_path == "/opt/firefox/firefox"
    assert restored.browser_channel == "nightly"
    assert restored.browser_session_scope == "session"


def test_roundtrip_runtime_recovery_and_worker_timing_config():
    config = AthenaConfig(
        parked_slot_wait_s=0.25,
        worker_lease_duration_seconds=42.0,
        worker_lease_renewal_divisor=2.0,
    )

    restored = config_from_dict(config_to_dict(config))

    assert restored.parked_slot_wait_s == 0.25
    assert restored.worker_lease_duration_seconds == 42.0
    assert restored.worker_lease_renewal_divisor == 2.0
    assert restored.browser_timeout_ms == 12_000
    assert restored.browser_viewport == (1024, 768)


def test_legacy_hermes_required_flag_normalizes_to_explicit_policy():
    restored = config_from_dict(
        {
            "hermes_referee": {
                "enabled": True,
                "required_for_self_host": False,
            }
        }
    )

    assert restored.hermes_referee.supervision_mode.value == "advisory"
    assert "required_for_self_host" not in config_to_dict(restored)["hermes_referee"]


def test_save_config_migrates_legacy_provider_key_to_private_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = AthenaConfig(
        providers=(ProviderConfig(kind="openai", name="main", api_key="sk-test"),)
    )

    path = tmp_path / "athena.toml"
    save_config(config, path)

    text = path.read_text()
    assert "sk-test" not in text
    assert 'credential_id = "main_api_key"' in text
    assert (tmp_path / "config" / "athena" / "secrets" / "main_api_key").read_text() == "sk-test"


def test_save_config_sanitizes_generated_legacy_secret_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = AthenaConfig(
        providers=(ProviderConfig(kind="openai", name="team/provider", api_key="sk-test"),)
    )

    save_config(config, tmp_path / "athena.toml")

    assert 'credential_id = "team_provider_api_key"' in (tmp_path / "athena.toml").read_text()
    assert (
        tmp_path / "config" / "athena" / "secrets" / "team_provider_api_key"
    ).read_text() == "sk-test"


def test_save_config_is_atomic_and_keeps_owner_only_file_on_write_failure(tmp_path, monkeypatch):
    import tomli_w

    path = tmp_path / "athena.toml"
    previous = b'autonomy = "supervised"\n'
    path.write_bytes(previous)
    os.chmod(path, 0o600)

    def fail_after_partial_write(data, handle):
        del data
        handle.write(b"partial = ")
        raise OSError("simulated config write failure")

    monkeypatch.setattr(tomli_w, "dump", fail_after_partial_write)
    with pytest.raises(OSError, match="simulated config write failure"):
        save_config(AthenaConfig(), path)

    assert path.read_bytes() == previous
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(tmp_path.glob(".athena.toml.*.tmp"))


@pytest.mark.parametrize("mode", (0o755, 0o770))
def test_save_config_does_not_change_existing_parent_permissions(tmp_path, mode):
    parent = tmp_path / "shared-config"
    parent.mkdir()
    os.chmod(parent, mode)

    save_config(AthenaConfig(), parent / "athena.toml")

    assert stat.S_IMODE(parent.stat().st_mode) == mode


def test_save_config_makes_new_parent_private_and_file_owner_only(tmp_path):
    path = tmp_path / "new-config" / "athena.toml"

    save_config(AthenaConfig(), path)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_config_rejects_destination_symlink(tmp_path):
    target = tmp_path / "target.toml"
    target.write_text("keep = true\n")
    link = tmp_path / "athena.toml"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlinked config path"):
        save_config(AthenaConfig(), link)
    assert target.read_text() == "keep = true\n"


def test_save_config_rejects_symlinked_parent_directory(tmp_path):
    target_dir = tmp_path / "real-config"
    target_dir.mkdir()
    link_dir = tmp_path / "config"
    link_dir.symlink_to(target_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked config directory"):
        save_config(AthenaConfig(), link_dir / "athena.toml")
    assert not (target_dir / "athena.toml").exists()


def test_project_config_paths_root_most_first(tmp_path: Path):
    root = tmp_path / "root"
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (root / ".athena").mkdir()
    (nested / ".athena").mkdir()

    paths = project_config_paths(str(nested))
    found = [p for p in paths if p.parent.parent in (root, nested)]
    # root-most must come before the cwd-local one
    assert found.index(root / ".athena" / "config.toml") < found.index(
        nested / ".athena" / "config.toml"
    )


def test_load_config_local_overrides_ancestor(tmp_path: Path):
    from athena.service.config import load_config

    root = tmp_path
    cwd = root / "project" / "sub"
    cwd.mkdir(parents=True)
    (root / ".athena").mkdir()
    (cwd / ".athena").mkdir()
    (root / ".athena" / "config.toml").write_text("context_window = 999\n")
    (cwd / ".athena" / "config.toml").write_text("context_window = 42\n")

    cfg = load_config(cwd=str(cwd))
    assert cfg.context_window == 42


def test_no_path_monkeypatch():
    assert not hasattr(Path, "reversed_parents")
