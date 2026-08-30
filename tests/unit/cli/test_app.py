"""CLI composition-root configuration tests."""

from athena.cli.app import Options, _arg_parse, _config_set, build_config


def test_build_config_auto_wires_openrouter_free_router(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-secret")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    config = build_config(Options(config_path="/tmp/athena-test-no-config.toml"))

    assert len(config.providers) == 1
    provider = config.providers[0]
    assert provider.kind == "openai-compat"
    assert provider.name == "openrouter"
    assert provider.model == "poolside/laguna-s-2.1:free"
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.credential_id == "OPENROUTER_API_KEY"
    assert provider.api_key is None


def test_build_config_honors_openrouter_model_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")

    config = build_config(Options(config_path="/tmp/athena-test-no-config.toml"))

    assert config.providers[0].model == "google/gemma-4-31b-it:free"


def test_build_config_rejects_paid_openrouter_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-5")

    try:
        build_config(Options(config_path="/tmp/athena-test-no-config.toml"))
    except ValueError as exc:
        assert "free route" in str(exc)
    else:  # pragma: no cover - keeps the assertion explicit for the contract
        raise AssertionError("paid OpenRouter model was accepted on the free path")


def test_argparse_oi_stream_preserves_task_and_db_options():
    options = _arg_parse(
        [
            "oi-stream",
            "--db",
            "/tmp/athena-events.db",
            "--task",
            "task-42",
        ]
    )

    assert options.command == "oi-stream"
    assert options.db_path == "/tmp/athena-events.db"
    assert options.args == ["task-42"]


def test_argparse_config_set_preserves_operator_key_and_value():
    options = _arg_parse(["config", "set", "hermes-referee.enabled", "true"])

    assert options.command == "config"
    assert options.config_action == "set"
    assert options.config_key == "hermes-referee.enabled"
    assert options.config_value == "true"


def test_config_set_writes_hermes_referee_section(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    assert (
        _config_set(
            Options(
                command="config",
                config_action="set",
                config_key="hermes-referee.enabled",
                config_value="true",
            )
        )
        == 0
    )
    assert (
        _config_set(
            Options(
                command="config",
                config_action="set",
                config_key="hermes-referee.endpoint",
                config_value="http://127.0.0.1:8642",
            )
        )
        == 0
    )

    from athena.service.config import load_toml_file

    data = load_toml_file(tmp_path / "config-home" / "athena" / "config.toml")
    assert data["hermes_referee"] == {
        "enabled": True,
        "endpoint": "http://127.0.0.1:8642",
    }
