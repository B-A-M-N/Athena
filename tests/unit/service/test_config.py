"""Tests for config layering (P11)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from athena.service.config import (
    AthenaConfig,
    deep_merge,
    load_config,
    load_toml_file,
    merge_configs,
    save_config,
)


def test_deep_merge_simple():
    base = {"a": 1, "b": 2}
    override = {"b": 3, "c": 4}
    result = deep_merge(base, override)
    assert result == {"a": 1, "b": 3, "c": 4}


def test_deep_merge_nested():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    override = {"a": {"y": 10, "z": 20}}
    result = deep_merge(base, override)
    assert result == {"a": {"x": 1, "y": 10, "z": 20}, "b": 3}


def test_merge_configs_multiple():
    a = {"x": 1, "y": 2}
    b = {"y": 10, "z": 3}
    c = {"z": 30, "w": 4}
    result = merge_configs(a, b, c)
    assert result == {"x": 1, "y": 10, "z": 30, "w": 4}


def test_load_toml_file_missing():
    result = load_toml_file("/nonexistent/path/config.toml")
    assert result == {}


def test_load_toml_file_exists():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text('[athena]\nautonomy = "autonomous"\n')
        result = load_toml_file(path)
        assert result == {"athena": {"autonomy": "autonomous"}}


def test_load_config_defaults():
    """load_config with no files/env returns defaults."""
    old_env = os.environ.copy()
    try:
        for key in list(os.environ):
            if key.startswith("ATHENA_"):
                del os.environ[key]
        config = load_config(cwd="/tmp/nonexistent_athena_test")
        assert config.autonomy == "supervised" or str(config.autonomy) == "supervised"
        assert config.worker_max_parallel == 4
        assert config.context_window == 128_000
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_load_config_cli_overrides():
    """CLI overrides take highest precedence."""
    old_env = os.environ.copy()
    try:
        for key in list(os.environ):
            if key.startswith("ATHENA_"):
                del os.environ[key]
        config = load_config(
            cwd="/tmp/nonexistent_athena_test",
            cli_overrides={"worker_max_parallel": 8, "autonomy": "autonomous"},
        )
        assert config.worker_max_parallel == 8
        assert config.autonomy == "autonomous" or str(config.autonomy) == "autonomous"
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_load_config_reads_hermes_referee_environment(monkeypatch):
    monkeypatch.setenv("ATHENA_HERMES_REFEREE_ENABLED", "true")
    monkeypatch.setenv("ATHENA_HERMES_REFEREE_ENDPOINT", "http://hermes.test:8642")
    monkeypatch.setenv("ATHENA_HERMES_REFEREE_PROFILE", "athena-referee")
    monkeypatch.setenv("ATHENA_HERMES_REFEREE_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("ATHENA_HERMES_REFEREE_CREDENTIAL_ID", "HERMES_API_KEY")

    config = load_config(cwd="/tmp/nonexistent_athena_test")

    assert config.hermes_referee.enabled is True
    assert config.hermes_referee.endpoint == "http://hermes.test:8642"
    assert config.hermes_referee.profile == "athena-referee"
    assert config.hermes_referee.timeout_seconds == 30.0
    assert config.hermes_referee.credential_id == "HERMES_API_KEY"


def test_save_and_load_roundtrip():
    """Save a config and load it back (requires tomli_w)."""
    try:
        import tomli_w  # noqa: F401
    except ImportError:
        pytest.skip("tomli_w not installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        config = AthenaConfig(
            db_path="~/test.db",
            autonomy="autonomous",
            worker_max_parallel=8,
        )
        save_config(config, path)
        assert path.exists()

        loaded = load_config(explicit_path=path)
        # db_path gets ~ expanded
        assert "test.db" in (loaded.db_path or "")
        assert loaded.worker_max_parallel == 8


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
