"""Pack source validation, bounded acquisition, and archive safety.

This module owns untrusted pack-source mechanics.  It does not install,
enable, or activate a pack; those lifecycle decisions remain with
``PackManager``.
"""

from __future__ import annotations

import hashlib
import os
import re
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Mapping

from athena.network import pinned_sync_transport, validate_target
from athena.packs.models import PackManifest

_PACK_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}(?:[-+][A-Za-z0-9.-]+)?$")
_EFFECTS = frozenset(
    {
        "READ_LOCAL",
        "WRITE_LOCAL",
        "EXECUTE",
        "SPAWN_PROCESS",
        "NETWORK_READ",
        "NETWORK_WRITE",
        "SECRET_READ",
        "DELETE",
        "PRIVILEGED",
        "EXTERNAL_MESSAGE",
        "EXTERNAL_PUBLISH",
        "COMPUTER_INPUT",
        "FINANCIAL",
    }
)
_PROVIDED_FILES = {
    "skills": (".md",),
    "workflows": (".json",),
    "capabilities": (".json",),
    "mcp_servers": (".json", ".toml"),
    "instruments": (".json",),
    "hooks": (".json", ".toml"),
}


def parse_manifest(raw: Mapping[str, Any]) -> PackManifest:
    pack_id = str(raw.get("id") or "")
    version = str(raw.get("version") or "")
    if not _PACK_ID.fullmatch(pack_id):
        raise ValueError("pack id must be lowercase and contain only letters, digits, _, ., -")
    if not _VERSION.fullmatch(version):
        raise ValueError("pack version must be numeric semver-like text")
    provides_raw = raw.get("provides") or {}
    if not isinstance(provides_raw, Mapping):
        raise ValueError("pack provides must be a table")
    provides: dict[str, tuple[str, ...]] = {}
    for kind in _PROVIDED_FILES:
        value = provides_raw.get(kind) or ()
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"pack provides.{kind} must be an array")
        provides[kind] = tuple(str(item) for item in value)
    authority = raw.get("authority") or {}
    if not isinstance(authority, Mapping):
        raise ValueError("pack authority must be a table")
    requested = tuple(str(item) for item in authority.get("requested_effects") or ())
    unknown = set(requested) - _EFFECTS
    if unknown:
        raise ValueError("pack requests unknown effects: " + ", ".join(sorted(unknown)))
    integrity = raw.get("integrity") or {}
    declared = integrity.get("sha256")
    if declared is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", str(declared)):
        raise ValueError("pack integrity.sha256 must be a 64-character hex digest")
    return PackManifest(
        id=pack_id,
        version=version,
        publisher=str(raw.get("publisher") or ""),
        minimum_athena=(str(raw["minimum_athena"]) if raw.get("minimum_athena") else None),
        provides=provides,
        requested_effects=requested,
        declared_integrity=str(declared).lower() if declared else None,
        metadata=dict(raw.get("metadata") or {}),
    )


def govern_remote_target(source_url: str, network_policy: str | object | None):
    """Apply the common outbound target gate before reading pack bytes."""
    target, error = validate_target(source_url, network_policy)
    if error:
        raise PermissionError(error)
    if target is None:
        raise PermissionError("network target validation failed")
    return target


def download_remote(
    source_url: str,
    *,
    target,
    max_bytes: int,
    timeout: float,
    user_agent: str,
    destination: Path | None = None,
) -> bytes:
    """Read one bounded, non-redirecting pack response.

    Restricted targets use the addresses validated before the request,
    closing the DNS-rebinding window.  The compatibility path retains the
    small urllib test seam but still rejects redirects.
    """
    if max_bytes <= 0:
        raise ValueError("remote response size limit must be positive")
    headers = {"User-Agent": user_agent}
    if target.addresses:
        import httpx

        transport = pinned_sync_transport(target.hostname, target.addresses)
        try:
            with httpx.Client(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                headers=headers,
            ) as client:
                with client.stream("GET", source_url) as response:
                    if response.status_code >= 300:
                        raise ValueError(
                            f"remote pack fetch returned HTTP {response.status_code}; redirects are not followed"
                        )
                    return bounded_response(
                        response.iter_bytes(), max_bytes, destination=destination
                    )
        finally:
            transport.close()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(source_url, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        if int(getattr(response, "status", 200)) >= 300:
            raise ValueError(
                f"remote pack fetch returned HTTP {getattr(response, 'status', 0)}; redirects are not followed"
            )
        return bounded_response(
            iter(lambda: response.read(1024 * 1024), b""), max_bytes, destination=destination
        )


def bounded_response(chunks, max_bytes: int, *, destination: Path | None = None) -> bytes:
    if destination is not None:
        size = 0
        with destination.open("wb") as output:
            for chunk in chunks:
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("remote pack response exceeds size limit")
                output.write(bytes(chunk))
        return b""
    values: list[bytes] = []
    size = 0
    for chunk in chunks:
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("remote pack response exceeds size limit")
        values.append(bytes(chunk))
    return b"".join(values)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def tree_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def extract_archive_safely(
    archive: Path,
    destination: Path,
    *,
    max_total_bytes: int = 256 * 1024 * 1024,
    max_member_bytes: int = 64 * 1024 * 1024,
    max_members: int = 10_000,
    max_path_depth: int = 32,
) -> None:
    """Extract an archive with traversal, link, and decompression quotas."""
    if max_total_bytes <= 0 or max_member_bytes <= 0 or max_members <= 0:
        raise ValueError("archive extraction limits must be positive")
    total_bytes = 0
    member_count = 0

    def admit(name: str, declared_size: int) -> Path:
        nonlocal total_bytes, member_count
        member_count += 1
        if member_count > max_members:
            raise ValueError("remote pack archive contains too many members")
        if len(Path(name).parts) > max_path_depth:
            raise ValueError("remote pack archive path is too deep")
        if declared_size < 0 or declared_size > max_member_bytes:
            raise ValueError("remote pack archive member exceeds size limit")
        total_bytes += declared_size
        if total_bytes > max_total_bytes:
            raise ValueError("remote pack archive exceeds uncompressed size limit")
        return safe_archive_target(destination, name)

    def copy_bounded(source, target: Path, declared_size: int) -> None:
        written = 0
        with target.open("wb") as out:
            while True:
                chunk = source.read(min(1024 * 1024, max_member_bytes - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > max_member_bytes or written > declared_size:
                    raise ValueError("remote pack archive member expands beyond its declared size")
                out.write(chunk)
        if written != declared_size:
            raise ValueError("remote pack archive member size does not match its declaration")

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zip_handle:
            for info in zip_handle.infolist():
                name = str(info.filename).replace("\\", "/")
                target = admit(name, int(info.file_size))
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("remote pack archive may not contain links")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_handle.open(info) as source:
                    copy_bounded(source, target, int(info.file_size))
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tar_handle:
            for member in tar_handle.getmembers():
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise ValueError("remote pack archive may contain only regular files")
                target = admit(member.name, 0 if member.isdir() else int(member.size))
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                extracted = tar_handle.extractfile(member)
                if extracted is None:
                    raise ValueError("remote pack archive contains an unreadable file")
                with extracted:
                    copy_bounded(extracted, target, int(member.size))
        return
    raise ValueError("remote pack must be a zip or tar archive")


def safe_archive_target(destination: Path, name: str) -> Path:
    if not name or name.startswith("/"):
        raise ValueError("remote pack archive contains an absolute path")
    target = (destination / name).resolve()
    try:
        target.relative_to(destination.resolve())
    except ValueError as exc:
        raise ValueError("remote pack archive contains a path traversal") from exc
    return target


def find_pack_root(extracted: Path) -> Path:
    candidates = [path.parent for path in extracted.rglob("athena.pack.toml")]
    if len(candidates) != 1:
        raise ValueError("remote pack archive must contain exactly one athena.pack.toml")
    root = candidates[0]
    if not root.is_dir():
        raise ValueError("remote pack manifest root is not a directory")
    return root


def validated_source_for_remote(source: Path) -> None:
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("remote pack may not contain symbolic links")


def validate_provided_files(source: Path, manifest: PackManifest) -> None:
    for kind, names in manifest.provides.items():
        suffixes = _PROVIDED_FILES[kind]
        for name in names:
            path = (source / name).resolve()
            if not inside(source, path) or path == source or not path.is_file():
                raise ValueError(f"pack contribution is not a regular in-pack file: {name}")
            if path.suffix.lower() not in suffixes:
                raise ValueError(f"pack {kind} contribution has unsupported type: {name}")


def directory_integrity(root: Path) -> str:
    digest = hashlib.sha256()
    # The manifest may contain the digest of the payload. Including the
    # manifest itself would make the declared hash self-referential.
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.relative_to(root).as_posix() != "athena.pack.toml"
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def inside(root: Path, target: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(target))) == str(root)
    except ValueError:
        return False


__all__ = [
    "bounded_response",
    "directory_integrity",
    "download_remote",
    "extract_archive_safely",
    "file_sha256",
    "find_pack_root",
    "govern_remote_target",
    "inside",
    "parse_manifest",
    "safe_archive_target",
    "tree_size",
    "validated_source_for_remote",
    "validate_provided_files",
]
