"""Workspace checkpoints: capture a workspace tree, restore it later.

Deliberately simple: capture is a filtered ``shutil.copytree`` plus a JSON
manifest; restore copies the snapshot back over the target directory and
removes files that exist in the target but not in the snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from athena.protocol.ids import new_id

_logger = logging.getLogger(__name__)

_IGNORE_PATTERNS = shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv")


class CheckpointManager:
    def __init__(self, root: str = "/tmp/athena-checkpoints") -> None:
        self._root = Path(root)

    @property
    def root(self) -> str:
        return str(self._root)

    async def capture(self, *, task_id: str, workspace_root: str, label: str) -> dict:
        src = Path(workspace_root)
        if not src.is_dir():
            raise NotADirectoryError(f"workspace root does not exist: {workspace_root}")

        checkpoint_id = new_id("ckpt")
        dest = self._root / checkpoint_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=_IGNORE_PATTERNS, dirs_exist_ok=True)

        files = [
            str(p.relative_to(dest))
            for p in sorted(dest.rglob("*"))
            if p.is_file()
        ]
        manifest = {
            "id": checkpoint_id,
            "task_id": task_id,
            "label": label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file_count": len(files),
            "files": files,
        }
        (self._root / f"{checkpoint_id}.manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        _logger.info("captured checkpoint %s (%d files)", checkpoint_id, len(files))
        return manifest

    async def restore(self, checkpoint_id: str, workspace_root: str) -> dict:
        manifest_path = self._root / f"{checkpoint_id}.manifest.json"
        if not manifest_path.is_file():
            raise KeyError(f"Unknown checkpoint: {checkpoint_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot = self._root / checkpoint_id
        if not snapshot.is_dir():
            raise FileNotFoundError(f"checkpoint data missing: {snapshot}")

        target = Path(workspace_root)
        target.mkdir(parents=True, exist_ok=True)

        restored = 0
        for rel in manifest.get("files", []):
            src_file = snapshot / rel
            dst_file = target / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            restored += 1

        # Remove current-tree files that the snapshot did not contain, but only
        # under directories the snapshot actually covered (rsync --delete style).
        removed = 0
        # Remove current-tree files that the snapshot did not contain when
        # they live in a directory the snapshot covered (rsync --delete style).
        captured_files = set(manifest.get("files", []))
        captured_dirs = {""}
        for rel in captured_files:
            parts = rel.split("/")
            for i in range(1, len(parts)):
                captured_dirs.add("/".join(parts[:i]))
        for cur in sorted(target.rglob("*"), reverse=True):
            if not cur.is_file():
                continue
            rel = cur.relative_to(target).as_posix()
            parent_rel = os.path.dirname(rel)
            if parent_rel in captured_dirs and rel not in captured_files:
                cur.unlink()
                removed += 1

        summary = {
            "checkpoint_id": checkpoint_id,
            "workspace_root": str(target),
            "restored_files": restored,
            "removed_files": removed,
        }
        _logger.info("restored checkpoint %s: %s", checkpoint_id, summary)
        return summary


__all__ = ["CheckpointManager"]
