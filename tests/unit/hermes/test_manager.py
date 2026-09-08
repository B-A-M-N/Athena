from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from athena.hermes.agent_adapter import HermesRefereeSafetyError
from athena.hermes.manager import (
    HermesRefereeDisconnectedError,
    HermesRefereeManager,
    HermesRefereeManagerError,
)
from athena.policy.credentials import write_user_secret
from athena.service.config import load_toml_file, write_toml_atomic_private


def _fake_hermes_root(tmp_path):
    root = tmp_path / "hermes"
    api_server = root / "gateway" / "platforms" / "api_server.py"
    api_server.parent.mkdir(parents=True)
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    api_server.write_text('"tool_execution": "disabled"\n_referee_mode = True\n', encoding="utf-8")
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_mode", "expected_mode"),
    ((None, "required"), ("advisory", "advisory")),
)
async def test_setup_enables_only_after_probe_and_keeps_key_in_user_store(
    tmp_path, monkeypatch, configured_mode, expected_mode
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    config_path = tmp_path / "athena.toml"
    manager = HermesRefereeManager(
        config_path=config_path,
        runtime_root=tmp_path / "hermes",
        self_host_supervision=configured_mode,
    )
    _fake_hermes_root(tmp_path)
    monkeypatch.setattr(HermesRefereeManager, "_run_hermes", AsyncMock())
    monkeypatch.setattr(
        HermesRefereeManager,
        "_probe",
        AsyncMock(  # noqa: SLF001
            return_value={"preflight": {"safety_verified": True}, "e2e_decision": "PASS"}
        ),
    )

    result = await manager.setup()

    assert result["e2e_decision"] == "PASS"
    settings = load_toml_file(config_path)["hermes_referee"]
    assert settings["enabled"] is True
    assert settings["managed"] is True
    assert settings["self_host_supervision"] == expected_mode
    assert "required_for_self_host" not in settings
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
    _fake_hermes_root(tmp_path)
    monkeypatch.setattr(HermesRefereeManager, "_run_hermes", AsyncMock())
    monkeypatch.setattr(
        HermesRefereeManager,
        "_probe",
        AsyncMock(  # noqa: SLF001
            side_effect=HermesRefereeManagerError("capability contract mismatch")
        ),
    )

    with pytest.raises(HermesRefereeManagerError, match="contract mismatch"):
        await manager.setup()

    assert not config_path.exists()
    secret_dir = tmp_path / "config-home" / "athena" / "secrets"
    assert not secret_dir.exists()


@pytest.mark.asyncio
async def test_setup_restores_durable_state_and_reports_unconfirmed_compensation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    config_path = tmp_path / "athena.toml"
    manager = HermesRefereeManager(
        config_path=config_path,
        runtime_root=tmp_path / "hermes",
    )
    _fake_hermes_root(tmp_path)
    run_hermes = AsyncMock(
        side_effect=[None, HermesRefereeManagerError("permission denied stopping Hermes")]
    )
    monkeypatch.setattr(HermesRefereeManager, "_run_hermes", run_hermes)
    monkeypatch.setattr(
        HermesRefereeManager,
        "_probe",
        AsyncMock(return_value={"preflight": {"safety_verified": True}}),
    )
    monkeypatch.setattr(
        HermesRefereeManager,
        "_write_settings",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = await manager.setup()

    assert result["configuration_commit"] == "failed"
    assert result["managed_service_state"] == "stop_unconfirmed"
    assert result["recovery_required"] is True
    assert result["recommended_action"] == "athena referee repair"
    assert not config_path.exists()
    assert not list((tmp_path / "config-home" / "athena" / "secrets").glob("*"))
    assert run_hermes.await_count == 2
    assert run_hermes.await_args_list[1].args[0][-2:] == ["gateway", "stop"]


@pytest.mark.asyncio
async def test_setup_commit_failure_restores_previous_config_and_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    config_path = tmp_path / "athena.toml"
    previous_config = b'[athena]\nautonomy = "supervised"\n'
    config_path.write_bytes(previous_config)
    write_user_secret("HERMES_REFEREE_API_KEY", "previous-key")
    manager = HermesRefereeManager(
        config_path=config_path,
        runtime_root=tmp_path / "hermes",
    )
    _fake_hermes_root(tmp_path)
    monkeypatch.setattr(HermesRefereeManager, "_run_hermes", AsyncMock())
    monkeypatch.setattr(
        HermesRefereeManager,
        "_probe",
        AsyncMock(return_value={"preflight": {"safety_verified": True}}),
    )
    monkeypatch.setattr(
        HermesRefereeManager,
        "_write_settings",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(HermesRefereeManagerError, match="configuration_commit"):
        await manager.setup()

    assert config_path.read_bytes() == previous_config
    assert (tmp_path / "config-home" / "athena" / "secrets" / "HERMES_REFEREE_API_KEY").read_text(
        encoding="utf-8"
    ) == "previous-key"


@pytest.mark.asyncio
async def test_disable_reports_unconfirmed_shutdown_but_persists_local_disable(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "athena.toml"
    root = _fake_hermes_root(tmp_path)
    write_toml_atomic_private(
        config_path,
        {
            "hermes_referee": {
                "enabled": True,
                "managed": True,
                "runtime_root": str(root),
                "self_host_supervision": "required",
            }
        },
    )
    monkeypatch.setattr(
        HermesRefereeManager,
        "_run_hermes",
        AsyncMock(side_effect=HermesRefereeManagerError("unexpected stop failure")),
    )

    result = await HermesRefereeManager(config_path=config_path).disable()

    assert result["enabled"] is False
    assert result["integration_state"] == "disabled"
    assert result["managed_service"] == "stop_unconfirmed"
    assert result["warning"]
    assert load_toml_file(config_path)["hermes_referee"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("off", "advisory", "required"))
async def test_disable_stops_transport_and_repair_restores_it_with_same_policy(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    config_path = tmp_path / "athena.toml"
    root = _fake_hermes_root(tmp_path)
    write_toml_atomic_private(
        config_path,
        {
            "hermes_referee": {
                "enabled": True,
                "managed": True,
                "endpoint": "http://127.0.0.1:8643",
                "profile": "athena-referee",
                "runtime_root": str(root),
                "credential_id": "HERMES_REFEREE_API_KEY",
                "self_host_supervision": mode,
            }
        },
    )
    monkeypatch.setattr(HermesRefereeManager, "_run_hermes", AsyncMock())
    monkeypatch.setattr(
        HermesRefereeManager,
        "_probe",
        AsyncMock(return_value={"preflight": {"safety_verified": True}}),
    )
    manager = HermesRefereeManager(config_path=config_path)

    await manager.disable()

    disabled = load_toml_file(config_path)["hermes_referee"]
    assert disabled["enabled"] is False
    assert disabled["self_host_supervision"] == mode
    assert (await HermesRefereeManager(config_path=config_path).status())["state"] == "disabled"

    await manager.repair()

    repaired = load_toml_file(config_path)["hermes_referee"]
    assert repaired["enabled"] is True
    assert repaired["self_host_supervision"] == mode


def test_existing_key_does_not_fall_back_from_explicit_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    write_user_secret("HERMES_REFEREE_API_KEY", "fallback-key")
    manager = HermesRefereeManager(credential_id="MY_HERMES_KEY")

    assert manager._existing_key("MY_HERMES_KEY") is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_status_reports_missing_explicit_credential_as_error(tmp_path, monkeypatch):
    config_path = tmp_path / "athena.toml"
    write_toml_atomic_private(
        config_path,
        {
            "hermes_referee": {
                "enabled": True,
                "credential_id": "MY_HERMES_KEY",
                "self_host_supervision": "advisory",
            }
        },
    )

    result = await HermesRefereeManager(config_path=config_path).status()

    assert result["state"] == "error"
    assert result["safety_verified"] is False
    assert "MY_HERMES_KEY" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_error", "expected_state"),
    (
        (HermesRefereeDisconnectedError("connection refused"), "disconnected"),
        (HermesRefereeSafetyError("unsafe profile"), "unsafe"),
    ),
)
async def test_status_distinguishes_disconnected_from_unsafe(
    tmp_path, monkeypatch, probe_error, expected_state
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    write_user_secret("HERMES_REFEREE_API_KEY", "key")
    config_path = tmp_path / "athena.toml"
    write_toml_atomic_private(
        config_path,
        {
            "hermes_referee": {
                "enabled": True,
                "credential_id": "HERMES_REFEREE_API_KEY",
                "self_host_supervision": "advisory",
            }
        },
    )
    monkeypatch.setattr(HermesRefereeManager, "_probe", AsyncMock(side_effect=probe_error))

    result = await HermesRefereeManager(config_path=config_path).status()

    assert result["state"] == expected_state
    assert result["safety_verified"] is False
