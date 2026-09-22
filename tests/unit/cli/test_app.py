"""CLI composition-root and run-status tests."""

from types import SimpleNamespace

import pytest

from athena.cli.app import (
    Options,
    _arg_parse,
    _cmd_run,
    _cmd_workflows,
    _config_set,
    build_config,
)
from athena.protocol.errors import ModelProviderUnconfigured
from athena.protocol.tasks import AutonomyLevel, TaskStatus


def test_build_config_auto_wires_openrouter_free_router(monkeypatch):
    monkeypatch.delenv("FREEINFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("FREEINFERENCE_MODEL", raising=False)
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


def test_build_config_auto_wires_freeinference_glm_flash(monkeypatch):
    monkeypatch.setenv("FREEINFERENCE_API_KEY", "test-only-secret")
    monkeypatch.delenv("FREEINFERENCE_MODEL", raising=False)
    monkeypatch.delenv("FREEINFERENCE_API_BASE_URL", raising=False)
    monkeypatch.delenv("FREEINFERENCE_API_ENDPOINT", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-fallback-secret")

    config = build_config(Options(config_path="/tmp/athena-test-no-config.toml"))

    assert len(config.providers) == 1
    provider = config.providers[0]
    assert provider.kind == "openai-compat"
    assert provider.name == "freeinference"
    assert provider.model == "deepseek-v4-flash"
    assert provider.base_url == "http://127.0.0.1:18769/v1"
    assert provider.credential_id == "FREEINFERENCE_API_KEY"
    assert provider.api_key is None


def test_build_config_honors_openrouter_model_override(monkeypatch):
    monkeypatch.delenv("FREEINFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("FREEINFERENCE_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")

    config = build_config(Options(config_path="/tmp/athena-test-no-config.toml"))

    assert config.providers[0].model == "google/gemma-4-31b-it:free"


def test_build_config_rejects_paid_openrouter_override(monkeypatch):
    monkeypatch.delenv("FREEINFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("FREEINFERENCE_MODEL", raising=False)
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


def test_argparse_workflows_preserves_actions():
    options = _arg_parse(["workflows", "describe", "workflow-1", "task-1"])

    assert options.command == "workflows"
    assert options.args == ["describe", "workflow-1", "task-1"]


@pytest.mark.asyncio
async def test_workflow_command_renders_durable_view(capsys):
    class Service:
        async def list_workflows(self, *, task_id=None):
            assert task_id is None
            return [
                {
                    "id": "workflow-1",
                    "scope": "project",
                    "lifecycle_state": "ACTIVE",
                    "enabled": True,
                    "name": "release procedure",
                }
            ]

    assert await _cmd_workflows(Options(command="workflows", args=["list"]), Service()) == 0
    assert "workflow-1\tproject\tACTIVE\tenabled\trelease procedure" in capsys.readouterr().out


def test_argparse_config_set_preserves_operator_key_and_value():
    options = _arg_parse(["config", "set", "hermes-referee.enabled", "true"])

    assert options.command == "config"
    assert options.config_action == "set"
    assert options.config_key == "hermes-referee.enabled"
    assert options.config_value == "true"


def test_argparse_referee_accepts_explicit_supervision_policy():
    options = _arg_parse(["referee", "setup", "--self-host-supervision", "advisory"])

    assert options.command == "referee"
    assert options.referee_action == "setup"
    assert options.referee_supervision == "advisory"


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
                config_key="hermes-referee.self-host-supervision",
                config_value="advisory",
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
        "self_host_supervision": "advisory",
    }


def test_config_set_reports_the_actual_boolean_field(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    result = _config_set(
        Options(
            command="config",
            config_action="set",
            config_key="hermes-referee.allow-remote",
            config_value="sometimes",
        )
    )

    assert result == 2
    assert "hermes-referee.allow-remote expects true or false" in capsys.readouterr().err


def test_config_set_rejects_raw_credentials_in_structured_values(capsys, tmp_path):
    result = _config_set(
        Options(
            command="config",
            config_action="set",
            config_path=str(tmp_path / "config.toml"),
            config_key="providers",
            config_value='[{"kind":"openai-compat","api_key":"raw-secret"}]',
        )
    )

    assert result == 2
    assert "raw credentials" in capsys.readouterr().err


def test_config_set_allows_secret_env_references(tmp_path):
    path = tmp_path / "config.toml"
    result = _config_set(
        Options(
            command="config",
            config_action="set",
            config_path=str(path),
            config_key="mcp_servers",
            config_value='[{"name":"local","secret_env":{"API_KEY":"ATHENA_API_KEY"}}]',
        )
    )

    assert result == 0


class _RunSurface:
    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def render_user_message(self, text: str) -> None:
        del text

    def render_notice(self, text: str, **kwargs) -> None:
        del text, kwargs

    def render_result(self, text: str, **kwargs) -> None:
        del text, kwargs


class _RunService:
    def __init__(self, status: TaskStatus | None = None, *, ready: bool = True) -> None:
        self.status = status
        self.ready = ready
        self.submit_calls = 0
        self.config = SimpleNamespace(autonomy_level=AutonomyLevel.AUTONOMOUS)

    def require_agent_ready(self, request=None) -> None:
        del request
        if not self.ready:
            raise ModelProviderUnconfigured("No model provider is configured.")

    async def submit(self, request, *, wait=False):
        del request, wait
        self.submit_calls += 1
        return SimpleNamespace(id="task-run")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        (TaskStatus.COMPLETE, 0),
        (TaskStatus.FAILED, 1),
        (TaskStatus.PARTIAL, 1),
        (TaskStatus.CANCELLED, 130),
    ],
)
async def test_run_maps_terminal_status_to_exit_code(monkeypatch, tmp_path, status, expected_exit):
    import athena.cli.chat as chat

    monkeypatch.setattr(chat, "_make_surface", lambda **kwargs: _RunSurface())

    async def fake_stream_task(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(summary="done", status=status, usage=None)

    monkeypatch.setattr(chat, "stream_task", fake_stream_task)
    service = _RunService(status)
    code = await _cmd_run(
        Options(command="run", args=["hello"], workspace=str(tmp_path)),
        service,
    )

    assert code == expected_exit
    assert service.submit_calls == 1


@pytest.mark.asyncio
async def test_run_rejects_unconfigured_service_before_constructing_surface(
    monkeypatch, tmp_path, capsys
):
    import athena.cli.chat as chat

    constructed = []
    monkeypatch.setattr(chat, "_make_surface", lambda **kwargs: constructed.append(kwargs))
    service = _RunService(ready=False)

    code = await _cmd_run(
        Options(command="run", args=["hello"], workspace=str(tmp_path)),
        service,
    )

    assert code == 2
    assert service.submit_calls == 0
    assert constructed == []
    assert "No model provider is configured" in capsys.readouterr().err
