"""Authenticated SSH session creation and reattachment mechanics."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from typing import Any

from athena.concurrency import run_blocking
from athena.execution.ssh_profile import SSHProfile, _SAFE_REMOTE_PATH
from athena.execution.ssh_session import _SSHPythonSession, _SSHSession
from athena.execution.ssh_transport import SSHTransport
from athena.protocol.tasks import NetworkPolicy


class SSHSessionLifecycle:
    """Own remote worker creation and restart-time identity verification."""

    def __init__(self, profile: SSHProfile, transport: SSHTransport) -> None:
        self._profile = profile
        self._transport = transport

    def create(
        self,
        *,
        task_id: str,
        runtime: str,
        cwd: str | None,
        env: Mapping[str, str] | None,
        workspace_root: str | None,
        network_policy: NetworkPolicy | str | None,
    ) -> _SSHSession:
        del workspace_root
        policy = getattr(network_policy, "value", network_policy)
        if policy not in {None, NetworkPolicy.ALLOW.value}:
            raise ValueError("SSH backend cannot prove denied/restricted remote networking")
        canonical = _runtime_name(runtime)
        if canonical not in {"python", "shell", "node"}:
            raise ValueError("SSH backend supports python, shell, and node")
        key_path = self._transport.credential_path(task_id)
        remote_cwd = cwd or self._transport.target_root(task_id)
        if (
            not remote_cwd.startswith(self._profile.remote_root.rstrip("/") + "/")
            or _SAFE_REMOTE_PATH.fullmatch(remote_cwd) is None
        ):
            remote_cwd = self._transport.target_root(task_id)
        session_id = f"ssh_{task_id}_{secrets.token_hex(8)}"
        authority_digest = hashlib.sha256(
            json.dumps(
                {
                    "profile": self._profile.name,
                    "task_id": task_id,
                    "session_id": session_id,
                    "runtime": canonical,
                    "remote_cwd": remote_cwd,
                    "network_policy": str(policy or ""),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        remote_metadata: dict[str, str] | None = None
        try:
            remote_metadata = self._transport.start_remote_python_supervisor(
                task_id=task_id,
                session_id=session_id,
                remote_cwd=remote_cwd,
                key_path=key_path,
                runtime=canonical,
                authority_digest=authority_digest,
            )
            relay_paths = {
                "socket_path": remote_metadata["socket_path"],
                "token_path": remote_metadata["token_path"],
            }
            command = [
                *self._transport.ssh_base(key_path),
                self._transport.remote_relay_command(relay_paths),
            ]
            worker = _SSHPythonSession(command=command, env=env)
            worker.start()
        except Exception:  # noqa: BLE001 - close owned remote session before re-raising
            if remote_metadata is not None:
                self._transport.remote_shutdown(
                    _SSHSession(
                        id=session_id,
                        task_id=task_id,
                        runtime=canonical,
                        remote_cwd=remote_cwd,
                        worker=None,
                        host=self._profile.host,
                        start_identity=remote_metadata.get("start_identity", session_id),
                        runtime_identity=remote_metadata.get("runtime_identity"),
                        worker_source_sha256=remote_metadata.get("worker_source_sha256"),
                        session_nonce=remote_metadata.get("session_nonce"),
                        authority_digest=remote_metadata.get("authority_digest"),
                        key_path=key_path,
                        remote_socket=remote_metadata.get("socket_path"),
                        remote_token_path=remote_metadata.get("token_path"),
                        remote_controller_pid=remote_metadata.get("pid"),
                    )
                )
            _remove_temporary_key(self._profile, key_path)
            raise
        return _SSHSession(
            id=session_id,
            task_id=task_id,
            runtime=canonical,
            remote_cwd=remote_cwd,
            worker=worker,
            host=self._profile.host,
            start_identity=(remote_metadata or {}).get("start_identity", session_id),
            runtime_identity=(remote_metadata or {}).get("runtime_identity"),
            worker_source_sha256=(remote_metadata or {}).get("worker_source_sha256"),
            session_nonce=(remote_metadata or {}).get("session_nonce"),
            authority_digest=(remote_metadata or {}).get("authority_digest"),
            key_path=key_path if self._profile.identity_file is None else None,
            remote_socket=(remote_metadata or {}).get("socket_path"),
            remote_token_path=(remote_metadata or {}).get("token_path"),
            remote_metadata_path=(remote_metadata or {}).get("metadata_path"),
            remote_controller_pid=(remote_metadata or {}).get("pid"),
        )

    async def reattach(self, record: Mapping[str, Any]) -> _SSHSession:
        runtime = _runtime_name(str(record.get("runtime") or ""))
        if runtime not in {"python", "node", "shell"}:
            raise RuntimeError("SSH reattachment requires python, node, or shell runtime")
        raw_metadata = record.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        session_id = str(record.get("id") or "")
        task_id = str(record.get("task_id") or "")
        remote_socket = str(metadata.get("remote_socket") or record.get("remote_socket") or "")
        remote_token_path = str(
            metadata.get("remote_token_path") or record.get("remote_token_path") or ""
        )
        remote_metadata_path = str(
            metadata.get("remote_metadata_path") or record.get("remote_metadata_path") or ""
        )
        if (
            not session_id
            or not task_id
            or not remote_socket
            or not remote_token_path
            or not remote_metadata_path
        ):
            raise RuntimeError("SSH runtime record lacks remote supervisor identity")
        expected_suffixes = (
            f"/.athena-supervisor/{session_id}.sock",
            f"/.athena-supervisor/{session_id}.token",
            f"/.athena-supervisor/{session_id}.json",
        )
        for path, suffix in zip(
            (remote_socket, remote_token_path, remote_metadata_path),
            expected_suffixes,
            strict=True,
        ):
            if (
                _SAFE_REMOTE_PATH.fullmatch(path) is None
                or ".." in path.split("/")
                or not path.endswith(suffix)
            ):
                raise RuntimeError("SSH runtime record contains an invalid supervisor path")
        key_path = await run_blocking(self._transport.credential_path, task_id, _pool="long")
        try:
            probe = await run_blocking(
                self._transport.run_remote_command,
                f"test -s {self._transport.remote_arg(remote_metadata_path)} && cat {self._transport.remote_arg(remote_metadata_path)}",
                _pool="long",
                key_path=key_path,
                check=False,
            )
            if probe.returncode != 0:
                raise RuntimeError("remote supervisor metadata is unavailable")
            try:
                remote = json.loads(probe.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("remote supervisor metadata is invalid") from exc
            if not isinstance(remote, Mapping):
                raise RuntimeError("remote supervisor metadata is not an object")
            if str(remote.get("session_id")) != session_id:
                raise RuntimeError("remote supervisor session identity mismatch")
            if str(remote.get("task_id")) != task_id:
                raise RuntimeError("remote supervisor task ownership mismatch")
            if str(remote.get("runtime")) != runtime:
                raise RuntimeError("remote supervisor runtime identity mismatch")
            expected_start = str(
                record.get("start_identity") or metadata.get("start_identity") or ""
            )
            if expected_start and str(remote.get("start_identity")) != expected_start:
                raise RuntimeError("remote supervisor process identity mismatch")
            for identity_key in (
                "runtime_identity",
                "worker_source_sha256",
                "session_nonce",
                "authority_digest",
            ):
                expected_identity = str(
                    record.get(identity_key) or metadata.get(identity_key) or ""
                )
                if expected_identity and str(remote.get(identity_key) or "") != expected_identity:
                    raise RuntimeError(f"remote supervisor {identity_key} mismatch")
            pid = str(remote.get("pid") or "")
            if not pid.isdigit():
                raise RuntimeError("remote supervisor pid identity is invalid")
            described = await run_blocking(
                self._transport.remote_describe,
                _pool="long",
                socket_path=remote_socket,
                token_path=remote_token_path,
                key_path=key_path,
            )
            if str(described.get("session_id")) != session_id:
                raise RuntimeError("remote supervisor live session identity mismatch")
            if str(described.get("task_id")) != task_id:
                raise RuntimeError("remote supervisor live task ownership mismatch")
            live_start = str(described.get("start_identity") or "")
            if not live_start or live_start != expected_start:
                raise RuntimeError("remote supervisor live process identity mismatch")
            relay_paths = {"socket_path": remote_socket, "token_path": remote_token_path}
            command = [
                *self._transport.ssh_base(key_path),
                self._transport.remote_relay_command(relay_paths),
            ]
            worker = _SSHPythonSession(command=command)
            worker.start()
            return _SSHSession(
                id=session_id,
                task_id=task_id,
                runtime=runtime,
                remote_cwd=str(
                    record.get("workspace_identity") or metadata.get("workspace_identity") or ""
                ),
                worker=worker,
                host=self._profile.host,
                start_identity=str(remote.get("start_identity") or expected_start),
                runtime_identity=str(remote.get("runtime_identity") or "") or None,
                worker_source_sha256=str(remote.get("worker_source_sha256") or "") or None,
                session_nonce=str(remote.get("session_nonce") or "") or None,
                authority_digest=str(remote.get("authority_digest") or "") or None,
                key_path=key_path if self._profile.identity_file is None else None,
                remote_socket=remote_socket,
                remote_token_path=remote_token_path,
                remote_metadata_path=remote_metadata_path,
                remote_controller_pid=str(remote.get("pid") or ""),
            )
        except Exception:  # noqa: BLE001 - clean temporary SSH key after failed reattach
            _remove_temporary_key(self._profile, key_path)
            raise


def _remove_temporary_key(profile: SSHProfile, key_path: str | None) -> None:
    if profile.identity_file is None and key_path:
        try:
            os.unlink(key_path)
        except FileNotFoundError:
            pass


def _runtime_name(value: str) -> str:
    value = str(value).casefold()
    return {
        "python3": "python",
        "py": "python",
        "bash": "shell",
        "sh": "shell",
        "zsh": "shell",
        "nodejs": "node",
        "js": "node",
        "javascript": "node",
    }.get(value, value)


__all__ = ["SSHSessionLifecycle"]
