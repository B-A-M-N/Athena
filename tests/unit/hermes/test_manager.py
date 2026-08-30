from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from athena.hermes.manager import HermesRefereeManager, HermesRefereeManagerError
from athena.service.config import load_toml_file


@pytest.mark.asyncio
async def test_setup_enables_only_after_probe_and_keeps_key_in_user_store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    config_path = tmp_path / "athena.toml"
    manager = HermesRefereeManager(
        config_path=config_path,
        runtime_root=tmp_path / "hermes",
    )
    api_server = tmp_path / "hermes" / "gateway" / "platforms" / "api_server.py"
    api_server.parent.mkdir(parents=True)
    (tmp_path / "hermes" / "hermes_cli").mkdir()
    (tmp_path / "hermes" / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    api_server.write_text('"tool_execution": "disabled"\n_referee_mode = True\n', encoding="utf-8")
    monkeypatch.setattr(HermesRefereeManager, "_run_hermes", AsyncMock())
    monkeypatch.setattr(HermesRefereeManager, "_probe", AsyncMock(  # noqa: SLF001
        return_value={"preflight": {"safety_verified": True}, "e2e_decision": "PASS"}
    ))

    result = await manager.setup()

    assert result["e2e_decision"] == "PASS"
    settings = load_toml_file(config_path)["hermes_referee"]
    assert settings["enabled"] is True
    assert settings["managed"] is True
    secret_path = tmp_path / "config-home" / "athena" / "secrets" / "HERMES_REFEREE_API_KEY"
    assert secret_path.exists()
    assert secret_path.stat().st_mode & 0o077 == 0
    assert "HERMES_REFEREE_API_KEY" in config_path.read_text(encoding="utf-8")
    assert secret_path.read_text(encoding="utf-8") not in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_setup_does_not_enable_when_live_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    config_path = tmp_path / "athena.toml"
    manager = HermesRefereeManager(
        config_path=config_path,
        runtime_root=tmp_path / "hermes",
    )
    api_server = tmp_path / "hermes" / "gateway" / "platforms" / "api_server.py"
    api_server.parent.mkdir(parents=True)
    (tmp_path / "hermes" / "hermes_cli").mkdir()
    (tmp_path / "hermes" / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    api_server.write_text('"tool_execution": "disabled"\n_referee_mode = True\n', encoding="utf-8")
    monkeypatch.setattr(HermesRefereeManager, "_run_hermes", AsyncMock())
    monkeypatch.setattr(HermesRefereeManager, "_probe", AsyncMock(  # noqa: SLF001
        side_effect=HermesRefereeManagerError("capability contract mismatch")
    ))

    with pytest.raises(HermesRefereeManagerError, match="contract mismatch"):
        await manager.setup()

    assert not config_path.exists()
    secret_dir = tmp_path / "config-home" / "athena" / "secrets"
    assert not secret_dir.exists()
