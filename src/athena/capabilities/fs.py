"""Structured ``fs`` capability (BUILDSPEC sections 39-40, BHV-052..055).

Canonical name: ``fs``. ``files.py`` re-exports this same class for backward
compatibility. Operations:

    read | write | patch | list | stat | mkdir | copy | move | delete

All writes produce mutation records; expected-content-hash patch avoids
silent overwrite (Scenario E ConflictError). Every structural write is
reported via the result ``metadata["mutation"]`` so the dispatcher records a
single MutationRef through the MutationStore without double-counting
(BHV-055 / BHV-043).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import secrets
import threading

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    InvocationContext,
)
from athena.protocol.errors import FilesystemConflict, PolicyDenied
from athena.protocol.tasks import PathRule, WorkspaceSpec

_OPERATIONS = (
    "read",
    "write",
    "patch",
    "list",
    "stat",
    "mkdir",
    "copy",
    "move",
    "delete",
)

_MUTATING_OPERATIONS = frozenset(("write", "patch", "mkdir", "copy", "move", "delete"))

_LOCK = threading.RLock()
_PATH_LOCKS: dict[str, asyncio.Lock] = {}


def _path_lock(path: str) -> asyncio.Lock:
    """Per-realpath lock to serialize writes/patches to the same target."""
    real = os.path.realpath(os.path.abspath(path))
    with _LOCK:
        return _PATH_LOCKS.setdefault(real, asyncio.Lock())

_PATH = {"type": "string", "minLength": 1}


def _operation_schema(operation: str, properties: dict, required: tuple[str, ...]) -> dict:
    return {
        "type": "object",
        "properties": {
            "operation": {"const": operation},
            "path": _PATH,
            **properties,
        },
        "required": list(required),
        "additionalProperties": False,
    }


_INPUT_SCHEMA = {
    "oneOf": [
        _operation_schema(
            "read",
            {"encoding": {"type": "string", "enum": ["text", "base64"]}},
            ("operation", "path"),
        ),
        {
            "oneOf": [
                _operation_schema(
                    "write",
                    {
                        "content": {"type": "string"},
                        "create_dirs": {"type": "boolean"},
                    },
                    ("operation", "path", "content"),
                ),
                _operation_schema(
                    "write",
                    {
                        "content_base64": {"type": "string", "minLength": 1},
                        "create_dirs": {"type": "boolean"},
                    },
                    ("operation", "path", "content_base64"),
                ),
            ],
        },
        {
            "oneOf": [
                _operation_schema(
                    "patch",
                    {
                        "new_content": {"type": "string"},
                        "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
                    },
                    ("operation", "path", "new_content"),
                ),
                _operation_schema(
                    "patch",
                    {
                        "new_content_base64": {"type": "string", "minLength": 1},
                        "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
                    },
                    ("operation", "path", "new_content_base64"),
                ),
            ],
        },
        _operation_schema("list", {}, ("operation", "path")),
        _operation_schema("stat", {}, ("operation", "path")),
        _operation_schema("mkdir", {}, ("operation", "path")),
        _operation_schema(
            "copy", {"destination": _PATH}, ("operation", "path", "destination")
        ),
        _operation_schema(
            "move", {"destination": _PATH}, ("operation", "path", "destination")
        ),
        _operation_schema("delete", {}, ("operation", "path")),
    ],
}


class FilesystemCapability:
    """Structured fs operations enforcing workspace writable/readable scopes."""

    descriptor = CapabilityDescriptor(
        id="fs",
        description=(
            "Structured fs: read_file, write_file, patch_file, list_dir, stat, "
            "mkdir, copy, move, delete. Writes respect workspace writable path "
            "scopes and report mutations for the ledger."
        ),
        input_schema=_INPUT_SCHEMA,
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL, EffectClass.DELETE}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self,
        workspace: "WorkspaceSpec | None" = None,
        event_sink=None,
        artifact_store=None,
        mutation_store=None,
    ) -> None:
        self.workspace = workspace
        self._event_sink = event_sink
        self.artifact_store = artifact_store
        self.mutation_store = mutation_store

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        output_accumulator=None,
        context: "InvocationContext | None" = None,
    ) -> CapabilityResult:
        args = request.arguments or {}
        op = args.get("operation")
        path = args.get("path")
        ws = context.workspace if context else self.workspace
        if ws is None:
            return _fail(request, "no workspace bound")
        if op not in _OPERATIONS:
            return _fail(request, f"unknown operation {op!r}")
        if not path:
            return _fail(request, "path required")
        try:
            abs_path = self._resolve(path, ws)
        except PolicyDenied as e:
            return _fail(request, str(e))

        if op == "copy":
            self._check_readable(abs_path, ws)
        elif op in _MUTATING_OPERATIONS:
            self._check_writable(abs_path, ws)
        else:
            self._check_readable(abs_path, ws)

        handler = {
            "read": self._read,
            "write": self._write,
            "patch": self._patch,
            "list": self._list,
            "stat": self._stat,
            "mkdir": self._mkdir,
            "copy": self._copy,
            "move": self._move,
            "delete": self._delete,
        }[op]

        try:
            return await handler(request, args, abs_path, ws)
        except FilesystemConflict as e:
            return _fail(request, str(e))
        except (PolicyDenied, OSError) as e:
            return _fail(request, str(e))

    # ------------------------------------------------------------------ #
    # Operations
    # ------------------------------------------------------------------ #
    async def _read(self, request, args, path, ws):
        try:
            with self._safe_open_read(path, ws) as f:
                data = f.read()
        except PolicyDenied as e:
            return _fail(request, str(e))
        except FileNotFoundError:
            return _fail(request, f"no such file: {path}")
        except IsADirectoryError:
            return _fail(request, f"is a directory: {path}")
        except PermissionError:
            return _fail(request, f"permission denied: {path}")
        encoding = args.get("encoding", "text")
        if encoding == "base64":
            return _ok(
                request,
                base64.b64encode(data).decode("ascii"),
                metadata={"encoding": "base64", "bytes": len(data)},
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return _fail(request, "file is not valid UTF-8; read it with encoding=base64")
        return _ok(request, text, metadata={"encoding": "utf-8", "bytes": len(data)})

    async def _write(self, request, args, path, ws):
        try:
            content = _decode_content(args, "content", "content_base64")
        except ValueError as e:
            return _fail(request, str(e))
        if content is None:
            return _fail(request, "write requires content or content_base64")
        create_dirs = args.get("create_dirs", False)
        lock = _path_lock(path)
        async with lock:
            before_ref, before = await self._capture_before(request.task_id, path, ws)
            intent_id = await self._intent(
                request.task_id, path, "write", before_ref,
                _inverse("write", path, before_ref),
            )
            # Mark STARTED after PLANNED but before the side effect
            if self.mutation_store is not None and intent_id is not None:
                await self.mutation_store.mark_started(intent_id)
            try:
                if create_dirs:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                after = _atomic_write(path, content, ws)
            except OSError as e:
                await self._abort(intent_id, str(e))
                return _fail(request, str(e))
            reversible = before is None or before_ref is not None
            await self._complete(intent_id, after, reversible,
                                 _inverse("write", path, before_ref))
        return _ok(request, f"wrote {len(content)} bytes",
                   mutation=_mutation("write", path, before, after,
                                      before_ref, reversible, intent_id))

    async def _patch(self, request, args, path, ws):
        expected = args.get("expected_sha256")
        try:
            new_content = _decode_content(args, "new_content", "new_content_base64")
        except ValueError as e:
            return _fail(request, str(e))
        if new_content is None:
            return _fail(request, "patch requires new_content")
        lock = _path_lock(path)
        async with lock:
            before_ref, before = await self._capture_before(request.task_id, path, ws)
            if expected and (before is None or before != expected):
                raise FilesystemConflict(
                    f"expected sha256 {expected} but file has changed"
                )
            intent_id = await self._intent(
                request.task_id, path, "patch", before_ref,
                _inverse("patch", path, before_ref),
            )
            if self.mutation_store is not None and intent_id is not None:
                await self.mutation_store.mark_started(intent_id)
            try:
                after = _atomic_write(path, new_content, ws)
            except OSError as e:
                await self._abort(intent_id, str(e))
                return _fail(request, str(e))
            reversible = before is None or before_ref is not None
            await self._complete(intent_id, after, reversible,
                                 _inverse("patch", path, before_ref))
        return _ok(request, "patched",
                   mutation=_mutation("patch", path, before, after,
                                      before_ref, reversible, intent_id))

    async def _list(self, request, args, path, ws):
        try:
            fd = self._safe_open_directory(path, ws)
            try:
                entries = sorted(os.listdir(fd))
            finally:
                os.close(fd)
        except FileNotFoundError:
            return _fail(request, f"no such dir: {path}")
        except NotADirectoryError:
            return _fail(request, f"not a directory: {path}")
        return _ok(request, "\n".join(entries))

    async def _stat(self, request, args, path, ws):
        try:
            fd = self._safe_open_stat(path, ws)
            try:
                st = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError as e:
            return _fail(request, str(e))
        import stat as statmod
        info = {
            "size": st.st_size,
            "is_dir": statmod.S_ISDIR(st.st_mode),
            "is_file": statmod.S_ISREG(st.st_mode),
            "mtime": st.st_mtime,
        }
        return _ok(request, json.dumps(info))

    async def _mkdir(self, request, args, path, ws):
        # Check existence BEFORE recording intent (write-ahead)
        existed_before = os.path.isdir(path)
        # If the directory already existed, this is a no-op mutation
        reversible = not existed_before
        intent_id = await self._intent(request.task_id, path, "mkdir", None,
                                       _inverse("mkdir", path, None, existed_before=existed_before))
        if self.mutation_store is not None and intent_id is not None:
            await self.mutation_store.mark_started(intent_id)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            await self._abort(intent_id, str(e))
            return _fail(request, str(e))
        await self._complete(intent_id, None, reversible,
                             _inverse("mkdir", path, None, existed_before=existed_before))
        if existed_before:
            return _ok(request, "mkdir ok (already existed)")
        return _ok(request, "mkdir ok",
                   mutation=_mutation("mkdir", path, None, None, None, reversible, intent_id))

    async def _copy(self, request, args, path, ws):
        dest = self._resolve(args.get("destination"), ws)
        self._check_writable(dest, ws)
        dest_before_ref, dest_before = await self._capture_before(request.task_id, dest, ws)
        intent_id = await self._intent(
            request.task_id, dest, "copy", dest_before_ref,
            _inverse("copy", dest, dest_before_ref),
        )
        if self.mutation_store is not None and intent_id is not None:
            await self.mutation_store.mark_started(intent_id)
        try:
            with self._safe_open_read(path, ws) as source:
                content = source.read()
            after = _atomic_write(dest, content, ws)
        except OSError as e:
            await self._abort(intent_id, str(e))
            return _fail(request, str(e))
        await self._complete(intent_id, after, True, _inverse("copy", dest, dest_before_ref))
        return _ok(request, f"copied to {dest}",
                   mutation=_mutation("copy", dest, dest_before, after,
                                      dest_before_ref, True, intent_id))

    async def _move(self, request, args, path, ws):
        dest = self._resolve(args.get("destination"), ws)
        self._check_writable(dest, ws)
        src_ref, src_before = await self._capture_before(request.task_id, path, ws)
        dest_ref, dest_before = await self._capture_before(request.task_id, dest, ws)
        intent_id = await self._intent(
            request.task_id, path, "move", src_ref,
            _inverse("move", path, src_ref, dest=dest, dest_before_ref=dest_ref),
        )
        if self.mutation_store is not None and intent_id is not None:
            await self.mutation_store.mark_started(intent_id)
        source_directory = None
        destination_directory = None
        try:
            source_directory = self._safe_open_directory(os.path.dirname(path), ws)
            destination_directory = self._safe_open_directory(os.path.dirname(dest), ws)
            os.replace(
                os.path.basename(path),
                os.path.basename(dest),
                src_dir_fd=source_directory,
                dst_dir_fd=destination_directory,
            )
        except (OSError, PolicyDenied) as e:
            await self._abort(intent_id, str(e))
            return _fail(request, str(e))
        finally:
            if source_directory is not None:
                os.close(source_directory)
            if destination_directory is not None:
                os.close(destination_directory)
        after = src_before
        # Reversible only if we can restore both source and destination
        reversible = (src_before is None or src_ref is not None) and (dest_before is None or dest_ref is not None)
        await self._complete(intent_id, after, reversible,
                             _inverse("move", path, src_ref, dest=dest, dest_before_ref=dest_ref))
        return _ok(request, f"moved to {dest}",
                   mutation=_mutation("move", path, src_before, after,
                                      src_ref, reversible, intent_id, dest=dest))

    async def _delete(self, request, args, path, ws):
        if os.path.isdir(path) and not os.path.islink(path):
            return _fail(request, "refusing recursive directory delete")
        before_ref, before = await self._capture_before(request.task_id, path, ws)
        intent_id = await self._intent(
            request.task_id, path, "delete", before_ref,
            _inverse("delete", path, before_ref),
        )
        if self.mutation_store is not None and intent_id is not None:
            await self.mutation_store.mark_started(intent_id)
        directory = None
        try:
            directory = self._safe_open_directory(os.path.dirname(path), ws)
            os.unlink(os.path.basename(path), dir_fd=directory)
        except (OSError, PolicyDenied) as e:
            await self._abort(intent_id, str(e))
            return _fail(request, str(e))
        finally:
            if directory is not None:
                os.close(directory)
        reversible = before_ref is not None
        await self._complete(intent_id, None, reversible, _inverse("delete", path, before_ref))
        return _ok(request, "deleted",
                   mutation=_mutation("delete", path, before, None,
                                      before_ref, reversible, intent_id))

    # ------------------------------------------------------------------ #
    # Scope enforcement
    # ------------------------------------------------------------------ #
    def _real(self, p: str) -> str:
        return os.path.realpath(os.path.abspath(p))

    def _resolve(self, path: str, ws: WorkspaceSpec) -> str:
        if os.path.isabs(path):
            candidate = path
        else:
            candidate = os.path.join(self._real(ws.root), path)
        real = self._real(candidate)
        root = self._real(ws.root)
        if real != root and not real.startswith(root + os.sep):
            raise PolicyDenied(f"path escapes workspace: {path}")
        return real

    def _check_readable(self, path: str, ws: WorkspaceSpec) -> None:
        if not self._within_root(path, ws):
            raise PolicyDenied(f"not readable: {path}")
        if ws.readable:
            if not self._within_rule(path, ws.readable):
                raise PolicyDenied(f"not readable: {path}")

    def _check_writable(self, path: str, ws: WorkspaceSpec) -> None:
        if not self._within_root(path, ws):
            raise PolicyDenied(f"outside writable workspace: {path}")
        if not self._within_rule(path, ws.writable):
            raise PolicyDenied(f"not in writable scope: {path}")

    def _safe_open_read(self, path: str, ws: WorkspaceSpec):
        """Open ``path`` O_NOFOLLOW and re-validate the fd resolves inside the
        workspace, closing the TOCTOU window between policy check and read.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            real = self._real(f"/proc/self/fd/{fd}")
            if not self._within_root(real, ws):
                raise PolicyDenied(f"path escapes workspace: {path}")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def _safe_open_directory(self, path: str, ws: WorkspaceSpec) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            real = self._real(f"/proc/self/fd/{fd}")
            if not self._within_root(real, ws):
                raise PolicyDenied(f"path escapes workspace: {path}")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _safe_open_stat(self, path: str, ws: WorkspaceSpec) -> int:
        flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            real = self._real(f"/proc/self/fd/{fd}")
            if not self._within_root(real, ws):
                raise PolicyDenied(f"path escapes workspace: {path}")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _within_root(self, path: str, ws: WorkspaceSpec) -> bool:
        root = self._real(ws.root)
        real = self._real(path)
        return real == root or real.startswith(root + os.sep)

    def _within_rule(self, path: str, rules: tuple[PathRule, ...]) -> bool:
        real = self._real(path)
        if not rules:
            return True
        matched_allow = False
        for rule in rules:
            if _match_path(real, rule.path):
                if not rule.allow:
                    return False
                matched_allow = True
        return matched_allow

    async def _capture_before(self, task_id, path, ws=None):
        if not _exists(path) or os.path.isdir(path):
            return None, None
        if ws is not None:
            with self._safe_open_read(path, ws) as f:
                data = f.read()
        else:
            with open(path, "rb") as f:
                data = f.read()
        h = hashlib.sha256(data).hexdigest()
        ref = None
        if self.artifact_store is not None:
            try:
                saved = await self.artifact_store.save(
                    task_id=task_id,
                    content=data,
                    mime_type="application/octet-stream",
                    producer="fs",
                )
                ref = saved.uri
            except Exception:
                ref = None
        return ref, h

    async def _intent(self, task_id, path, op, before_ref, inverse):
        if self.mutation_store is None:
            return None
        return await self.mutation_store.record_intent(
            task_id=task_id,
            resource=path,
            operation=op,
            before_ref=before_ref,
            inverse=inverse,
            metadata={"capability_id": self.descriptor.id},
        )

    async def _complete(self, intent_id, after, reversible, inverse):
        if self.mutation_store is None or intent_id is None:
            return
        try:
            await self.mutation_store.complete(
                intent_id, after_hash=after, reversible=reversible, inverse=inverse
            )
        except Exception:
            try:
                await self.mutation_store.mark_recovery_required(intent_id)
            except Exception:
                pass
            raise

    async def _abort(self, intent_id, error):
        if self.mutation_store is not None and intent_id is not None:
            try:
                await self.mutation_store.mark_failed(intent_id, error)
            except Exception:
                pass


def _mutation(op: str, path: str, before: str | None, after: str | None,
              before_ref: str | None, reversible: bool,
              intent_id: str | None, dest: str | None = None) -> dict:
    return {
        "resource": path,
        "operation": op,
        "before_hash": before,
        "after_hash": after,
        "before_ref": before_ref,
        "reversible": reversible,
        "mutation_id": intent_id,
        "inverse": _inverse(op, path, before_ref, dest=dest),
        "destination": dest,
    }


def _inverse(op: str, path: str, before_ref: str | None, dest: str | None = None, existed_before: bool = False, dest_before_ref: str | None = None) -> dict:
    target = dest if dest is not None else path
    if op in ("write", "patch"):
        if before_ref:
            return {"op": "restore_from_ref", "target": path, "ref": before_ref}
        return {"op": "delete", "target": path}
    if op == "delete":
        if before_ref:
            return {"op": "create_from_ref", "target": path, "ref": before_ref}
        return {"op": "noop", "target": path}
    if op == "move":
        # Preserve BOTH source and destination pre-state for correct undo
        inv = {"op": "move_restore", "source": target, "target": path}
        if before_ref:
            inv["source_before_ref"] = before_ref
        if dest_before_ref:
            inv["overwritten_destination_ref"] = dest_before_ref
        return inv
    if op == "copy":
        if before_ref:
            return {"op": "restore_from_ref", "target": target, "ref": before_ref}
        return {"op": "delete", "target": target}
    if op == "mkdir":
        if existed_before:
            return {"op": "noop", "target": target}
        return {"op": "rmdir", "target": target}
    return {"op": "none", "target": target}


def _atomic_write(path: str, content: bytes, ws: WorkspaceSpec) -> str:
    """Write ``content`` to ``path`` atomically (temp file + fsync + rename).

    The temp file lives in the same directory so ``os.replace`` stays on one
    filesystem and the target never sits in a partially-written, truncated
    state.
    """
    directory = os.path.dirname(path) or "."
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory, flags)
    try:
        directory_real = os.path.realpath(f"/proc/self/fd/{directory_fd}")
        root_real = os.path.realpath(os.path.abspath(ws.root))
        if directory_real != root_real and not directory_real.startswith(root_real + os.sep):
            raise PolicyDenied(f"path escapes workspace: {path}")
        temp_name = f".athena-{secrets.token_hex(12)}.tmp"
        temp_fd = -1
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temp_fd, "wb") as f:
            temp_fd = -1
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(
            temp_name,
            os.path.basename(path),
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        return hashlib.sha256(content).hexdigest()
    except BaseException:
        if "temp_fd" in locals() and temp_fd >= 0:
            os.close(temp_fd)
        if "temp_name" in locals():
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(directory_fd)


def _match_path(real_path: str, pattern: str) -> bool:
    if pattern.startswith("**/"):
        return real_path.startswith(_real_path(pattern[3:]))
    if pattern.endswith("/**"):
        base = _real_path(pattern[:-3])
        return real_path.startswith(base + os.sep)
    if "*" in pattern:
        import fnmatch
        return fnmatch.fnmatch(real_path, _real_path(pattern))
    base = _real_path(pattern)
    return real_path == base or real_path.startswith(base + os.sep)


def _real_path(p: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def _sha256(path: str) -> str | None:
    if not _exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _exists(path: str) -> bool:
    return os.path.lexists(path)


def _decode_content(args: dict, text_key: str, base64_key: str) -> bytes | None:
    if base64_key in args:
        encoded = args[base64_key]
        if not isinstance(encoded, str):
            raise ValueError(f"{base64_key} must be a base64 string")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError(f"{base64_key} is not valid base64") from None
    if text_key in args:
        content = args[text_key]
        if not isinstance(content, str):
            raise ValueError(f"{text_key} must be a string")
        return content.encode("utf-8")
    return b"" if text_key == "content" else None


def _ok(
    request: CapabilityRequest,
    output: str,
    mutation: dict | None = None,
    metadata: dict | None = None,
) -> CapabilityResult:
    meta = dict(metadata or {})
    if mutation:
        meta["mutation"] = mutation
    return CapabilityResult(
        request.call_id, request.capability_id, CapabilityResultStatus.OK,
        output=output, metadata=meta,
    )


def _fail(request: CapabilityRequest, msg: str) -> CapabilityResult:
    return CapabilityResult(
        request.call_id, request.capability_id, CapabilityResultStatus.FAILED, error=msg,
    )


__all__ = ["FilesystemCapability"]
