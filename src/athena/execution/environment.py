"""Canonical, secret-free environment identity for executable proofs."""

from __future__ import annotations

import hashlib
import json
import os  # noqa: F401 - compatibility surface for environment record tests
import platform  # noqa: F401 - compatibility surface for environment record tests
import shutil  # noqa: F401 - compatibility surface for environment probe tests
import subprocess  # noqa: F401 - compatibility surface for environment probe tests
import sys  # noqa: F401 - compatibility surface for environment record tests
from pathlib import Path
from typing import Any, Mapping

from athena.concurrency import run_blocking
from athena.execution.environment_probe import EnvironmentProbe
from athena.execution.environment_record import EnvironmentRecordBuilder
from athena.execution.verification_environment import ToolchainBinding, VerificationEnvironment
from athena.protocol.tasks import WorkspaceSpec


class ProjectEnvironmentFingerprint:
    """Compute one stable identity for the environment a proof ran in."""

    _version_cache = EnvironmentProbe._version_cache

    def describe(
        self,
        workspace: WorkspaceSpec,
        *,
        extras: Mapping[str, Any] | None = None,
        project_profile: Any = None,
        toolchain_names: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        root = Path(workspace.root)
        relevant_files = self._relevant_file_hashes(root)
        return EnvironmentRecordBuilder.build(
            workspace,
            relevant_files=relevant_files,
            toolchain=self._toolchain_identity(project_profile, names=toolchain_names),
            extras=extras,
        )

    def fingerprint(
        self,
        workspace: WorkspaceSpec,
        *,
        extras: Mapping[str, Any] | None = None,
        project_profile: Any = None,
        toolchain_names: tuple[str, ...] | None = None,
    ) -> str:
        payload = json.dumps(
            self.describe(
                workspace,
                extras=extras,
                project_profile=project_profile,
                toolchain_names=toolchain_names,
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def digest(description: Mapping[str, Any]) -> str:
        """Hash a protocol-stable description into one environment identity."""
        payload = json.dumps(description, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def describe_async(
        self,
        workspace: WorkspaceSpec,
        *,
        extras: Mapping[str, Any] | None = None,
        project_profile: Any = None,
        toolchain_names: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Compute one environment description with off-loop host probes.

        The bounded workspace walk and potentially slow executable probes run
        off the event loop, while producing exactly the same record shape as
        :meth:`describe`.
        """
        relevant_files, toolchain = await run_blocking(
            self._describe_inputs,
            Path(workspace.root),
            project_profile,
            toolchain_names,
        )
        return self._environment_record(
            workspace,
            relevant_files=relevant_files,
            toolchain=toolchain,
            extras=extras,
        )

    async def fingerprint_async(
        self,
        workspace: WorkspaceSpec,
        *,
        extras: Mapping[str, Any] | None = None,
        project_profile: Any = None,
        toolchain_names: tuple[str, ...] | None = None,
    ) -> str:
        """Compute the same identity with async tool-version probes."""
        description = await self.describe_async(
            workspace,
            extras=extras,
            project_profile=project_profile,
            toolchain_names=toolchain_names,
        )
        payload = json.dumps(description, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _describe_inputs(
        self,
        root: Path,
        project_profile: Any,
        toolchain_names: tuple[str, ...] | None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Collect all potentially blocking identity inputs at one seam."""
        return (
            self._relevant_file_hashes(root),
            self._toolchain_identity(project_profile, names=toolchain_names),
        )

    def _environment_record(
        self,
        workspace: WorkspaceSpec,
        *,
        relevant_files: dict[str, str],
        toolchain: dict[str, str],
        extras: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the protocol-stable environment description record."""
        return EnvironmentRecordBuilder.build(
            workspace,
            relevant_files=relevant_files,
            toolchain=toolchain,
            extras=extras,
        )

    @staticmethod
    def _relevant_file_hashes(root: Path) -> dict[str, str]:
        return EnvironmentProbe.relevant_file_hashes(root)

    @classmethod
    def _toolchain_identity(
        cls,
        project_profile: Any,
        *,
        names: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        return EnvironmentProbe.toolchain_identity(project_profile, names=names)

    @classmethod
    def _executable_identity(cls, name: str, path: str) -> str:
        return EnvironmentProbe.executable_identity(name, path)

    @staticmethod
    def _probe_version(name: str, path: str) -> str:
        return EnvironmentProbe.probe_version(name, path)

    @classmethod
    async def _toolchain_identity_async(
        cls,
        project_profile: Any,
        *,
        names: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        return await EnvironmentProbe.toolchain_identity_async(project_profile, names=names)

    @classmethod
    async def _executable_identity_async(cls, name: str, path: str) -> tuple[str, str]:
        return await EnvironmentProbe.executable_identity_async(name, path)

    @staticmethod
    async def _probe_version_async(name: str, path: str) -> str:
        return await EnvironmentProbe.probe_version_async(name, path)


__all__ = [
    "ProjectEnvironmentFingerprint",
    "ToolchainBinding",
    "VerificationEnvironment",
]
