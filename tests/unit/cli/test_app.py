"""CLI composition-root configuration tests."""

from athena.cli.app import Options, _arg_parse, build_config


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
    options = _arg_parse([
        "oi-stream",
        "--db", "/tmp/athena-events.db",
        "--task", "task-42",
    ])

    assert options.command == "oi-stream"
    assert options.db_path == "/tmp/athena-events.db"
    assert options.args == ["task-42"]
