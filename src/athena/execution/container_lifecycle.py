"""Container runtime-session creation and reattachment mechanics."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Mapping

from athena.execution.container_session import (
    ContainerSession,
    DockerNodeSession,
    DockerPythonSession,
)
from athena.execution.container_transport import ContainerTransport
from athena.execution.runtimes.shell import _SubprocessSession
from athena.protocol.tasks import NetworkPolicy
from athena.state.runtime_sessions import environment_fingerprint, sanitize_environment


class ContainerSessionLifecycle:
    """Create and restore sessions without owning backend session indexes."""

    def __init__(
        self,
        *,
        require_available: Callable[[], None],
        canonical_runtime: Callable[[str], str],
        workspace_root: Callable[[str | None], str],
        workspace_cwd: Callable[[str, str | None], str],
        validate_env: Callable[[Mapping[str, str] | None], dict[str, str]],
        image_for_runtime: Callable[[str], str],
        resolve_image: Callable[[str | None], tuple[str, str]],
        create_container: Callable[..., str],
        inspect_container: Callable[[str], dict[str, Any]],
        exec_command: Callable[..., list[str]],
        remove_container: Callable[[str], None],
    ) -> None:
        self._require_available = require_available
        self._canonical_runtime = canonical_runtime
        self._workspace_root = workspace_root
        self._workspace_cwd = workspace_cwd
        self._validate_env = validate_env
        self._image_for_runtime = image_for_runtime
        self._resolve_image = resolve_image
        self._create_container = create_container
        self._inspect_container = inspect_container
        self._exec_command = exec_command
        self._remove_container = remove_container

    def create(
        self,
        *,
        session_id: str,
        task_id: str,
        runtime: str,
        cwd: str | None,
        env: Mapping[str, str] | None,
        workspace_root: str | None,
        network_policy: NetworkPolicy | str | None,
    ) -> ContainerSession:
        self._require_available()
        canonical = self._canonical_runtime(runtime)
        root = self._workspace_root(workspace_root)
        container_cwd = self._workspace_cwd(root, cwd)
        values = self._validate_env(env)
        image_ref, image_digest = self._resolve_image(self._image_for_runtime(canonical))
        container_id = self._create_container(
            task_id=task_id,
            workspace_root=root,
            network_policy=network_policy,
            runtime=canonical,
            env=values,
            image_ref=image_ref,
            session_id=session_id,
            image_digest=image_digest,
        )
        inspected = self._inspect_container(container_id)
        state = inspected.get("State") or {}
        start_identity = str(state.get("StartedAt") or container_id)
        command = self._exec_command(
            container_id=container_id,
            runtime=canonical,
            cwd=container_cwd,
            env=values,
        )
        try:
            worker = self._worker(canonical, command, values)
            worker.start()
        except Exception:  # noqa: BLE001 - remove the owned container before re-raising
            self._remove_container(container_id)
            raise
        return ContainerSession(
            session_id=session_id,
            task_id=task_id,
            runtime=canonical,
            container_id=container_id,
            cwd=container_cwd,
            env=values,
            worker=worker,
            workspace_root=root,
            image_ref=image_ref,
            image_digest=image_digest,
            network_policy=str(getattr(network_policy, "value", network_policy) or "deny"),
            start_identity=start_identity,
        )

    def reattach(self, record: Mapping[str, Any]) -> ContainerSession:
        session_id = str(record.get("id") or "")
        task_id = str(record.get("task_id") or "")
        runtime = self._canonical_runtime(str(record.get("runtime") or ""))
        raw_metadata = record.get("metadata")
        metadata: Mapping[str, Any] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        container_id = str(metadata.get("container_id") or record.get("process_identity") or "")
        if not session_id or not task_id or not container_id:
            raise RuntimeError("runtime record lacks container/session ownership identity")
        inspected = self._inspect_container(container_id)
        labels = (inspected.get("Config") or {}).get("Labels") or {}
        expected_labels = {
            "athena.session_id": session_id,
            "athena.task_id": task_id,
            "athena.backend": "container",
            "athena.runtime": runtime,
        }
        for key, expected in expected_labels.items():
            if str(labels.get(key) or "") != expected:
                raise RuntimeError(f"container identity mismatch for {key}")
        if str(inspected.get("Id") or container_id) != container_id:
            raise RuntimeError("container process identity does not match durable identity")
        if not bool((inspected.get("State") or {}).get("Running")):
            raise RuntimeError("container is not running")

        workspace = str(record.get("workspace_identity") or metadata.get("workspace_root") or "")
        if not workspace or os.path.realpath(workspace) != str(
            labels.get("athena.workspace_identity") or ""
        ):
            raise RuntimeError("container workspace identity mismatch")
        network_policy = str(
            record.get("network_policy") or metadata.get("network_policy") or "deny"
        )
        if str(labels.get("athena.network_policy") or "") != network_policy:
            raise RuntimeError("container network policy identity mismatch")
        expected_start = str(record.get("start_identity") or metadata.get("start_identity") or "")
        actual_start = str((inspected.get("State") or {}).get("StartedAt") or "")
        if expected_start and actual_start and expected_start != actual_start:
            raise RuntimeError("container start identity mismatch")
        network_mode = str((inspected.get("HostConfig") or {}).get("NetworkMode") or "")
        if network_policy != NetworkPolicy.ALLOW.value and network_mode != "none":
            raise RuntimeError("container network contract no longer matches")
        if network_policy == NetworkPolicy.ALLOW.value and network_mode == "none":
            raise RuntimeError("container network contract no longer matches")
        mounts = inspected.get("Mounts") or []
        workspace_mount = next(
            (
                mount
                for mount in mounts
                if mount.get("Destination") == ContainerTransport.workspace_mount
            ),
            None,
        )
        if (
            not isinstance(workspace_mount, Mapping)
            or os.path.realpath(str(workspace_mount.get("Source") or ""))
            != os.path.realpath(workspace)
            or bool(workspace_mount.get("RW"))
        ):
            raise RuntimeError("container workspace mount contract no longer matches")

        raw_environment = metadata.get("environment")
        if not isinstance(raw_environment, Mapping):
            raw_environment = metadata.get("env")
        env, redacted_environment_keys = sanitize_environment(raw_environment)
        if redacted_environment_keys:
            raise RuntimeError(
                "container reattachment cannot prove redacted environment identity: "
                + ", ".join(redacted_environment_keys)
            )
        expected_environment = str(
            record.get("environment_fingerprint") or metadata.get("environment_fingerprint") or ""
        )
        if expected_environment and expected_environment != environment_fingerprint(env):
            raise RuntimeError("container environment identity mismatch")
        cwd = str(record.get("cwd") or workspace)
        container_cwd = (
            cwd
            if cwd == ContainerTransport.workspace_mount
            or cwd.startswith(ContainerTransport.workspace_mount + "/")
            else self._workspace_cwd(workspace, cwd)
        )
        image_digest = str(
            metadata.get("image_digest")
            or record.get("runtime_version")
            or labels.get("athena.image_digest")
            or ""
        )
        if image_digest and str(labels.get("athena.image_digest") or "") != image_digest:
            raise RuntimeError("container image identity mismatch")
        image_ref = str((inspected.get("Config") or {}).get("Image") or image_digest)
        command = self._exec_command(
            container_id=container_id,
            runtime=runtime,
            cwd=container_cwd,
            env=self._validate_env(env),
        )
        worker = self._worker(runtime, command, env)
        worker.start()
        return ContainerSession(
            session_id=session_id,
            task_id=task_id,
            runtime=runtime,
            container_id=container_id,
            cwd=container_cwd,
            env=self._validate_env(env),
            worker=worker,
            workspace_root=workspace,
            image_ref=image_ref,
            image_digest=image_digest,
            network_policy=network_policy,
            start_identity=actual_start or expected_start or container_id,
        )

    @staticmethod
    def _worker(runtime: str, command: list[str], env: Mapping[str, str]) -> Any:
        if runtime == "shell":
            return _SubprocessSession(
                env={},
                cwd=None,
                start_cmd=command,
                sandbox_root=None,
                network_policy=None,
            )
        if runtime == "node":
            return DockerNodeSession(command=command, env=env)
        return DockerPythonSession(command=command, env=env)


__all__ = ["ContainerSessionLifecycle"]
