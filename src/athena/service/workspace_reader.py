"""Workspace instruction-chain reader for context compilation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from athena.context.instructions import hierarchical_agents_md

__all__ = ["WorkspaceInstructionReader"]


class WorkspaceInstructionReader:
    """Read hierarchical AGENTS.md instructions for one workspace root."""

    def __init__(self, root: str | None) -> None:
        self._root = root

    @classmethod
    def from_workspace(cls, workspace: Any) -> "WorkspaceInstructionReader | None":
        root = getattr(workspace, "root", None) if workspace is not None else None
        if not isinstance(root, str) or not root:
            return None
        return cls(root)

    def list_agents_md(self) -> list[tuple[str, str]]:
        if not self._root:
            return []
        try:
            return hierarchical_agents_md(self._root)
        except Exception:
            return []

    def snapshot(self) -> tuple[tuple[Any, ...], list[tuple[str, str]]]:
        files = self.list_agents_md()
        root = self._root or ""
        revision = tuple(
            (
                str(path),
                int(Path(root, str(path)).stat().st_mtime_ns)
                if Path(root, str(path)).exists()
                else 0,
                len(text),
            )
            for path, text in files
        )
        return revision, files
