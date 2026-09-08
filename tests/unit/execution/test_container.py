from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from athena.execution.container import ContainerBackend
from athena.execution.manager import ExecutionManager
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
    ExecutionRequest,
)
from athena.protocol.tasks import NetworkPolicy


def _completed(args: list[str], *, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args, 0, stdout, stderr)


def test_container_availability_requires_reachable_daemon(monkeypatch):
    monkeypatch.setattr("athena.execution.container.shutil.which", lambda _: "/usr/bin/docker")

    def runner(args, **kwargs):
        return _completed(args, stdout="27.0")

    backend = ContainerBackend(runner=runner)
    assert backend.available() is True


def test_container_command_is_read_only_and_network_fail_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("athena.execution.container.shutil.which", lambda _: "/usr/bin/docker")
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        if args[1] == "info":
            return _completed(args, stdout="27.0")
        if args[1:3] == ["image", "inspect"]:
            digest = "sha256:" + "a" * 64
            return _completed(
                args,
                stdout=json.dumps(
                    {
                        "Id": digest,
                        "RepoDigests": [f"python@{digest}"],
                    }
                ),
            )
        return _completed(args, stdout="container-id\n")

    backend = ContainerBackend(runner=runner)
    container_id = backend._create_container(  # noqa: SLF001 - command contract test
        task_id="task-1",
        workspace_root=str(tmp_path),
        network_policy=NetworkPolicy.RESTRICTED,
    )

    command = calls[-1]
    assert container_id == "container-id"
    assert "--read-only" in command
    assert "--interactive" in command
    assert "--network" in command
    assert command[command.index("--network") + 1] == "none"
    mount = command[command.index("--mount") + 1]
    assert "target=/workspace" in mount
    assert "readonly" in mount
    assert command[command.index("--read-only") + 1] == "--tmpfs"
    assert command[command.index("--workdir") + 1] == "/workspace"
    image = "python@sha256:" + "a" * 64
    image_index = command.index(image)
    assert command[image_index + 1 : image_index + 4] == ["python", "-u", "-c"]
    assert backend.environment_identity() == {
        "image": "python@sha256:" + "a" * 64,
        "image_digest": "sha256:" + "a" * 64,
    }


def test_container_resolves_local_image_id_when_no_repository_digest(monkeypatch):
    monkeypatch.setattr("athena.execution.container.shutil.which", lambda _: "/usr/bin/docker")

    def runner(args, **kwargs):
        if args[1] == "info":
            return _completed(args, stdout="27.0")
        digest = "sha256:" + "b" * 64
        return _completed(args, stdout=json.dumps({"Id": digest, "RepoDigests": []}))

    backend = ContainerBackend(runner=runner)
    assert backend._resolve_image() == ("sha256:" + "b" * 64, "sha256:" + "b" * 64)  # noqa: SLF001


@pytest.mark.asyncio
async def test_container_reattach_proves_identity_and_rejects_tampering(
    tmp_path: Path, monkeypatch
):
    container_id = "container-proven"
    digest = "sha256:" + "d" * 64
    record = {
        "id": "container-session",
        "task_id": "task-reattach",
        "backend": "container",
        "runtime": "python",
        "cwd": "/workspace",
        "workspace_identity": str(tmp_path),
        "network_policy": "deny",
        "process_identity": container_id,
        "start_identity": "2026-09-07T12:00:00.000000000Z",
        "runtime_version": digest,
        "metadata": {
            "container_id": container_id,
            "workspace_root": str(tmp_path),
            "env": {"ATHENA_TEST": "1"},
            "image_digest": digest,
        },
    }
    inspected = {
        "Id": container_id,
        "Config": {
            "Image": "python@" + digest,
            "Labels": {
                "athena.session_id": "container-session",
                "athena.task_id": "task-reattach",
                "athena.backend": "container",
                "athena.runtime": "python",
                "athena.workspace_identity": str(tmp_path),
                "athena.network_policy": "deny",
                "athena.image_digest": digest,
            },
        },
        "State": {"Running": True, "StartedAt": record["start_identity"]},
        "HostConfig": {"NetworkMode": "none"},
        "Mounts": [
            {"Destination": "/workspace", "Source": str(tmp_path), "RW": False},
        ],
    }
    backend = ContainerBackend()
    monkeypatch.setattr(backend, "_inspect_container", lambda _container_id: inspected)
    monkeypatch.setattr(
        "athena.execution.container._DockerPythonSession.start", lambda _session: None
    )

    assert await backend.reattach_session(record) == "container-session"
    assert backend._sessions["container-session"].container_id == container_id  # noqa: SLF001

    tampered = {**record, "start_identity": "2026-09-07T12:01:00.000000000Z"}
    with pytest.raises(RuntimeError, match="start identity"):
        backend._reattach(tampered)  # noqa: SLF001


@pytest.mark.asyncio
async def test_execution_manager_routes_registered_backend():
    class FakeBackend:
        name = "container"

        def __init__(self):
            self.closed = False

        def available(self):
            return True

        async def create_session(self, **kwargs):
            return "container-session"

        async def execute(self, request):
            execution_id = request.metadata["__execution_id"]
            yield ExecutionEvent(
                type=ExecutionEventType.STARTED,
                execution_id=execution_id,
                metadata={
                    "runtime_session_id": "container-session",
                    "image_digest": "sha256:" + "c" * 64,
                },
            )
            yield ExecutionEvent(
                type=ExecutionEventType.STDOUT,
                execution_id=execution_id,
                data="inside container\n",
            )
            yield ExecutionEvent(
                type=ExecutionEventType.EXITED,
                execution_id=execution_id,
                exit_status=ExecutionExitStatus.EXITED,
                exit_code=0,
            )

        async def interrupt(self, execution_id):
            return None

        async def destroy_session(self, runtime_session_id):
            return None

        async def shutdown(self):
            self.closed = True

    backend = FakeBackend()
    events = []

    async def sink(event):
        events.append(event)

    manager = ExecutionManager(event_sink=sink)
    manager.register_backend(backend)
    result = await manager.execute(
        ExecutionRequest(
            runtime="python",
            source="print('ok')",
            task_id="task-1",
            workspace_id="workspace-1",
            backend="container",
        )
    )

    assert result.status is ExecutionExitStatus.EXITED
    assert result.stdout == "inside container\n"
    assert any(
        event.type == "ExecutionExited"
        and event.payload.get("metadata", {}).get("runtime_session_id") == "container-session"
        and event.payload.get("metadata", {}).get("image_digest") == "sha256:" + "c" * 64
        for event in events
    )
    await manager.close_all()
    assert backend.closed is True
