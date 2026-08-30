"""Round-trip tests: AthenaConfig -> config_to_dict -> config_from_dict."""

from pathlib import Path

from athena.service.config import (
    MCPConfig,
    AthenaConfig,
    HermesRefereeConfig,
    ProviderConfig,
    config_from_dict,
    config_to_dict,
    project_config_paths,
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
    )
    d = config_to_dict(config)
    assert "providers" in d and "mcp_servers" in d

    restored = config_from_dict(d)

    assert len(restored.providers) == 1
    p = restored.providers[0]
    assert (p.kind, p.name, p.model) == ("openai", "main", "gpt-4o")
    assert p.credential_id == "cred-1"
    assert p.api_key == "sk-test"
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
        )
    )

    restored = config_from_dict(config_to_dict(config))

    assert restored.hermes_referee == config.hermes_referee


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
