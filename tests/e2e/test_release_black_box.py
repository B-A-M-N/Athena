"""Release acceptance against the installed Athena wheel and source archive.

This test deliberately leaves the source checkout off ``sys.path``.  It builds
both publish artifacts, installs each into a temporary prefix, invokes the
installed CLI, and runs the same canonical Task through the service, HTTP/SSE,
ACP, approval, cancellation, persistence, policy, execution, and artifact
paths.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import zipfile

import pytest


@pytest.mark.dsh_release
@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_claim("ATHENA-EXT-016")
@pytest.mark.athena_evidence("e2e")
def test_installed_artifacts_cover_application_entry_paths(tmp_path: Path) -> None:
    """Install the exact hashed artifacts and exercise application entry paths."""
    repo = Path(__file__).resolve().parents[2]
    configured_artifacts = os.environ.get("ATHENA_RELEASE_ARTIFACT_DIR")
    artifact_dir = (
        Path(configured_artifacts).resolve() if configured_artifacts else repo / "release-artifacts"
    )
    if not artifact_dir.is_dir():
        artifact_dir = tmp_path / "dist"
        subprocess.run(
            [
                str(repo / "scripts" / "build-release-artifacts"),
                "--output-dir",
                str(artifact_dir),
            ],
            cwd=repo,
            check=True,
        )

    artifacts = sorted(artifact_dir.iterdir())
    wheel = next(
        (path for path in artifacts if path.suffix == ".whl" and "native" not in path.name),
        None,
    )
    sdist = next((path for path in artifacts if path.suffix == ".gz"), None)
    native_wheel = next(
        (path for path in artifacts if path.suffix == ".whl" and "native" in path.name), None
    )
    assert wheel is not None and "native" not in wheel.name
    assert wheel is not None, f"wheel missing from {artifacts!r}"
    assert sdist is not None, f"sdist missing from {artifacts!r}"
    assert native_wheel is not None, f"native companion wheel missing from {artifacts!r}"
    manifest = artifact_dir / "release-manifest.json"
    assert manifest.is_file(), "release manifest missing; acceptance must consume exact artifacts"
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_hashes = {
        Path(item["path"]).resolve(): item["sha256"] for item in manifest_data["artifacts"]
    }
    expected_artifacts = {wheel.resolve(), sdist.resolve(), native_wheel.resolve()}
    assert set(manifest_hashes) == expected_artifacts
    assert {path.resolve() for path in artifacts if path.name != "release-manifest.json"} == (
        expected_artifacts
    )
    for artifact in (wheel, sdist, native_wheel):
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == manifest_hashes.get(
            artifact.resolve()
        )

    def assert_wheel_contract(path: Path) -> None:
        with zipfile.ZipFile(path) as archive:
            contains_native = any(name.endswith("/athena-terminal") for name in archive.namelist())
        if contains_native and path.name.endswith("-py3-none-any.whl"):
            raise AssertionError(
                f"native payload must not be embedded in a universal wheel: {path.name}"
            )

    assert_wheel_contract(wheel)
    assert_wheel_contract(native_wheel)
    with zipfile.ZipFile(wheel) as archive:
        assert not any(name.endswith("/athena-terminal") for name in archive.namelist())
    with zipfile.ZipFile(native_wheel) as archive:
        assert any(name.endswith("/athena-terminal") for name in archive.namelist())
    assert "-py3-none-" in native_wheel.name
    assert not native_wheel.name.endswith("-py3-none-any.whl")
    offline_demo_config = tmp_path / "offline-demo.toml"
    offline_demo_config.write_text(
        """[[providers]]
kind = "fake"
name = "fake"
model = "fake-1"
scripts = [{match = {user_contains = "2+2"}, respond = {text = "4", done = true}}]
""",
        encoding="utf-8",
    )

    for artifact in (wheel, sdist):
        prefix = tmp_path / (artifact.stem.replace(".", "-") + "-env")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                str(prefix),
            ],
            cwd=tmp_path,
            check=True,
        )
        venv_python = prefix / "bin" / "python"
        install_env = os.environ.copy()
        install_env.pop("PYTHONPATH", None)
        install_env.pop("PYTHONHOME", None)
        subprocess.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                f"{artifact}[cli]",
                str(native_wheel),
            ],
            cwd=tmp_path,
            env=install_env,
            check=True,
        )
        purelib = _installed_purelib(prefix)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(purelib)
        env.pop("PYTHONHOME", None)
        # Release acceptance must exercise the deterministic built-in fake
        # provider. Do not let a developer's ambient provider credentials turn
        # this artifact test into a network/model-availability test.
        env.pop("OPENROUTER_API_KEY", None)
        env.pop("OPENROUTER_MODEL", None)

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
        empty_config = tmp_path / f"{artifact.stem}.empty.toml"
        empty_config.write_text("", encoding="utf-8")
        no_config_doctor = subprocess.run(
            [
                str(cli),
                "--db",
                str(tmp_path / f"{artifact.stem}.doctor.db"),
                "--workspace",
                str(cli_workspace),
                "--config",
                str(empty_config),
                "doctor",
                "startup",
            ],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        # A clean install must diagnose missing configuration explicitly. It
        # is a readiness failure, not a startup crash or silent fake-provider
        # substitution.
        assert no_config_doctor.returncode == 1, no_config_doctor.stderr
        assert "model_provider: unconfigured" in no_config_doctor.stdout

        invalid_config = tmp_path / f"{artifact.stem}.invalid.toml"
        invalid_config.write_text(
            '[[providers]]\nkind = "not-a-provider"\nname = "broken"\nmodel = "broken-1"\n',
            encoding="utf-8",
        )
        invalid_provider = subprocess.run(
            [
                str(cli),
                "--db",
                str(tmp_path / f"{artifact.stem}.invalid.db"),
                "--workspace",
                str(cli_workspace),
                "--config",
                str(invalid_config),
                "doctor",
                "startup",
            ],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert invalid_provider.returncode != 0
        diagnostic = f"{invalid_provider.stdout}\n{invalid_provider.stderr}".lower()
        assert "error" in diagnostic or "failed" in diagnostic

        packaged_native = purelib / "athena_native" / "athena-terminal"
        assert packaged_native.is_file() and os.access(packaged_native, os.X_OK)
        native_headless = subprocess.run(
            [str(packaged_native), "--headless", "--command", "printf installed-native"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert native_headless.returncode == 0, native_headless.stderr
        assert "installed-native" in native_headless.stdout
        cli_sessions = subprocess.run(
            [
                str(cli),
                "--db",
                str(tmp_path / f"{artifact.stem}.cli.db"),
                "--workspace",
                str(cli_workspace),
                "sessions",
            ],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert cli_sessions.returncode == 0, cli_sessions.stderr
        assert "no sessions" in cli_sessions.stdout.lower()
        cli_task = subprocess.run(
            [
                str(cli),
                "--db",
                str(tmp_path / f"{artifact.stem}.cli-task.db"),
                "--workspace",
                str(cli_workspace),
                "--config",
                str(offline_demo_config),
                "run",
                "2+2",
            ],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert cli_task.returncode == 0, cli_task.stderr
        assert "4" in cli_task.stdout
        assert "-> complete" in cli_task.stdout.lower()

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
        import importlib.util
        import json
        import os
        from pathlib import Path
        import re
        import socket
        import sqlite3
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
        from athena.capabilities.dispatcher import SuspendedCall
        from athena.mcp.adapter import MCPAdapter
        from athena.mcp.client import MCPToolRef, MCPToolResult
        from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
        from athena.protocol.capabilities import (
            CapabilityRequest,
            CapabilityRequestOrigin,
            CapabilityResult,
            CapabilityResultStatus,
        )
        from athena.service.config import AthenaConfig, ProviderConfig
        from athena.service.service import AthenaService

        installed_file = Path(athena.__file__).resolve()
        assert purelib in installed_file.parents, installed_file
        metadata = distribution("athena-agent")
        requires = {re.split(r"[<=>!~;\[]", requirement, maxsplit=1)[0].strip().lower() for requirement in metadata.requires or ()}
        assert {"starlette", "click", "uvicorn"} <= requires
        native_metadata = distribution("athena-agent-native")
        native_requires = {
            requirement.strip().lower()
            for requirement in native_metadata.requires or ()
        }
        assert f"athena-agent=={metadata.version}" in native_requires
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

                # Installed-artifact capability wiring: inventory the effective
                # fabric, then exercise one harmless operation per registered
                # family through the dispatcher. This intentionally uses the
                # installed service's fabric rather than importing capability
                # classes from the checkout or calling executors directly.
                expected_matrix = {
                    "core": {
                        "artifacts",
                        "capabilities",
                        "capability_health",
                        "context_blocks",
                        "diagnostics",
                        "execute",
                        "fs",
                        "git",
                        "memory",
                        "packs",
                        "skills",
                        "delegate",
                        "delegate.external",
                    },
                    "computational": {
                        "capsule",
                        "dependency",
                        "machine",
                        "observer",
                        "process",
                        "research",
                        "scratch",
                        "synthesis",
                        "truth",
                        "workflow",
                    },
                    "environment": {
                        "database",
                        "fusion",
                        "maintain",
                        "network",
                        "schedule",
                        "service",
                        "watch",
                        "workspace",
                    },
                }
                expected_core = set().union(*expected_matrix.values())
                assert len(expected_core) == sum(len(ids) for ids in expected_matrix.values())
                optional_capabilities = {
                    "terminal_session": {
                        "available": all(
                            importlib.util.find_spec(module) is not None
                            for module in ("pexpect", "pyte")
                        ),
                        "reason": "requires both pexpect and pyte",
                    },
                    "debugger": {
                        "available": importlib.util.find_spec("debugpy") is not None,
                        "reason": "requires the optional debugpy package",
                    },
                }
                effective = {
                    descriptor.id: descriptor
                    for descriptor in service._fabric.list_descriptors()
                }
                expected_effective = expected_core | {
                    capability_id
                    for capability_id, condition in optional_capabilities.items()
                    if condition["available"]
                }
                expected_effective.add(mcp_descriptor.id)
                assert set(effective) == expected_effective, {
                    "missing": sorted(expected_effective - set(effective)),
                    "unexpected": sorted(set(effective) - expected_effective),
                    "optional": optional_capabilities,
                }
                for capability_id, condition in optional_capabilities.items():
                    if not condition["available"]:
                        assert capability_id not in effective
                        assert condition["reason"]

                workspace_root = Path(service._default_workspace.root)
                sqlite_path = workspace_root / "release-capability-wiring.sqlite"
                sqlite3.connect(sqlite_path).close()
                subprocess.run(
                    ["git", "init", "--quiet", str(workspace_root)],
                    check=True,
                )
                capsule_body = {
                    "format": 1,
                    "capabilities": [],
                    "workflows": [],
                    "root_workflow_id": "",
                }
                capsule = dict(capsule_body)
                capsule["capsule_id"] = (
                    "capsule_"
                    + hashlib.sha256(
                        json.dumps(
                            capsule_body,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()[:24]
                )
                probes = {
                    "artifacts": {"operation": "list"},
                    "capabilities": {"operation": "search", "query": ""},
                    "capability_health": {"operation": "list"},
                    "capsule": {"operation": "inspect", "capsule": capsule},
                    "context_blocks": {"operation": "list"},
                    "database": {
                        "operation": "tables",
                        "path": str(sqlite_path),
                    },
                    "dependency": {"operation": "inspect", "name": "python"},
                    "diagnostics": {"operation": "normalize", "text": ""},
                    "execute": {
                        "language": "shell",
                        "code": "printf capability-wiring",
                    },
                    "fs": {"operation": "list", "path": "."},
                    "git": {"operation": "status"},
                    "machine": {"operation": "overview"},
                    "maintain": {"operation": "list"},
                    "memory": {"operation": "recall", "query": ""},
                    "network": {"operation": "listeners"},
                    "observer": {"operation": "list"},
                    "packs": {"operation": "search", "query": ""},
                    "process": {"operation": "list"},
                    "research": {"operation": "sources"},
                    "schedule": {"operation": "list"},
                    "scratch": {
                        "operation": "run",
                        "code": "def run(args):\n    return {'ok': True}",
                    },
                    "service": {"operation": "list"},
                    "skills": {"operation": "search", "query": ""},
                    "synthesis": {"operation": "candidates"},
                    "terminal_session": {"operation": "list"},
                    "truth": {"operation": "status"},
                    "watch": {"operation": "list"},
                    "workflow": {"operation": "list"},
                    "workspace": {"operation": "status"},
                }
                for capability_id, arguments in probes.items():
                    if capability_id not in effective:
                        continue
                    probe = await service._dispatcher.dispatch(
                        CapabilityRequest(
                            capability_id=capability_id,
                            arguments=arguments,
                            task_id=mcp_task.id,
                            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
                        ),
                        workspace=service._default_workspace,
                        profile=AutonomyLevel.AUTONOMOUS.value,
                    )
                    if capability_id == "service" and probe.status is CapabilityResultStatus.FAILED:
                        error = (probe.error or "").lower()
                        assert any(
                            marker in error
                            for marker in (
                                "system has not been booted with systemd",
                                "failed to connect to bus",
                            )
                        ), probe.error
                        continue
                    assert probe.status is CapabilityResultStatus.OK, {
                        "capability": capability_id,
                        "error": probe.error,
                    }
                if "delegate" in effective:
                    delegate_probe = await service._dispatcher.dispatch(
                        CapabilityRequest(
                            capability_id="delegate",
                            arguments={
                                "operation": "status",
                                "child_task_id": "missing-release-probe-child",
                            },
                            task_id=mcp_task.id,
                            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
                        ),
                        workspace=service._default_workspace,
                        profile=AutonomyLevel.AUTONOMOUS.value,
                    )
                    assert delegate_probe.status is CapabilityResultStatus.FAILED
                    assert "missing-release-probe-child" in (delegate_probe.error or "")
                if "debugger" in effective:
                    debugger_probe = await service._dispatcher.dispatch(
                        CapabilityRequest(
                            capability_id="debugger",
                            arguments={
                                "operation": "status",
                                "session": "missing-release-probe-session",
                            },
                            task_id=mcp_task.id,
                            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
                        ),
                        workspace=service._default_workspace,
                        profile=AutonomyLevel.AUTONOMOUS.value,
                    )
                    assert debugger_probe.status is CapabilityResultStatus.FAILED
                    assert "unowned debugger session" in (
                        debugger_probe.error or ""
                    ).lower()

                # Release-level compositional nervous-system scenario. Keep
                # every seam in this one started service: task-local scratch
                # observations feed synthesis, the validated generated tool
                # is reached through the fabric and dispatcher, and a
                # workflow invokes a nested capability through that same
                # dispatcher boundary. The workflow uses a read-only native
                # child so its own approval does not obscure the composition
                # assertion with a second independent approval continuation.
                composition_task_id = "release-composition-task"
                await service._store_tasks.insert_task(
                    composition_task_id,
                    None,
                    None,
                    "release compositional capability scenario",
                    autonomy="autonomous",
                    workspace=service._default_workspace,
                )

                async def dispatch_composed(capability_id, arguments, call_id):
                    def request():
                        return CapabilityRequest(
                            capability_id=capability_id,
                            arguments=arguments,
                            task_id=composition_task_id,
                            call_id=call_id,
                            origin=CapabilityRequestOrigin.MODEL,
                        )

                    result = await service._dispatcher.dispatch(
                        request(),
                        workspace=service._default_workspace,
                        profile=service.config.autonomy_level,
                    )
                    if isinstance(result, SuspendedCall):
                        assert result.approval_id
                        await service.approve(
                            result.approval_id,
                            granted=True,
                            scope="task",
                        )
                        result = await service._dispatcher.dispatch(
                            request(),
                            workspace=service._default_workspace,
                            profile=service.config.autonomy_level,
                        )
                    assert isinstance(result, CapabilityResult), result
                    return result

                scratch_code = (
                    "def run(args):\n"
                    "    return {'value': args['value']}\n"
                )
                scratch_first = await dispatch_composed(
                    "scratch",
                    {
                        "operation": "run",
                        "code": scratch_code,
                        "args": {"value": "release-one"},
                    },
                    "release-compose-scratch-1",
                )
                assert scratch_first.status is CapabilityResultStatus.OK
                scratch_id = scratch_first.metadata["scratch_id"]
                scratch_second = await dispatch_composed(
                    "scratch",
                    {
                        "operation": "run",
                        "scratch_id": scratch_id,
                        "args": {"value": "release-two"},
                    },
                    "release-compose-scratch-2",
                )
                assert scratch_second.status is CapabilityResultStatus.OK
                promoted = await dispatch_composed(
                    "synthesis",
                    {"operation": "promote_scratch", "scratch_id": scratch_id},
                    "release-compose-promote",
                )
                assert promoted.status is CapabilityResultStatus.OK
                promoted_payload = json.loads(promoted.output)
                assert promoted_payload["capability_id"] == scratch_id
                assert promoted_payload["status"] == "task_reusable"

                generated = await dispatch_composed(
                    scratch_id,
                    {"value": "release-generated"},
                    "release-compose-generated",
                )
                assert generated.status is CapabilityResultStatus.OK
                assert json.loads(generated.output) == {"value": "release-generated"}

                workflow_created = await dispatch_composed(
                    "workflow",
                    {
                        "operation": "create",
                        "name": "release_nested_capability",
                        "description": "release compositional nested capability",
                        "steps": [
                            {
                                "id": "inventory",
                                "capability": "capabilities",
                                "arguments": {
                                    "operation": "search",
                                    "query": "",
                                },
                            }
                        ],
                    },
                    "release-compose-workflow-create",
                )
                assert workflow_created.status is CapabilityResultStatus.OK
                workflow_id = workflow_created.metadata["workflow_id"]
                workflow_run = await dispatch_composed(
                    "workflow",
                    {"operation": "run", "workflow_id": workflow_id},
                    "release-compose-workflow-run",
                )
                assert workflow_run.status is CapabilityResultStatus.OK
                workflow_payload = json.loads(workflow_run.output)
                assert workflow_payload["status"] == "completed"
                assert workflow_payload["outputs"]["inventory"]

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
                    admission=service.require_agent_ready,
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
                persist_root = Path.cwd() / ("persist-" + expected_artifact_digest[:12])
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
                store = ArtifactStore(root=Path.cwd() / "artifact-store")
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
        r"""
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
        """
    )
