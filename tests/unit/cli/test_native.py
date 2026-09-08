import asyncio
from types import SimpleNamespace

import pytest

from athena.cli.app import Options, _arg_parse
from athena.cli.native import (
    NativePreflight,
    _load_credential_env,
    native_binary,
    native_preflight,
    worker_command,
)
from athena.cli.native_session import NativeSession, parse_args


def test_native_command_is_available_in_argparse_fallback():
    options = _arg_parse(["native", "--workspace", "/tmp/project"])

    assert options.command == "native"
    assert options.workspace == "/tmp/project"


def test_native_doctor_target_is_available_in_argparse_fallback():
    options = _arg_parse(["doctor", "native"])

    assert options.command == "doctor"
    assert options.args == ["native"]


def test_native_preflight_reports_missing_display(monkeypatch):
    monkeypatch.setenv("DISPLAY", "")
    monkeypatch.setattr("athena.cli.native.platform.system", lambda: "Linux")
    monkeypatch.setattr("athena.cli.native.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("athena.cli.native.platform.libc_ver", lambda: ("glibc", "2.36"))
    monkeypatch.setattr("athena.cli.native.ctypes.util.find_library", lambda name: name)
    monkeypatch.setattr("athena.cli.native._display_is_usable", lambda *_args: True)

    result = native_preflight()

    assert isinstance(result, NativePreflight)
    assert not result.ok
    assert result.failures == ("DISPLAY is not set",)


def test_native_launch_rejects_incompatible_host_before_spawning(monkeypatch, tmp_path, capsys):
    from athena.cli import native

    binary = tmp_path / "athena-terminal"
    binary.write_bytes(b"native")
    binary.chmod(0o755)
    monkeypatch.setattr(native, "native_binary", lambda: binary)
    monkeypatch.setattr(
        native, "native_preflight", lambda: NativePreflight(("DISPLAY is not set",))
    )
    monkeypatch.setattr(
        native.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned")),
    )

    assert native.launch(SimpleNamespace()) == 2
    assert "requires Linux x86_64 GNU/glibc >= 2.34" in capsys.readouterr().err


def test_native_worker_command_forwards_scope_without_credentials():
    options = Options(
        command="native",
        config_path="/tmp/athena.toml",
        db_path="/tmp/athena.db",
        workspace="/tmp/project",
        autonomy="coding",
        model="openrouter/free",
        criteria="command:pytest -q;report exists",
        verbose=True,
        mascot="owl",
        animations=False,
        reduced_motion=True,
    )

    command = worker_command(options)

    assert command[:3] == [command[0], "-m", "athena.cli.native_session"]
    assert "--workspace" in command
    assert "--model" in command
    assert "OPENROUTER_API_KEY" not in command
    assert command[-5:] == ["--verbose", "--mascot", "owl", "--no-animations", "--reduced-motion"]


def test_native_loads_only_supported_credential_settings_without_executing_file(
    tmp_path, monkeypatch
):
    env_file = tmp_path / "opencodex.env"
    env_file.write_text(
        "FREEINFERENCE_API_KEY=test-key\n"
        "FREEINFERENCE_API_ENDPOINT=https://fi.example/v1\n"
        "FREEINFERENCE_MODEL=glm-5.3-flash\n"
        "ATHENA_NATIVE_INJECTED=should-not-load\n"
        "$(touch %s)=not-shell\n" % (tmp_path / "should-not-exist"),
        encoding="utf-8",
    )
    monkeypatch.setenv("ATHENA_CREDENTIAL_ENV_FILE", str(env_file))

    env = {"ATHENA_CREDENTIAL_ENV_FILE": str(env_file)}
    _load_credential_env(env)

    assert env == {
        "ATHENA_CREDENTIAL_ENV_FILE": str(env_file),
        "FREEINFERENCE_API_KEY": "test-key",
        "FREEINFERENCE_API_ENDPOINT": "https://fi.example/v1",
        "FREEINFERENCE_MODEL": "glm-5.3-flash",
    }
    assert not (tmp_path / "should-not-exist").exists()


def test_native_session_parser_matches_worker_contract():
    options = parse_args(
        [
            "--db",
            "/tmp/athena.db",
            "--workspace",
            "/tmp/project",
            "--autonomy",
            "coding",
            "--criteria",
            "tests pass",
        ]
    )

    assert options.command == "native"
    assert options.db_path == "/tmp/athena.db"
    assert options.workspace == "/tmp/project"
    assert options.autonomy == "coding"
    assert options.criteria == "tests pass"


def test_native_binary_does_not_default_to_debug_checkout(monkeypatch):
    monkeypatch.delenv("ATHENA_NATIVE_BIN", raising=False)

    binary = native_binary()

    assert "target/debug" not in str(binary)


def test_native_session_parser_accepts_presentation_controls():
    options = parse_args(["--mascot", "cat", "--no-animations", "--reduced-motion"])

    assert options.mascot == "cat"
    assert options.animations is False
    assert options.reduced_motion is True


@pytest.mark.asyncio
async def test_native_transcript_does_not_expose_internal_task_id(capsys):
    session = NativeSession(parse_args([]))

    class Service:
        async def submit(self, request, wait=False):
            assert wait is False
            return SimpleNamespace(id="internal-task-secret")

        async def stream_events(self, task_id, after_sequence=0):
            assert task_id == "internal-task-secret"
            assert after_sequence == 0
            if False:
                yield None

        async def get_result(self, task_id):
            assert task_id == "internal-task-secret"
            return SimpleNamespace(summary="completed")

    session.service = Service()
    await session._submit("inspect workspace")

    output = capsys.readouterr().out
    assert "internal-task-secret" not in output
    assert "YOU\ninspect workspace" in output
    assert "ATHENA\ncompleted" in output


@pytest.mark.asyncio
async def test_native_submissions_reuse_session_until_new_command():
    session = NativeSession(parse_args([]))
    requests = []

    class Service:
        async def submit(self, request, wait=False):
            requests.append(request)
            session_id = request.session_id or "native-session"
            return SimpleNamespace(id=f"task-{len(requests)}", session_id=session_id)

        async def stream_events(self, task_id, after_sequence=0):
            del task_id, after_sequence
            if False:
                yield None

        async def get_result(self, task_id):
            return SimpleNamespace(summary=f"done {task_id}")

    session.service = Service()
    await session._submit("first fact")
    await session._submit("second fact")

    assert requests[0].session_id is None
    assert requests[1].session_id == "native-session"
    assert session.session_id == "native-session"

    session.session_id = "session-to-clear"
    assert await session._dispatch_command("/new")
    assert session.session_id is None


@pytest.mark.asyncio
async def test_native_resume_adopts_resumed_task_session():
    session = NativeSession(parse_args([]))

    class Service:
        async def resume_task(self, task_id):
            assert task_id == "task-resume"
            return SimpleNamespace(id=task_id, session_id="resumed-session")

        async def stream_events(self, task_id, after_sequence=0):
            assert task_id == "task-resume"
            assert after_sequence == 0
            if False:
                yield None

        async def get_result(self, task_id):
            return SimpleNamespace(summary=f"resumed {task_id}")

    session.service = Service()
    assert await session._dispatch_command("/resume task-resume")
    assert session.session_id == "resumed-session"
    assert session._last_task_id == "task-resume"


@pytest.mark.asyncio
async def test_native_shared_approval_commands_forward_grant_scope_and_deny():
    session = NativeSession(parse_args([]))
    calls = []

    class Service:
        async def approve(self, approval_id, *, granted, scope=None):
            calls.append((approval_id, granted, scope))

    session.service = Service()
    assert await session._dispatch_command("/approve apr-1 task")
    assert await session._dispatch_command("/deny apr-2")

    assert calls == [("apr-1", True, "task"), ("apr-2", False, None)]


@pytest.mark.asyncio
async def test_native_projection_debounce_drains_event_arriving_during_send():
    session = NativeSession(parse_args([]))
    session._projection_interval = 0.001
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    sends: list[str] = []

    async def send_projection() -> None:
        sends.append(session.projection.status)
        if len(sends) == 1:
            send_started.set()
            await release_send.wait()

    session._send_projection = send_projection  # type: ignore[method-assign]
    session._projection_dirty = True
    worker = asyncio.create_task(session._debounced_projection())

    await asyncio.wait_for(send_started.wait(), timeout=1)
    await session._on_event(SimpleNamespace(type="TaskStarted", payload={}, task_id=None))
    release_send.set()
    await asyncio.wait_for(worker, timeout=1)

    assert len(sends) == 2
    assert session._projection_dirty is False


@pytest.mark.asyncio
async def test_native_ctrl_c_cancels_foreground_task_but_keeps_session_state():
    session = NativeSession(parse_args([]))
    cancelled: list[str] = []

    class Service:
        async def cancel(self, task_id: str) -> None:
            cancelled.append(task_id)

    session.service = Service()
    session._foreground_task_id = "task-ctrl-c"
    session._foreground_task = asyncio.create_task(asyncio.sleep(10))

    await session._cancel_foreground()

    assert cancelled == ["task-ctrl-c"]
    assert session._foreground_task is None
    assert session._foreground_task_id is None


@pytest.mark.asyncio
async def test_native_scroll_emits_retained_oi_navigation_control():
    session = NativeSession(parse_args([]))

    assert await session._dispatch_command("/scroll oi up 3")
    assert session._navigation == {"pane": "oi", "direction": "up", "amount": 3}

    assert await session._dispatch_command("/scroll oi bottom")
    assert session._navigation == {"pane": "oi", "direction": "bottom", "amount": 1}
