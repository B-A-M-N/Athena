"""CLI composition-root configuration tests."""

from athena.cli.app import Options, build_config


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
