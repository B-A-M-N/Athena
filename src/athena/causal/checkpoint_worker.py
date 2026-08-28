"""Child-process entry point for blocking checkpoint operations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from athena.causal.checkpoint import CheckpointManager


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=(
            "capture",
            "fingerprint",
            "restore",
            "delete",
            "clone",
            "manifest",
            "inspect",
            "read",
            "mode",
            "preimage",
        ),
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--label")
    parser.add_argument("--checkpoint-id")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--relative")
    parser.add_argument("--metadata-base64")
    args = parser.parse_args()
    manager = CheckpointManager(root=args.root)
    try:
        if args.operation == "capture":
            metadata = None
            if args.metadata_base64:
                metadata = json.loads(base64.b64decode(args.metadata_base64).decode("utf-8"))
                if not isinstance(metadata, dict):
                    raise ValueError("checkpoint metadata must be an object")
            result = manager._capture_sync(
                task_id=args.task_id or "unknown",
                workspace_root=args.workspace_root,
                label=args.label or "workspace-snapshot",
                metadata=metadata,
            )
        elif args.operation == "fingerprint":
            result = {"fingerprint": manager._fingerprint_sync(args.workspace_root)}
        elif args.operation == "delete":
            _remove_child(Path(args.root), args.checkpoint_id or "")
            result = {"deleted": True}
        elif args.operation == "clone":
            result = _clone_shadow(
                Path(args.workspace_root), Path(args.root), args.checkpoint_id or ""
            )
        elif args.operation == "manifest":
            from athena.shadow.engine import ShadowEngine

            result = {"manifest": ShadowEngine._manifest(args.workspace_root)}
        elif args.operation == "inspect":
            result = manager._inspect_sync(args.checkpoint_id or "")
        elif args.operation == "read":
            path = _safe_relative(Path(args.workspace_root), args.relative or "")
            if path.is_symlink() or not path.is_file():
                raise ValueError("worker read supports regular files only")
            result = {"content_base64": base64.b64encode(path.read_bytes()).decode("ascii")}
        elif args.operation == "mode":
            path = _safe_relative(Path(args.workspace_root), args.relative or "")
            if path.is_symlink() or not path.is_file():
                raise ValueError("worker mode supports regular files only")
            result = {"mode": stat.S_IMODE(path.stat().st_mode)}
        elif args.operation == "preimage":
            path = _safe_relative(Path(args.workspace_root), args.relative or "")
            if not path.exists():
                result = {"hash": "<missing>"}
            elif path.is_symlink() or not path.is_file():
                raise ValueError("worker preimage supports regular files only")
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result = {"hash": digest}
        else:
            result = manager._restore_sync(
                checkpoint_id=args.checkpoint_id or "",
                workspace_root=args.workspace_root,
                expected_fingerprint=args.expected_fingerprint,
            )
    except Exception as exc:  # noqa: BLE001 - serialize worker failures at the process boundary
        print(json.dumps({"error_type": type(exc).__name__, "error": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def _safe_relative(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"unsafe worker path: {relative}")
    root_resolved = root.resolve()
    candidate = (root / value).resolve(strict=False)
    if os.path.commonpath((str(root_resolved), str(candidate))) != str(root_resolved):
        raise ValueError(f"worker path escapes root: {relative}")
    return candidate


def _remove_child(root: Path, child_id: str) -> None:
    if not child_id or "/" in child_id or "\\" in child_id:
        raise ValueError(f"unsafe checkpoint id: {child_id}")
    target = _safe_relative(root, child_id)
    if target == root.resolve():
        raise ValueError("worker cannot remove its root")
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    manifest = root / f"{child_id}.manifest.json"
    if manifest.is_symlink() or manifest.is_file():
        manifest.unlink()


def _clone_shadow(source: Path, root: Path, branch_id: str) -> dict:
    if not source.is_dir():
        raise NotADirectoryError(f"workspace root does not exist: {source}")
    target = _safe_relative(root, branch_id)
    source_resolved = source.resolve()
    if os.path.commonpath((str(source_resolved), str(target))) == str(source_resolved):
        raise ValueError("shadow destination cannot be inside its source workspace")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"shadow destination already exists: {target}")
    from athena.shadow.engine import ShadowEngine

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv", "*.pyc"),
    )
    # The clone is the immutable branch base. Capturing its manifest after the
    # copy means an edit racing with copytree is treated as base drift at
    # commit rather than silently folded into the proof.
    base_manifest = ShadowEngine._manifest(str(target))
    return {
        "base_manifest": base_manifest,
        "base_preimages": _preimage_manifest(target),
    }


def _preimage_manifest(root: Path) -> dict[str, str]:
    """Return full content hashes for regular files in a clone base."""
    result: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in {".git", "__pycache__", "node_modules", ".venv"}
        )
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[path.relative_to(root).as_posix()] = digest.hexdigest()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
