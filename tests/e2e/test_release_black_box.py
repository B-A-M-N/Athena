"""Release acceptance against the installed Athena wheel and source archive.

This test deliberately leaves the source checkout off ``sys.path``.  It builds
both publish artifacts, installs each into a temporary prefix, invokes the
installed CLI, and runs the same canonical Task through the service, HTTP/SSE,
ACP, approval, cancellation, persistence, policy, execution, and artifact
paths.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.dsh_release
@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_claim("ATHENA-EXT-016")
@pytest.mark.athena_evidence("e2e")
def test_installed_artifacts_cover_application_entry_paths(tmp_path: Path) -> None:
    """Build, install, and exercise both release artifacts outside the tree."""
    repo = Path(__file__).resolve().parents[2]
    artifact_dir = tmp_path / "dist"
    artifact_dir.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--sdist",
            "--outdir",
            str(artifact_dir),
        ],
        cwd=repo,
        check=True,
    )

    artifacts = sorted(artifact_dir.iterdir())
    wheel = next((path for path in artifacts if path.suffix == ".whl"), None)
    sdist = next((path for path in artifacts if path.suffix == ".gz"), None)
    assert wheel is not None, f"wheel missing from {artifacts!r}"
    assert sdist is not None, f"sdist missing from {artifacts!r}"

    for artifact in (wheel, sdist):
        prefix = tmp_path / (artifact.stem.replace(".", "-") + "-env")
        subprocess.run(
            [
                sys.executable,
                "-m", "venv", str(prefix),
            ],
            cwd=tmp_path,
            check=True,
        )
        venv_python = prefix / "bin" / "python"
        subprocess.run(
            [
                str(venv_python),
                "-m", "pip", "install", f"{artifact}[api,cli]",
            ],
            cwd=tmp_path,
            check=True,
        )
        purelib = _installed_purelib(prefix)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(purelib)
        env.pop("PYTHONHOME", None)

        cli = prefix / "bin" / "athena"
        cli_help = subprocess.run(
            [str(cli), "--help"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert cli_help.returncode == 0, cli_help.stderr
        assert "Usage" in cli_help.stdout
        cli_workspace = tmp_path / f"{artifact.stem}.cli-workspace"
        cli_workspace.mkdir()
        cli_sessions = subprocess.run(
            [str(cli), "--db", str(tmp_path / f"{artifact.stem}.cli.db"), "--workspace", str(cli_workspace), "sessions"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert cli_sessions.returncode == 0, cli_sessions.stderr
        assert "no sessions" in cli_sessions.stdout.lower()

        artifact_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        try:
            subprocess.run(
                [
                    str(venv_python),
                    "-c",
                    _installed_acceptance_program(),
                    str(purelib),
                    artifact_digest,
                    str(artifact),
                    str(repo),
                    _installed_api_server_program(),
                ],
                cwd=tmp_path,
                env=env,
                check=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                "installed application acceptance exceeded 45 seconds; "
                "the target lifecycle did not terminate"
            ) from exc


def _installed_purelib(prefix: Path) -> Path:
    """Locate the target interpreter's purelib directory under a prefix."""
    matches = list(prefix.glob("lib/python*/site-packages"))
    assert len(matches) == 1, f"unexpected installed layout: {matches!r}"
    return matches[0]


def _installed_acceptance_program() -> str:
    """Return the subprocess program that imports only the installed package."""
    return textwrap.dedent(
        r'''
        import asyncio
        import hashlib
        import json
        import os
        from pathlib import Path
        import re
        import socket
        import subprocess
        import sys
        from importlib.metadata import distribution


        import httpx

        purelib = Path(sys.argv[1]).resolve()
        expected_artifact_digest = sys.argv[2]
        artifact_path = Path(sys.argv[3]).resolve()
        checkout = Path(sys.argv[4]).resolve()
        api_server_program = sys.argv[5]
        sys.path.insert(0, str(purelib))

        assert all(
            (entry_path := Path(entry or ".").resolve()) != checkout
            and checkout not in entry_path.parents
            for entry in sys.path
            if entry
        ), sys.path

        import athena
        import click
        import starlette
        import uvicorn
        from athena.acp.adapter import ACPAdapter, ACPRequest
        from athena.api.app import create_app
        from athena.artifacts.store import ArtifactStore
        from athena.mcp.adapter import MCPAdapter
        from athena.mcp.client import MCPToolRef, MCPToolResult
        from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
        from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
        from athena.service.config import AthenaConfig, ProviderConfig
        from athena.service.service import AthenaService

        installed_file = Path(athena.__file__).resolve()
        assert purelib in installed_file.parents, installed_file
        metadata = distribution("athena-agent")
        requires = {re.split(r"[<=>!~;\[]", requirement, maxsplit=1)[0].strip().lower() for requirement in metadata.requires or ()}
        assert {"starlette", "click", "uvicorn"} <= requires
        assert purelib in Path(click.__file__).resolve().parents
        assert purelib in Path(starlette.__file__).resolve().parents
        assert purelib in Path(uvicorn.__file__).resolve().parents

        def terminal(marker):
            return {
                "match": {"user_contains": marker},
                "respond": {"text": marker + "_DONE", "done": True},
            }

        def execute(marker, code):
            return {
                "match": {"user_contains": marker},
                "respond": {
                    "capability_call": {
                        "capability_id": "execute",
                        "arguments": {"language": "shell", "code": code},
                    }
                },
            }

        async def wait_status(service, task_id, wanted, *, tries=300):
            for _ in range(tries):
                status = await service.get_task_status(task_id)
                if status == wanted:
                    return status
                await asyncio.sleep(0.02)
            return await service.get_task_status(task_id)

        async def events_for(service, task_id):
            """Collect a terminal event stream without allowing a bad task to hang release."""
            return await asyncio.wait_for(
                collect(service.stream_events(task_id)),
                timeout=10.0,
            )

        async def stop_checked(service):
            """Make a teardown defect fail the release lane instead of hanging it."""
            try:
                await asyncio.wait_for(service.stop(), timeout=10.0)
            except asyncio.TimeoutError as exc:
                raise AssertionError("AthenaService.stop() did not return within 10 seconds") from exc

        class InstalledMCPClient:
            """Small connected MCP transport double for the installed projection."""

            connection_id = "release-mcp"
            connected = True

            async def call_tool(self, name, arguments):
                assert name == "release_echo"
                assert arguments == {"message": "MCP_OK"}
                return MCPToolResult(is_error=False, content="MCP_OK")

        async def run():
            scripts = [
                {"match": {"capability_result_ok": True},
                 "respond": {"text": "CAPABILITY_OK", "done": True}},
                {"match": {"capability_result_ok": False},
                 "respond": {"text": "CAPABILITY_DENIED", "done": True}},
                execute("RELEASE_EXECUTION", "printf release-execution"),
                execute("RELEASE_APPROVAL", "printf release-approval"),
                execute("RELEASE_CANCEL", "sleep 10"),
                {
                    "match": {"user_contains": "RELEASE_POLICY_DENY"},
                    "respond": {"capability_call": {
                        "capability_id": "fs",
                        "arguments": {"operation": "read", "path": "/etc/passwd"},
                    }},
                },
                terminal("RELEASE_CANONICAL"),
                terminal("RELEASE_HTTP"),
                terminal("RELEASE_ACP"),
                terminal("RELEASE_PERSIST"),
                terminal("RELEASE_MCP_BOOT"),
            ]
            service = AthenaService.in_memory(extra_scripts=scripts)
            try:
                await service.start()
                assert isinstance(service._mcp, MCPAdapter)
                assert service._registry is not None
                assert service._dispatcher is not None
                mcp_descriptor = service._mcp.register_tool(
                    MCPToolRef(
                        name="release_echo",
                        description="Return a release acceptance marker",
                        input_schema={
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                        annotations={"readOnlyHint": True},
                    ),
                    connection_id="release-mcp",
                    client=InstalledMCPClient(),
                    server_alias="release",
                )
                assert mcp_descriptor.id in service._mcp.capability_ids()
                mcp_task = await service.submit(
                    AgentRequest(prompt="RELEASE_MCP_BOOT"), wait=False
                )
                mcp_result = await service._dispatcher.dispatch(
                    CapabilityRequest(
                        capability_id=mcp_descriptor.id,
                        arguments={"message": "MCP_OK"},
                        task_id=mcp_task.id,
                    ),
                    workspace=service._default_workspace,
                    profile=AutonomyLevel.AUTONOMOUS.value,
                )
                assert mcp_result.status is CapabilityResultStatus.OK
                assert mcp_result.output == "MCP_OK"
                # Exercise the installed API through an external uvicorn
                # process as well as the in-process ASGI projection below.
                with socket.socket() as probe_socket:
                    probe_socket.bind(("127.0.0.1", 0))
                    external_port = probe_socket.getsockname()[1]
                external = subprocess.Popen(
                    [sys.executable, "-c", api_server_program, str(external_port)],
                    cwd=Path.cwd(),
                    env=dict(os.environ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{external_port}") as external_client:
                        for _ in range(100):
                            try:
                                if (await external_client.get("/v1/health")).status_code == 200:
                                    break
                            except Exception:
                                if external.poll() is not None:
                                    stdout, stderr = external.communicate()
                                    raise AssertionError(
                                        f"external installed API server exited {external.returncode}: {stderr or stdout}"
                                    )
                                await asyncio.sleep(0.05)
                        else:
                            external.terminate()
                            stdout, stderr = external.communicate(timeout=5)
                            raise AssertionError(
                                "installed API server did not become ready: "
                                f"exit={external.returncode}; stderr={stderr or stdout}"
                            )
                        external_submitted = await external_client.post(
                            "/v1/tasks", json={"prompt": "RELEASE_HTTP_EXTERNAL"}
                        )
                        assert external_submitted.status_code == 202, external_submitted.text
                        external_task_id = external_submitted.json()["task_id"]
                        for _ in range(300):
                            external_status = await external_client.get(f"/v1/tasks/{external_task_id}")
                            if external_status.json()["task"]["status"] == TaskStatus.COMPLETE.value:
                                break
                            await asyncio.sleep(0.02)
                        else:
                            raise AssertionError("external installed API task did not complete")
                        external_sse = await external_client.get(
                            f"/v1/tasks/{external_task_id}/events", headers={"Last-Event-ID": "0"}
                        )
                        assert external_sse.status_code == 200
                        assert '"done": true' in external_sse.text
                        reconnect = await external_client.get(
                            f"/v1/tasks/{external_task_id}/events", headers={"Last-Event-ID": "1"}
                        )
                        assert reconnect.status_code == 200
                finally:
                    external.terminate()
                    try:
                        external.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        external.kill()
                        external.wait(timeout=10)
                # Canonical Task and real ExecutionManager operation.
                canonical = await service.submit(
                    AgentRequest(prompt="RELEASE_CANONICAL"), wait=True
                )
                assert await service.get_task_status(canonical.id) == TaskStatus.COMPLETE.value

                execution = await service.submit(
                    AgentRequest(
                        prompt="RELEASE_EXECUTION",
                        autonomy=AutonomyLevel.AUTONOMOUS,
                    ),
                    wait=True,
                )
                assert await service.get_task_status(execution.id) == TaskStatus.COMPLETE.value
                execution_events = await events_for(service, execution.id)
                assert any(event.type == "CapabilityCompleted" for event in execution_events)

                # Policy denial must be observable without an executed effect.
                denied = await service.submit(
                    AgentRequest(prompt="RELEASE_POLICY_DENY"), wait=False
                )
                assert await wait_status(service, denied.id, TaskStatus.COMPLETE.value) == TaskStatus.COMPLETE.value
                denied_events = await events_for(service, denied.id)
                assert any(
                    event.type == "CapabilityFailed"
                    and event.payload.get("reason") == "denied"
                    for event in denied_events
                )
                assert not any(event.type == "CapabilityCompleted" for event in denied_events)

                app = create_app(service)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://release.test",
                ) as client:
                    health = await client.get("/v1/health")
                    assert health.status_code == 200
                    assert health.json()["status"] == "ok"

                    submitted = await client.post(
                        "/v1/tasks", json={"prompt": "RELEASE_HTTP"}
                    )
                    assert submitted.status_code == 202, submitted.text
                    http_task_id = submitted.json()["task_id"]
                    assert await wait_status(
                        service, http_task_id, TaskStatus.COMPLETE.value
                    ) == TaskStatus.COMPLETE.value

                    fetched = await client.get(f"/v1/tasks/{http_task_id}")
                    assert fetched.status_code == 200
                    assert fetched.json()["task"]["status"] == TaskStatus.COMPLETE.value
                    result = await client.get(f"/v1/tasks/{http_task_id}/result")
                    assert result.status_code == 200
                    assert "result" in result.json()

                    sse = await client.get(
                        f"/v1/tasks/{http_task_id}/events",
                        headers={"Last-Event-ID": "0"},
                    )
                    assert sse.status_code == 200
                    assert "event:" in sse.text
                    assert '"done": true' in sse.text

                    approval = await service.submit(
                        AgentRequest(prompt="RELEASE_APPROVAL"), wait=False
                    )
                    assert await wait_status(
                        service, approval.id, TaskStatus.WAITING_APPROVAL.value
                    ) == TaskStatus.WAITING_APPROVAL.value
                    approval_id = await service.pending_approval_id(approval.id)
                    assert approval_id
                    approved = await client.post(
                        f"/v1/approvals/{approval_id}",
                        json={"granted": True, "scope": "call"},
                    )
                    assert approved.status_code == 200, approved.text
                    assert await wait_status(
                        service, approval.id, TaskStatus.COMPLETE.value
                    ) == TaskStatus.COMPLETE.value

                # Cancellation is exercised while the real shell runtime is busy.
                cancellable = await service.submit(
                    AgentRequest(
                        prompt="RELEASE_CANCEL",
                        autonomy=AutonomyLevel.AUTONOMOUS,
                    ),
                    wait=False,
                )
                assert await wait_status(
                    service, cancellable.id, TaskStatus.RUNNING.value
                ) == TaskStatus.RUNNING.value
                await service.cancel(cancellable.id)
                assert await wait_status(
                    service, cancellable.id, TaskStatus.CANCELLED.value
                ) == TaskStatus.CANCELLED.value

                # ACP submits into the same task/session stores and observes the
                # canonical event stream. Start streaming before enqueueing so
                # the replay cursor cannot skip the terminal event.
                acp = ACPAdapter(
                    service._task_manager,
                    service._sessions,
                    event_store=service._store_events,
                    stream_poll_interval=0.01,
                    stream_timeout=5.0,
                )
                acp_task_id = "release-acp-task"
                acp_stream = asyncio.create_task(
                    collect(acp.stream(acp_task_id))
                )
                await asyncio.sleep(0)
                accepted = await acp.submit(
                    ACPRequest(objective="RELEASE_ACP", task_id=acp_task_id)
                )
                assert accepted.type == "task.accepted"
                assert accepted.task_id == acp_task_id
                assert await wait_status(
                    service, acp_task_id, TaskStatus.COMPLETE.value
                ) == TaskStatus.COMPLETE.value
                acp_events = await asyncio.wait_for(acp_stream, timeout=10.0)
                assert any(event.type == "task.finished" for event in acp_events)
                assert all(event.task_id == acp_task_id for event in acp_events)

                # Durable persistence/restart keeps the completed Task visible.
                persist_root = Path(artifact_path).parent / (
                    "persist-" + expected_artifact_digest[:12]
                )
                persist_root.mkdir()
                persist_config = AthenaConfig(
                    db_path=str(persist_root / "athena.db"),
                    workspace_root=str(persist_root / "workspace"),
                    artifact_root=str(persist_root / "artifacts"),
                    providers=(ProviderConfig(
                        kind="fake",
                        name="fake",
                        extra={"scripts": [terminal("RELEASE_PERSIST")]},
                    ),),
                )
                first = AthenaService(config=persist_config)
                await first.start()
                persisted = await first.submit(
                    AgentRequest(prompt="RELEASE_PERSIST"), wait=True
                )
                persisted_id = persisted.id
                await stop_checked(first)
                second = AthenaService(config=persist_config)
                await second.start()
                try:
                    assert await second.get_task_status(persisted_id) == TaskStatus.COMPLETE.value
                finally:
                    await stop_checked(second)

                # The reviewed release artifact is stored by its real digest.
                artifact_bytes = artifact_path.read_bytes()
                artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
                assert artifact_digest == expected_artifact_digest
                store = ArtifactStore(root=Path(artifact_path).parent / "artifact-store")
                ref = await store.save(
                    content=artifact_bytes,
                    metadata={"candidate_artifact_digest": expected_artifact_digest},
                )
                assert ref.hash == expected_artifact_digest
                assert await store.load(ref) == artifact_bytes
                listed = await store.list()
                assert any(
                    item.hash == expected_artifact_digest
                    and item.metadata.get("candidate_artifact_digest") == expected_artifact_digest
                    for item in listed
                )
            finally:
                await stop_checked(service)

        async def collect(iterator):
            return [event async for event in iterator]

        asyncio.run(run())
        '''
    )


def _installed_api_server_program() -> str:
    """Return a child process that serves the installed API over uvicorn."""
    return textwrap.dedent(
        r'''
        import asyncio
        import sys

        import uvicorn

        from athena.api.app import create_app
        from athena.service.service import AthenaService

        def terminal(marker):
            return {
                "match": {"user_contains": marker},
                "respond": {"text": marker + "_DONE", "done": True},
            }

        async def main():
            service = AthenaService.in_memory(extra_scripts=[terminal("RELEASE_HTTP_EXTERNAL")])
            await service.start()
            server = uvicorn.Server(uvicorn.Config(
                create_app(service),
                host="127.0.0.1",
                port=int(sys.argv[1]),
                log_level="error",
            ))
            try:
                await server.serve()
            finally:
                await service.stop()

        asyncio.run(main())
        '''
    )
