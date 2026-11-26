"""Authenticated SSH transport and remote-supervisor mechanics.

The transport owns remote command construction and supervisor RPCs. It does not
own execution/session indexes or backend selection; those remain in SSHBackend.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
import re
from typing import Any

from athena.execution.ssh_protocol import PROTOCOL_NAME, PROTOCOL_VERSION
from athena.execution.ssh_remote_runtime import _REMOTE_RELAY, _REMOTE_PYTHON_SUPERVISOR_V2

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_REMOTE_PATH = re.compile(r"^[A-Za-z0-9_./~-]+$")


class SSHTransport:
    """Own authenticated remote transport operations for one operator profile."""

    def __init__(
        self, profile: Any, *, secret_manager: Any = None, ssh_command: str = "ssh"
    ) -> None:
        self.profile = profile
        self._secrets = secret_manager
        self.ssh_command = ssh_command

    def available(self) -> bool:
        return bool(
            shutil.which(self.ssh_command)
            and os.path.isfile(os.path.expanduser(self.profile.known_hosts))
        )

    def credential_path(self, task_id: str) -> str | None:
        if self.profile.identity_file:
            path = os.path.realpath(os.path.expanduser(self.profile.identity_file))
            if not os.path.isfile(path):
                raise RuntimeError(f"SSH identity file does not exist: {path}")
            return path
        if self._secrets is None:
            raise RuntimeError("SSH credential requires the configured SecretManager")
        value = self._secrets.resolve(
            self.profile.credential_id,
            owner_task=task_id,
            backend=self.profile.name,
        )
        if not value:
            raise RuntimeError("SSH credential resolved to an empty value")
        if os.path.isfile(os.path.expanduser(value)):
            return os.path.realpath(os.path.expanduser(value))
        if "BEGIN" not in value:
            raise RuntimeError("SSH credential must resolve to a private key or key path")
        handle = tempfile.NamedTemporaryFile(prefix="athena-ssh-", mode="w", delete=False)
        try:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(value)
            handle.flush()
            return handle.name
        finally:
            handle.close()

    def target_root(self, task_id: str) -> str:
        if not _SAFE_SEGMENT.fullmatch(str(task_id)):
            raise ValueError("task id cannot be used as an SSH workspace segment")
        return self.profile.remote_root.rstrip("/") + "/" + str(task_id)

    def ssh_base(self, key_path: str | None) -> list[str]:
        known_hosts = os.path.realpath(os.path.expanduser(self.profile.known_hosts))
        if not os.path.isfile(known_hosts):
            raise RuntimeError(f"SSH known_hosts file does not exist: {known_hosts}")
        command = [
            self.ssh_command,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            f"ConnectTimeout={int(self.profile.connect_timeout)}",
            "-p",
            str(self.profile.port),
        ]
        if key_path:
            command.extend(("-i", key_path, "-o", "IdentitiesOnly=yes"))
        command.append(f"{self.profile.user}@{self.profile.host}")
        return command

    @staticmethod
    def _encoded_command(program: str, source: str, cwd: str) -> str:
        encoded = base64.b64encode(source.encode()).decode("ascii")
        # The remote shell receives only fixed interpreter text and a
        # base64-encoded worker; model source travels later over the worker
        # protocol, never as an SSH command argument.
        if program == "python":
            runner = f"python3 -u -c \"import base64;exec(base64.b64decode('{encoded}'))\""
        else:
            runner = f"node -e \"eval(Buffer.from('{encoded}','base64').toString())\""
        return f"mkdir -p -- {cwd!s} && cd -- {cwd!s} && exec {runner}"

    @staticmethod
    def remote_arg(path: str) -> str:
        """Render a validated remote path while preserving ``~/`` expansion."""
        value = str(path)
        if value.startswith("~/"):
            return "$HOME/" + value[2:]
        return shlex.quote(value)

    def supervisor_paths(self, task_id: str, session_id: str) -> dict[str, str]:
        base = self.target_root(task_id).rstrip("/") + "/.athena-supervisor/" + session_id
        return {
            "socket": base + ".sock",
            "token": base + ".token",
            "metadata": base + ".json",
        }

    def run_remote_command(
        self,
        command: str,
        *,
        key_path: str | None,
        timeout: float | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [*self.ssh_base(key_path), command],
            capture_output=True,
            text=True,
            timeout=timeout or self.profile.connect_timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "remote command failed").strip()
            raise RuntimeError(f"SSH remote supervisor command failed: {detail[-1000:]}")
        return completed

    def start_remote_python_supervisor(
        self,
        *,
        task_id: str,
        session_id: str,
        remote_cwd: str,
        key_path: str | None,
        runtime: str = "python",
        authority_digest: str = "",
    ) -> dict[str, str]:
        paths = self.supervisor_paths(task_id, session_id)
        root = self.remote_arg(remote_cwd)
        socket_path = self.remote_arg(paths["socket"])
        token_path = self.remote_arg(paths["token"])
        metadata_path = self.remote_arg(paths["metadata"])
        encoded = base64.b64encode(_REMOTE_PYTHON_SUPERVISOR_V2.encode()).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"mkdir -p -- {root} {self.remote_arg(paths['socket'].rsplit('/', 1)[0])}; "
            f"nohup python3 -u -c {shlex.quote(launcher)} {socket_path} {token_path} "
            f"{metadata_path} {shlex.quote(session_id)} {shlex.quote(task_id)} {shlex.quote(runtime)} {root} "
            f"{shlex.quote(authority_digest)} "
            "></dev/null >/dev/null 2>&1 &"
        )
        self.run_remote_command(command, key_path=key_path)
        for _ in range(50):
            probe = self.run_remote_command(
                f"test -s {metadata_path} && cat {metadata_path}",
                key_path=key_path,
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                try:
                    metadata = json.loads(probe.stdout)
                except json.JSONDecodeError:
                    metadata = None
                if isinstance(metadata, dict) and metadata.get("session_id") == session_id:
                    if (
                        metadata.get("protocol") != PROTOCOL_NAME
                        or int(metadata.get("protocol_version", -1)) != PROTOCOL_VERSION
                    ):
                        raise RuntimeError("remote supervisor protocol is unsupported")
                    return {str(key): str(value) for key, value in metadata.items()}
            time.sleep(0.1)
        raise RuntimeError("remote Python supervisor did not become ready")

    def remote_relay_command(self, paths: Mapping[str, str]) -> str:
        encoded = base64.b64encode(_REMOTE_RELAY.encode()).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        return (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self.remote_arg(paths['socket_path'])} {self.remote_arg(paths['token_path'])}"
        )

    def remote_describe(
        self,
        *,
        socket_path: str,
        token_path: str,
        key_path: str | None,
    ) -> Mapping[str, Any]:
        encoded = base64.b64encode(
            (
                "import json,socket,sys; "
                "p,t=sys.argv[1:]; token=open(t).read().strip(); "
                "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(p); "
                "s.sendall((json.dumps({'token':token,'op':'describe','protocol':'athena-ssh-supervisor','version':1})+'\\n').encode()); "
                "print(s.recv(65536).decode().strip()); s.close()"
            ).encode()
        ).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self.remote_arg(socket_path)} {self.remote_arg(token_path)}"
        )
        completed = self.run_remote_command(
            command,
            key_path=key_path,
            check=False,
            timeout=self.profile.connect_timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError("remote supervisor describe failed")
        try:
            value = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("remote supervisor describe response is invalid") from exc
        if (
            not isinstance(value, Mapping)
            or value.get("kind") != "response"
            or value.get("protocol") != PROTOCOL_NAME
            or int(value.get("protocol_version", -1)) != PROTOCOL_VERSION
        ):
            raise RuntimeError("remote supervisor describe response is not authoritative")
        return value

    def remote_dependency(
        self,
        *,
        socket_path: str,
        token_path: str,
        key_path: str | None,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Call the authenticated remote dependency RPC with JSON only."""
        encoded_request = base64.b64encode(
            json.dumps(dict(request), separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        client = (
            "import base64,json,socket,sys; "
            "p,t,b=sys.argv[1:]; token=open(t,encoding='utf-8').read().strip(); "
            "payload=json.loads(base64.b64decode(b)); payload.update({'token':token,'op':'dependency','protocol':'athena-ssh-supervisor','version':1}); "
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(p); "
            "s.sendall((json.dumps(payload,separators=(',',':'))+'\\n').encode()); "
            "print(s.makefile('rb').readline().decode().strip()); s.close()"
        )
        encoded = base64.b64encode(client.encode("utf-8")).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self.remote_arg(socket_path)} {self.remote_arg(token_path)} "
            f"{shlex.quote(encoded_request)}"
        )
        completed = self.run_remote_command(
            command,
            key_path=key_path,
            check=False,
            timeout=max(self.profile.connect_timeout, 30.0),
        )
        try:
            response = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "remote dependency RPC response is invalid: "
                + (completed.stderr or completed.stdout or "")[-500:]
            ) from exc
        if not isinstance(response, Mapping) or response.get("kind") != "response":
            raise RuntimeError("remote dependency RPC response is not authoritative")
        return response

    def remote_interrupt(self, session: Any) -> bool:
        if not session.remote_socket or not session.remote_token_path:
            return False
        encoded = base64.b64encode(
            (
                "import json,socket,sys; "
                "p,t=sys.argv[1:]; token=open(t).read().strip(); "
                "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(p); "
                "s.sendall((json.dumps({'token':token,'op':'interrupt','protocol':'athena-ssh-supervisor','version':1})+'\\n').encode()); "
                "print(s.recv(65536).decode().strip()); s.close()"
            ).encode()
        ).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self.remote_arg(session.remote_socket)} {self.remote_arg(session.remote_token_path)}"
        )
        completed = self.run_remote_command(
            command, key_path=session.key_path, check=False, timeout=self.profile.connect_timeout
        )
        try:
            response = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (IndexError, TypeError, json.JSONDecodeError):
            return False
        return bool(response.get("interrupted"))

    def remote_shutdown(self, session: Any) -> dict[str, Any]:
        if not session.remote_socket or not session.remote_token_path:
            return {"confirmed": True, "alive_after": False}
        control = {
            "socket_path": session.remote_socket,
            "token_path": session.remote_token_path,
        }
        encoded = base64.b64encode(
            (
                "import json,socket,sys; "
                "p,t=sys.argv[1:]; token=open(t).read().strip(); "
                "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(p); "
                "s.sendall((json.dumps({'token':token,'op':'shutdown','protocol':'athena-ssh-supervisor','version':1})+'\\n').encode()); "
                "print(s.recv(65536).decode().strip()); s.close()"
            ).encode()
        ).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self.remote_arg(control['socket_path'])} {self.remote_arg(control['token_path'])}"
        )
        completed = self.run_remote_command(
            command,
            key_path=session.key_path,
            timeout=max(self.profile.connect_timeout, 5.0),
            check=False,
        )
        try:
            receipt = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (IndexError, TypeError, json.JSONDecodeError):
            return {
                "confirmed": False,
                "alive_after": True,
                "error": (completed.stderr or completed.stdout or "shutdown receipt missing")[
                    -1000:
                ],
            }
        controller_pid = str(
            receipt.get("controller_pid")
            or session.remote_controller_pid
            or str(session.start_identity).split(":", 1)[0]
        )
        metadata_path = session.remote_metadata_path or "/dev/null"
        probe = (
            f"for i in 1 2 3 4 5 6 7 8 9 10; do "
            f"if ! kill -0 {shlex.quote(controller_pid)} 2>/dev/null "
            f"&& test ! -e {self.remote_arg(session.remote_socket)} "
            f"&& test ! -e {self.remote_arg(session.remote_token_path)} "
            f"&& test ! -e {self.remote_arg(metadata_path)}; then exit 0; fi; "
            "sleep 0.1; done; exit 1"
        )
        verified = self.run_remote_command(
            probe,
            key_path=session.key_path,
            timeout=max(self.profile.connect_timeout, 5.0),
            check=False,
        )
        receipt["controller_alive_after"] = verified.returncode != 0
        receipt["confirmed"] = bool(receipt.get("confirmed")) and verified.returncode == 0
        receipt["alive_after"] = bool(receipt.get("alive_after")) or verified.returncode != 0
        return receipt
