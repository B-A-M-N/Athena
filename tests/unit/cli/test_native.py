import asyncio
from types import SimpleNamespace

import pytest

from athena.cli.app import Options, _arg_parse
from athena.cli.native import worker_command
from athena.cli.native_session import NativeSession, parse_args


def test_native_command_is_available_in_argparse_fallback():
    options = _arg_parse(["native", "--workspace", "/tmp/project"])

    assert options.command == "native"
    assert options.workspace == "/tmp/project"


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


def test_native_session_parser_accepts_presentation_controls():
    options = parse_args(["--mascot", "cat", "--no-animations", "--reduced-motion"])

    assert options.mascot == "cat"
    assert options.animations is False
    assert options.reduced_motion is True


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
