"""Docker command and container-identity mechanics for the container backend."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Mapping

from athena.execution.runtimes.node import _NODE_WORKER
from athena.execution.runtimes.python import _WORKER_SOURCE
from athena.protocol.tasks import NetworkPolicy

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORKSPACE_MOUNT = "/workspace"
_CONTAINER_CWD = "/workspace"


class ContainerTransport:
    """Own Docker CLI mechanics without owning runtime-session state."""

    workspace_mount = _WORKSPACE_MOUNT

    def __init__(
        self,
        *,
        image: str,
        runtime_images: Mapping[str, str],
        docker_command: str,
        runner: Callable[..., subprocess.CompletedProcess[str]],
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.image = image
        self.runtime_images = dict(runtime_images)
        self.docker_command = docker_command
        self._runner = runner
        self._which = which

    def available(self) -> bool:
        """Return whether Docker can actually service a request."""
        if self._which(self.docker_command) is None:
            return False
        try:
            result = self._runner(
                [self.docker_command, "info", "--format", "{{.ServerVersion}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
                env=self._docker_env(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and bool((result.stdout or "").strip())

    def environment_identity(self) -> dict[str, str]:
        """Return the image identity used by new container sessions."""
        image_ref, image_digest = self.resolve_image()
        return {"image": image_ref, "image_digest": image_digest}

    def image_for_runtime(self, runtime: str) -> str:
        return self.runtime_images.get(runtime, self.image)

    @staticmethod
    def validate_env(env: Mapping[str, str] | None) -> dict[str, str]:
        values = {str(key): str(value) for key, value in (env or {}).items()}
        for key, value in values.items():
            if not _ENV_NAME.fullmatch(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
            if "\x00" in value:
                raise ValueError(f"environment variable {key!r} contains NUL")
        return values

    @staticmethod
    def workspace_root(workspace_root: str | None) -> str:
        if not workspace_root:
            raise ValueError("container execution requires a workspace root")
        root = os.path.realpath(os.path.abspath(workspace_root))
        if not os.path.isdir(root):
            raise ValueError(f"container workspace root is not a directory: {root}")
        if "," in root:
            raise ValueError("container workspace paths may not contain commas")
        return root

    @staticmethod
    def workspace_cwd(root: str, cwd: str | None) -> str:
        target = os.path.realpath(os.path.abspath(cwd or root))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError(f"container cwd is outside workspace: {cwd}")
        if not os.path.isdir(target):
            raise ValueError(f"container cwd is not a directory: {cwd}")
        suffix = target[len(root) :]
        return _WORKSPACE_MOUNT + suffix

    @staticmethod
    def _docker_env() -> dict[str, str]:
        # These variables select the Docker endpoint/context; they are not
        # copied wholesale because the host environment may contain secrets.
        keys = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
        return {key: os.environ[key] for key in keys if key in os.environ}

    def run_docker(self, args: list[str], *, timeout: float = 30.0) -> str:
        result = self._runner(
            [self.docker_command, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=self._docker_env(),
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "docker command failed").strip()
            raise RuntimeError(error)
        return (result.stdout or "").strip()

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        raw = self.run_docker(["inspect", "--format", "{{json .}}", container_id])
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Docker returned invalid container metadata for {container_id}"
            ) from exc
        if isinstance(value, list):
            value = value[0] if value else None
        if not isinstance(value, dict):
            raise RuntimeError(f"Docker returned no metadata for container {container_id}")
        return value

    def resolve_image(self, image: str | None = None) -> tuple[str, str]:
        """Resolve a configured image to an immutable local identity."""
        selected_image = str(image or self.image)
        raw = self.run_docker(["image", "inspect", "--format", "{{json .}}", selected_image])
        try:
            record = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Docker returned invalid image metadata for {selected_image!r}"
            ) from exc
        if not isinstance(record, dict):
            raise RuntimeError(f"Docker image metadata is not an object for {selected_image!r}")

        repo_digests = record.get("RepoDigests")
        if isinstance(repo_digests, list):
            for value in repo_digests:
                candidate = str(value or "")
                match = re.search(r"@(?P<digest>sha256:[0-9a-f]{64})$", candidate)
                if match:
                    return candidate, match.group("digest")

        image_id = str(record.get("Id") or "")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            return image_id, image_id
        raise RuntimeError(
            f"container image {selected_image!r} has no immutable digest; "
            "use a digest-pinned image or build a local image"
        )

    def create_container(
        self,
        *,
        task_id: str,
        workspace_root: str,
        network_policy: NetworkPolicy | str | None,
        runtime: str = "python",
        env: Mapping[str, str] | None = None,
        image_ref: str | None = None,
        session_id: str | None = None,
        image_digest: str | None = None,
    ) -> str:
        root = self.workspace_root(workspace_root)
        policy = getattr(network_policy, "value", network_policy) or NetworkPolicy.DENY.value
        command = [
            "run",
            "--detach",
            "--interactive",
            "--rm",
            "--name",
            f"athena-{uuid.uuid4().hex[:16]}",
            "--label",
            "athena.task_id=" + task_id,
            "--label",
            "athena.backend=container",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev",
            "--mount",
            f"type=bind,source={root},target={_WORKSPACE_MOUNT},readonly",
            "--workdir",
            _CONTAINER_CWD,
        ]
        # Dependency acquisition has a narrow, operator-defined writable
        # enclave. The source workspace remains read-only, while Python/npm
        # installs can persist their reproducibility records across sessions.
        for relative in (".athena/dependencies", ".athena/node", ".athena/environments"):
            writable = Path(root, relative)
            writable.mkdir(parents=True, exist_ok=True)
            command.extend(
                (
                    "--mount",
                    f"type=bind,source={writable},target={_WORKSPACE_MOUNT}/{relative},rw",
                )
            )
        for key, value in self.validate_env(env).items():
            command.extend(("--env", f"{key}={value}"))
        labels: dict[str, str | None] = {
            "athena.session_id": session_id,
            "athena.runtime": runtime,
            "athena.workspace_identity": root,
            "athena.network_policy": policy,
            "athena.image_digest": image_digest,
        }
        for label_key, label_value in labels.items():
            if label_value:
                command.extend(("--label", f"{label_key}={label_value}"))
        if policy != NetworkPolicy.ALLOW.value:
            # Restricted currently has no allowlist representation at the
            # execution boundary, so it is fail-closed like denied network.
            command.extend(("--network", "none"))
        if runtime == "shell":
            container_program: tuple[str, ...] = ("bash", "--norc", "--noprofile")
        elif runtime == "node":
            container_program = ("node", "-e", _NODE_WORKER)
        else:
            container_program = ("python", "-u", "-c", _WORKER_SOURCE)
        selected_ref = image_ref or self.resolve_image(self.image_for_runtime(runtime))[0]
        command.extend((selected_ref, *container_program))
        container_id = self.run_docker(command)
        if not container_id:
            raise RuntimeError("Docker returned an empty container id")
        return container_id.splitlines()[-1].strip()

    def exec_command(
        self,
        *,
        container_id: str,
        runtime: str,
        cwd: str,
        env: Mapping[str, str],
    ) -> list[str]:
        del runtime, cwd, env
        # Attach to the long-lived init process so runtime state survives an
        # Athena restart; a fresh docker exec worker would lose that state.
        return [self.docker_command, "attach", container_id]

    def remove_container(self, container_id: str) -> None:
        try:
            self.run_docker(["rm", "-f", container_id], timeout=15)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # Teardown is best effort. The container was created with --rm;
            # the daemon will remove it when its init process exits.
            pass


__all__ = ["ContainerTransport"]
