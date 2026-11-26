"""Protocol-stable environment description composition."""

from __future__ import annotations

import os
import platform
import sys
from typing import Any, Mapping

from athena.protocol.tasks import WorkspaceSpec


class EnvironmentRecordBuilder:
    """Build secret-free environment records from already-probed inputs."""

    @staticmethod
    def build(
        workspace: WorkspaceSpec,
        *,
        relevant_files: Mapping[str, str],
        toolchain: Mapping[str, str],
        extras: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "python_version": platform.python_version(),
            "python_executable": os.path.realpath(sys.executable),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "execution_backend": workspace.execution_backend or "local",
            "sandbox_backend": workspace.execution_backend or "local",
            "workspace_policy": {
                "readable": [
                    {"path": rule.path, "allow": rule.allow} for rule in workspace.readable
                ],
                "writable": [
                    {"path": rule.path, "allow": rule.allow} for rule in workspace.writable
                ],
                "mutation_mode": getattr(
                    workspace.mutation_mode,
                    "value",
                    workspace.mutation_mode,
                ),
            },
            "network_policy": getattr(
                workspace.network_policy,
                "value",
                workspace.network_policy,
            ),
            "workspace_revision": getattr(workspace, "revision", None),
            "dependency_lock_hash": relevant_files.get(".athena/dependencies.lock.json"),
            "relevant_file_hashes": dict(relevant_files),
            "toolchain": dict(toolchain),
        }
        if extras:
            record["extras"] = dict(extras)
        return record


__all__ = ["EnvironmentRecordBuilder"]
