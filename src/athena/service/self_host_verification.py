"""Self-host target and trusted-toolchain validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from athena.execution.environment import VerificationEnvironment
from athena.protocol.tasks import WorkspaceSpec


def verification_environment(
    workspace: Any,
    *,
    include_project_root: bool = False,
    include_rust: bool = False,
    task_id: str | None = None,
) -> VerificationEnvironment:
    """Validate the only supported self-host target and its proof tools."""
    if workspace is None:
        raise ValueError("athena self requires an Athena source checkout")
    root = Path(workspace.root).resolve()
    if not (root / "src" / "athena" / "__init__.py").is_file():
        raise ValueError("athena self must target an Athena source checkout")
    try:
        import tomllib

        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError("Athena pyproject.toml could not be validated") from exc
    if project.get("project", {}).get("name") != "athena-agent":
        raise ValueError("athena self must target the athena-agent checkout")
    try:
        return VerificationEnvironment.from_project(
            str(root),
            include_project_root=include_project_root,
            include_rust=include_rust,
            task_id=task_id,
        )
    except ValueError as exc:
        raise ValueError(f"athena self: {exc}") from exc


def preflight_record(workspace_root: str | None = None) -> dict[str, Any]:
    """Return the service-owned self-host proof environment record."""
    root = str(Path(workspace_root or Path.cwd()).resolve())
    workspace = WorkspaceSpec(id="athena-self-preflight", root=root)
    return verification_environment(
        workspace,
        include_project_root=True,
        include_rust=True,
    ).to_record()


__all__ = ["preflight_record", "verification_environment"]
