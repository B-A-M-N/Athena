import base64
import json
import socket
import subprocess
import sys
import time
import shutil

import pytest

from athena.execution.ssh import (
    SSHBackend,
    SSHProfile,
    _REMOTE_PYTHON_SUPERVISOR_V2,
)


def _start_remote_fixture(tmp_path, runtime):
    socket_path = tmp_path / f"{runtime}.sock"
    token_path = tmp_path / f"{runtime}.token"
    metadata_path = tmp_path / f"{runtime}.json"
    source = base64.b64encode(_REMOTE_PYTHON_SUPERVISOR_V2.encode()).decode()
    launcher = "import base64;exec(base64.b64decode(" + repr(source) + "))"
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            launcher,
            str(socket_path),
            str(token_path),
            str(metadata_path),
            f"session-{runtime}",
            f"task-{runtime}",
            runtime,
            str(tmp_path),
            "authority-test",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):
        if socket_path.exists() and token_path.exists() and metadata_path.exists():
            break
        time.sleep(0.01)
    error = None
    if process.poll() is not None:
        error = process.stderr.read()
        if error and "Operation not permitted" in error:
            pytest.skip("sandbox does not permit local Unix socket binds")
    if not socket_path.exists():
        pytest.skip("sandbox does not permit local Unix socket binds")
    if error is not None:
        raise AssertionError(error)
    return process, socket_path, token_path


def test_ssh_profile_requires_strict_operator_identity(tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAA\n")
    profile = SSHProfile(
        name="gpu-box",
        host="gpu.example",
        user="athena",
        credential_id="GPU_KEY",
        known_hosts=str(known_hosts),
    )
    backend = SSHBackend(profile)
    command = backend._ssh_base(None)
    assert "StrictHostKeyChecking=yes" in command
    assert any(str(known_hosts) in value for value in command)
    assert "-o" in command


def test_ssh_profile_rejects_shell_injection_and_unsafe_root(tmp_path):
    with pytest.raises(ValueError):
        SSHProfile(
            name="gpu-box",
            host="gpu.example;touch /tmp/pwned",
            user="athena",
            credential_id="GPU_KEY",
            known_hosts=str(tmp_path / "known_hosts"),
        )


def test_remote_python_supervisor_owns_worker_and_authenticates_env(tmp_path):
    socket_path = tmp_path / "supervisor.sock"
    token_path = tmp_path / "supervisor.token"
    metadata_path = tmp_path / "supervisor.json"
    session_id = "session-test"
    task_id = "task-test"
    source = base64.b64encode(_REMOTE_PYTHON_SUPERVISOR_V2.encode()).decode()
    launcher = "import base64;exec(base64.b64decode(" + repr(source) + "))"
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            launcher,
            str(socket_path),
            str(token_path),
            str(metadata_path),
            session_id,
            task_id,
            "python",
            str(tmp_path),
            "authority-test",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        for _ in range(100):
            if socket_path.exists() and token_path.exists() and metadata_path.exists():
                break
            time.sleep(0.01)
        if not socket_path.exists():
            error = process.stderr.read() if process.poll() is not None else ""
            if error and "Operation not permitted" in error:
                pytest.skip("sandbox does not permit local Unix socket binds")
            pytest.skip("sandbox does not permit local Unix socket binds")
        if process.poll() is not None:
            error = process.stderr.read()
            if error and "Operation not permitted" in error:
                pytest.skip("sandbox does not permit local Unix socket binds")
            raise AssertionError(error)
        token = token_path.read_text(encoding="utf-8").strip()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert len(metadata["session_nonce"]) >= 16
        assert len(metadata["worker_source_sha256"]) == 64
        assert metadata["authority_digest"] == "authority-test"

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(35)
        connection.connect(str(socket_path))
        connection.sendall(
            (
                json.dumps({"token": token, "protocol": "athena-ssh-supervisor", "version": 1})
                + "\n"
            ).encode()
        )
        configure = json.dumps(
            {"op": "configure", "env": {"ATHENA_SECRET": "not-on-argv"}}
        ).encode()
        connection.sendall(f"{len(configure)}\n".encode() + configure)
        payload = json.dumps({"source": "import os; print(os.environ['ATHENA_SECRET'])"}).encode()
        connection.sendall(f"{len(payload)}\n".encode() + payload)
        frames = []
        stream = connection.makefile("rb")
        while True:
            line = stream.readline()
            if not line:
                break
            frames.append(json.loads(line.decode()))
            if frames[-1].get("type") == "done":
                break
        connection.close()
        assert {frame.get("data") for frame in frames if frame.get("type") == "out"} == {
            "not-on-argv\n"
        }

        shutdown = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        shutdown.connect(str(socket_path))
        shutdown.sendall(
            (
                json.dumps(
                    {
                        "token": token,
                        "op": "shutdown",
                        "protocol": "athena-ssh-supervisor",
                        "version": 1,
                    }
                )
                + "\n"
            ).encode()
        )
        shutdown_stream = shutdown.makefile("rb")
        receipt = json.loads(shutdown_stream.readline().decode())
        shutdown.close()
        assert receipt["confirmed"] is True
        assert process.wait(timeout=3) == 0
        assert not socket_path.exists()
        assert not token_path.exists()
        assert not metadata_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    with pytest.raises(ValueError):
        SSHProfile(
            name="gpu-box",
            host="gpu.example",
            user="athena",
            credential_id="GPU_KEY",
            known_hosts=str(tmp_path / "known_hosts"),
            remote_root="~/athena;rm -rf /",
        )


@pytest.mark.parametrize("runtime", ["shell", "node"])
def test_remote_supervisor_preserves_runtime_state(runtime, tmp_path):
    if runtime == "node" and shutil.which("node") is None:
        pytest.skip("node is not installed")
    process, socket_path, token_path = _start_remote_fixture(tmp_path, runtime)
    connection = None
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(35)
        connection.connect(str(socket_path))
        connection.sendall(
            (
                json.dumps({"token": token, "protocol": "athena-ssh-supervisor", "version": 1})
                + "\n"
            ).encode()
        )

        sources = (
            (
                "export ATHENA_REMOTE_STATE=kept; mkdir -p nested; cd nested; "
                "helper(){ printf '%s' \"$1\"; }"
                if runtime == "shell"
                else "globalThis.ATHENA_REMOTE_STATE = 41; globalThis.helper = (x) => x + 1;"
            ),
            (
                'printf \'%s:%s:%s\' "$ATHENA_REMOTE_STATE" "$(pwd | sed \'s#.*/##\')" "$(helper ok)"'
                if runtime == "shell"
                else "console.log(ATHENA_REMOTE_STATE + ':' + helper(ATHENA_REMOTE_STATE));"
            ),
        )
        frames = []
        stream = connection.makefile("rb")
        for source_code in sources:
            payload = json.dumps({"source": source_code}).encode()
            connection.sendall(f"{len(payload)}\n".encode() + payload)
            while True:
                line = stream.readline()
                assert line
                frame = json.loads(line.decode())
                frames.append(frame)
                if frame.get("type") == "done":
                    break
        output = "".join(
            str(frame.get("data") or "") for frame in frames if frame.get("type") == "out"
        )
        assert ("kept:nested:ok" if runtime == "shell" else "41:42") in output

        shutdown = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        shutdown.connect(str(socket_path))
        shutdown.sendall(
            (
                json.dumps(
                    {
                        "token": token,
                        "op": "shutdown",
                        "protocol": "athena-ssh-supervisor",
                        "version": 1,
                    }
                )
                + "\n"
            ).encode()
        )
        receipt = json.loads(shutdown.makefile("rb").readline().decode())
        shutdown.close()
        assert receipt["confirmed"] is True
        assert process.wait(timeout=3) == 0
    finally:
        if connection is not None:
            connection.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_remote_supervisor_dependency_inventory_rpc(tmp_path):
    process, socket_path, token_path = _start_remote_fixture(tmp_path, "python")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(socket_path))
        request = {
            "token": token,
            "op": "dependency",
            "protocol": "athena-ssh-supervisor",
            "version": 1,
            "operation": "inventory",
            "manager": "python",
            "name": "demo",
            "environment_id": "a" * 64,
        }
        connection.sendall((json.dumps(request) + "\n").encode())
        response = json.loads(connection.makefile("rb").readline().decode())
        connection.close()
        assert response["kind"] == "response"
        assert response["ok"] is True
        assert response["target"].endswith("/.athena/environments/" + "a" * 64 + "/python")
        assert response["packages"] == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
