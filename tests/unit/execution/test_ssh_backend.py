import base64
import json
import socket
import subprocess
import sys
import time

import pytest

from athena.execution.ssh import (
    SSHBackend,
    SSHProfile,
    _REMOTE_PYTHON_SUPERVISOR_V2,
)


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
        if process.poll() is not None:
            error = process.stderr.read()
            if "Operation not permitted" in error:
                pytest.skip("sandbox does not permit local Unix socket binds")
            raise AssertionError(error)
        assert socket_path.exists()
        token = token_path.read_text(encoding="utf-8").strip()

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(socket_path))
        connection.sendall((json.dumps({"token": token}) + "\n").encode())
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
        shutdown.sendall((json.dumps({"token": token, "op": "shutdown"}) + "\n").encode())
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
