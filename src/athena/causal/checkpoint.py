"""Integrity-checked workspace checkpoints.

Checkpoints are workspace-file snapshots, not process or VM checkpoints. The
snapshot is immutable after capture and restore is conflict-aware: callers may
provide the workspace fingerprint they observed when deciding to restore, and
Athena will refuse to overwrite a concurrent change.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from athena.protocol.ids import new_id
from athena.workspace_manifest import copy_ignore, tree_paths

_logger = logging.getLogger(__name__)
_IGNORE_PATTERNS = copy_ignore


class CheckpointConflict(RuntimeError):
    """The target workspace changed since the caller's expected revision."""


class CheckpointManager:
    def __init__(self, root: str = "/tmp/athena-checkpoints") -> None:
        self._root = Path(root)
        self._refs_path = self._root / "refs.json"

    @property
    def root(self) -> str:
        return str(self._root)

    async def capture(
        self,
        *,
        task_id: str,
        workspace_root: str,
        label: str,
        metadata: dict | None = None,
    ) -> dict:
        """Capture a content-addressed file manifest off the event loop."""
        metadata_payload = None
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise TypeError("checkpoint metadata must be an object")
            metadata_payload = base64.b64encode(
                json.dumps(metadata, sort_keys=True).encode("utf-8")
            ).decode("ascii")
        manifest = await _run_worker(
            "capture",
            root=str(self._root),
            task_id=task_id,
            workspace_root=workspace_root,
            label=label,
            metadata_base64=metadata_payload,
        )
        self._register(manifest, owner=task_id)
        return manifest

    async def inspect(self, checkpoint_id: str) -> dict:
        """Read an immutable checkpoint manifest, including semantic state."""
        return await _run_worker(
            "inspect",
            root=str(self._root),
            checkpoint_id=checkpoint_id,
            workspace_root=str(self._root),
        )

    def retain(self, checkpoint_id: str, *, owner: str) -> None:
        """Add a durable owner reference to an existing checkpoint."""
        refs = self._load_refs()
        record = refs.setdefault(checkpoint_id, {"refcount": 0, "owners": []})
        owners = set(str(item) for item in record.get("owners") or ())
        if owner not in owners:
            owners.add(owner)
            record["refcount"] = int(record.get("refcount") or 0) + 1
        record["owners"] = sorted(owners)
        record["last_used_at"] = datetime.now(timezone.utc).isoformat()
        self._persist_refs(refs)

    def mark_terminal(self, checkpoint_id: str, *, state: str) -> None:
        """Record the terminal owner state before a checkpoint is released."""
        refs = self._load_refs()
        record = refs.get(checkpoint_id)
        if record is None:
            return
        record["terminal_state"] = str(state)
        record["last_used_at"] = datetime.now(timezone.utc).isoformat()
        self._persist_refs(refs)

    async def gc_terminal(self) -> int:
        """Remove orphaned terminal snapshots with no durable owners."""
        refs = self._load_refs()
        removed = 0
        for checkpoint_id, record in list(refs.items()):
            if int(record.get("refcount") or 0) != 0:
                continue
            if not record.get("terminal_state"):
                continue
            await _run_worker(
                "delete",
                root=str(self._root),
                checkpoint_id=checkpoint_id,
                workspace_root=str(self._root),
            )
            refs.pop(checkpoint_id, None)
            removed += 1
        if removed:
            self._persist_refs(refs)
        return removed

    async def release(self, checkpoint_id: str, *, owner: str) -> bool:
        """Release one owner and garbage-collect an unreferenced snapshot."""
        refs = self._load_refs()
        record = refs.get(checkpoint_id)
        if record is None:
            return False
        owners = set(str(item) for item in record.get("owners") or ())
        if owner not in owners:
            return False
        owners.remove(owner)
        record["owners"] = sorted(owners)
        record["refcount"] = len(owners)
        removed = False
        if not owners:
            # Publish the zero-owner terminal state before deleting data.  If
            # the process stops during deletion, startup GC can safely retry
            # the exact snapshot instead of retaining an impossible owner
            # record or losing the cleanup intent.
            record["terminal_state"] = record.get("terminal_state") or "RELEASED"
            record["last_used_at"] = datetime.now(timezone.utc).isoformat()
            self._persist_refs(refs)
            # Keep deletion on the same short-lived worker boundary as
            # capture/fingerprint/restore. A blocked filesystem thread must
            # never pin Athena's completion or recovery event loop.
            await _run_worker(
                "delete",
                root=str(self._root),
                checkpoint_id=checkpoint_id,
                workspace_root=str(self._root),
            )
            refs.pop(checkpoint_id, None)
            removed = True
        self._persist_refs(refs)
        return removed

    def _register(self, manifest: dict, *, owner: str) -> None:
        checkpoint_id = str(manifest["id"])
        refs = self._load_refs()
        refs[checkpoint_id] = {
            "refcount": 1,
            "owners": [owner],
            "created_at": manifest.get("timestamp"),
            "last_used_at": datetime.now(timezone.utc).isoformat(),
            "terminal_state": None,
        }
        self._persist_refs(refs)

    def _load_refs(self) -> dict[str, dict]:
        try:
            value = json.loads(self._refs_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {str(key): dict(record) for key, record in value.items() if isinstance(record, dict)}

    def _persist_refs(self, refs: dict[str, dict]) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._refs_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(refs, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.chmod(0o600)
        except OSError:
            _logger.debug("could not restrict checkpoint ref permissions")
        os.replace(tmp, self._refs_path)
        try:
            directory_fd = os.open(self._root, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _remove_checkpoint(self, checkpoint_id: str) -> None:
        if not checkpoint_id or "/" in checkpoint_id or "\\" in checkpoint_id:
            raise ValueError(f"unsafe checkpoint id: {checkpoint_id}")
        try:
            shutil.rmtree(self._root / checkpoint_id, ignore_errors=False)
        except FileNotFoundError:
            # Cleanup is idempotent across a crash between deleting the
            # snapshot and persisting refs.json.
            pass
        self._remove_file(self._root / f"{checkpoint_id}.manifest.json")

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _capture_sync(
        self,
        *,
        task_id: str,
        workspace_root: str,
        label: str,
        metadata: dict | None = None,
    ) -> dict:
        src = Path(workspace_root)
        if not src.is_dir():
            raise NotADirectoryError(f"workspace root does not exist: {workspace_root}")

        checkpoint_id = new_id("ckpt")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root.chmod(0o700)
        dest = self._root / checkpoint_id
        shutil.copytree(
            src,
            dest,
            ignore=_IGNORE_PATTERNS,
            dirs_exist_ok=False,
            symlinks=True,
        )
        dest.chmod(0o700)
        file_manifest = _tree_manifest(dest)
        manifest = {
            "manifest_version": 2,
            "id": checkpoint_id,
            "task_id": task_id,
            "label": label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file_count": len(file_manifest),
            # Keep the old list for callers that only need display output.
            "files": sorted(file_manifest),
            "file_manifest": file_manifest,
            "workspace_fingerprint": _fingerprint(file_manifest),
        }
        if metadata is not None:
            manifest["metadata"] = dict(metadata)
        manifest_path = self._root / f"{checkpoint_id}.manifest.json"
        _write_private_json(manifest_path, manifest)
        _logger.info("captured checkpoint %s (%d files)", checkpoint_id, len(file_manifest))
        return manifest

    def _inspect_sync(self, checkpoint_id: str) -> dict:
        if not checkpoint_id or "/" in checkpoint_id or "\\" in checkpoint_id:
            raise ValueError(f"unsafe checkpoint id: {checkpoint_id}")
        manifest_path = self._root / f"{checkpoint_id}.manifest.json"
        if not manifest_path.is_file():
            raise KeyError(f"Unknown checkpoint: {checkpoint_id}")
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("checkpoint manifest must be an object")
        return value

    async def fingerprint(self, workspace_root: str) -> str:
        """Return the current revision fingerprint used for conflict checks."""
        result = await _run_worker(
            "fingerprint",
            root=str(self._root),
            workspace_root=workspace_root,
        )
        return str(result["fingerprint"])

    def _fingerprint_sync(self, workspace_root: str) -> str:
        root = Path(workspace_root)
        if not root.is_dir():
            raise NotADirectoryError(f"workspace root does not exist: {workspace_root}")
        return _fingerprint(_tree_manifest(root))

    async def restore(
        self,
        checkpoint_id: str,
        workspace_root: str,
        *,
        expected_fingerprint: str | None = None,
    ) -> dict:
        """Restore a snapshot after integrity and concurrent-change checks."""
        return await _run_worker(
            "restore",
            root=str(self._root),
            checkpoint_id=checkpoint_id,
            workspace_root=workspace_root,
            expected_fingerprint=expected_fingerprint,
        )

    async def materialize(self, checkpoint_id: str, workspace_root: str) -> dict:
        """Restore a checkpoint into a fresh independent workspace.

        This is the causal-fork boundary. It intentionally has no expected
        fingerprint because the destination is new; ordinary restore keeps
        the conflict check and mutation-ledger path in ``WorkspaceCapability``.
        """
        return await self.restore(checkpoint_id, workspace_root)

    def _restore_sync(
        self,
        *,
        checkpoint_id: str,
        workspace_root: str,
        expected_fingerprint: str | None,
    ) -> dict:
        manifest_path = self._root / f"{checkpoint_id}.manifest.json"
        if not manifest_path.is_file():
            raise KeyError(f"Unknown checkpoint: {checkpoint_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot = self._root / checkpoint_id
        if not snapshot.is_dir():
            raise FileNotFoundError(f"checkpoint data missing: {snapshot}")

        target = Path(workspace_root)
        target.mkdir(parents=True, exist_ok=True)
        target = target.resolve()
        if expected_fingerprint is not None:
            current = _fingerprint(_tree_manifest(target))
            if current != expected_fingerprint:
                raise CheckpointConflict("workspace changed since checkpoint operation was planned")

        file_manifest = _manifest_entries(manifest, snapshot)
        _verify_snapshot(snapshot, file_manifest)

        restored = 0
        for rel, entry in sorted(file_manifest.items()):
            src_path = _safe_join(snapshot, rel)
            dst_path = _safe_join(target, rel)
            _remove_conflicting_path(dst_path)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if entry["kind"] == "symlink":
                symlink_target = entry.get("target")
                if not isinstance(symlink_target, str):
                    raise ValueError(f"checkpoint symlink target is invalid: {rel}")
                os.symlink(symlink_target, dst_path)
            else:
                shutil.copy2(src_path, dst_path)
                try:
                    mode = entry.get("mode", 0o600)
                    os.chmod(dst_path, int(mode) if isinstance(mode, int) else 0o600)
                except OSError:
                    _logger.debug("could not restore mode for %s", dst_path)
            restored += 1

        captured_files = set(file_manifest)
        captured_dirs = {""}
        for rel in captured_files:
            parts = rel.split("/")
            captured_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        removed = 0
        for cur in sorted(_tree_paths(target), reverse=True):
            rel = cur.relative_to(target).as_posix()
            parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if parent_rel in captured_dirs and rel not in captured_files:
                _remove_conflicting_path(cur)
                removed += 1

        summary = {
            "checkpoint_id": checkpoint_id,
            "workspace_root": str(target),
            "restored_files": restored,
            "removed_files": removed,
            "workspace_fingerprint": _fingerprint(_tree_manifest(target)),
        }
        _logger.info("restored checkpoint %s: %s", checkpoint_id, summary)
        return summary


def _tree_paths(root: Path) -> list[Path]:
    """List files and symlinks without traversing symlinked directories."""
    return tree_paths(root)


def _tree_manifest(root: Path) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    for path in _tree_paths(root):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries[rel] = {
                "kind": "symlink",
                "target": os.readlink(path),
                "mode": stat.S_IMODE(path.lstat().st_mode),
            }
            continue
        entries[rel] = {
            "kind": "file",
            "sha256": _hash_file(path),
            "size": path.stat().st_size,
            "mode": stat.S_IMODE(path.stat().st_mode),
        }
    return entries


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(entries: dict[str, dict[str, object]]) -> str:
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _manifest_entries(
    manifest: dict,
    snapshot: Path,
) -> dict[str, dict[str, object]]:
    entries = manifest.get("file_manifest")
    if isinstance(entries, dict):
        return {str(rel): dict(value) for rel, value in entries.items()}
    # Backward compatibility for version-1 snapshots. Their integrity is
    # upgraded at restore time by hashing the captured files.
    return {
        str(rel): {
            "kind": "file",
            "sha256": _hash_file(_safe_join(snapshot, str(rel))),
            "size": _safe_join(snapshot, str(rel)).stat().st_size,
            "mode": stat.S_IMODE(_safe_join(snapshot, str(rel)).stat().st_mode),
        }
        for rel in manifest.get("files", [])
    }


def _verify_snapshot(snapshot: Path, entries: dict[str, dict[str, object]]) -> None:
    for rel, entry in entries.items():
        path = _safe_join(snapshot, rel)
        kind = entry.get("kind")
        if kind == "symlink":
            if not path.is_symlink() or os.readlink(path) != entry.get("target"):
                raise ValueError(f"checkpoint symlink integrity failure: {rel}")
        elif kind == "file":
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"checkpoint file missing or invalid: {rel}")
            if _hash_file(path) != entry.get("sha256"):
                raise ValueError(f"checkpoint content integrity failure: {rel}")
        else:
            raise ValueError(f"checkpoint entry has unknown kind: {rel}")


def _safe_join(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"unsafe checkpoint path: {relative}")
    root_resolved = root.resolve()
    candidate = (root / value).resolve(strict=False)
    if os.path.commonpath((str(root_resolved), str(candidate))) != str(root_resolved):
        raise ValueError(f"checkpoint path escapes root: {relative}")
    current = root
    for part in value.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"checkpoint path traverses symlink: {relative}")
    return candidate


def _remove_conflicting_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _write_private_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        _logger.debug("could not restrict checkpoint manifest permissions: %s", path)


async def _run_worker(operation: str, **kwargs) -> dict:
    """Run blocking checkpoint I/O in a short-lived child process.

    The service event loop must not perform copytree/rglob/hash work itself.
    A child process also avoids coupling checkpoint latency to asyncio's
    process-global default thread executor.
    """
    command = [sys.executable, "-m", "athena.causal.checkpoint_worker", operation]
    for key, value in kwargs.items():
        if value is None:
            continue
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    process = await asyncio.create_subprocess_exec(  # architecture-lint: allow subprocess-outside-approved-backends reason=owned checkpoint worker
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    try:
        payload = json.loads(stdout.decode("utf-8")) if stdout else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"checkpoint worker returned invalid output: {stderr.decode(errors='replace')}"
        ) from exc
    if process.returncode:
        message = str(payload.get("error") or stderr.decode(errors="replace"))
        error_type = str(payload.get("error_type") or "RuntimeError")
        error = {
            "KeyError": KeyError,
            "FileNotFoundError": FileNotFoundError,
            "NotADirectoryError": NotADirectoryError,
            "ValueError": ValueError,
            "CheckpointConflict": CheckpointConflict,
        }.get(error_type, RuntimeError)
        raise error(message)
    if not isinstance(payload, dict):
        raise TypeError("checkpoint worker returned a non-object result")
    return payload


__all__ = ["CheckpointConflict", "CheckpointManager"]
