"""Versioned, bounded framing primitives for the SSH supervisor boundary."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

PROTOCOL_NAME = "athena-ssh-supervisor"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_LENGTH_LINE_BYTES = 64


class SSHProtocolError(ValueError):
    """A supervisor frame or handshake is malformed or exceeds policy."""


def handshake(
    token: str,
    operation: str,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    runtime: str | None = None,
    language: str | None = None,
    runtime_identity: str | None = None,
    worker_source_sha: str | None = None,
    session_nonce: str | None = None,
    authority_digest: str | None = None,
) -> dict[str, Any]:
    """Build the authenticated supervisor envelope.

    The first protocol revision accepted only token/op.  The optional
    identity fields preserve compatibility with old supervisors while making
    the complete boundary available to new clients.  Once one identity field
    is supplied, validation requires the whole identity set.
    """
    value: dict[str, Any] = {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "token": str(token),
        "op": str(operation),
    }
    identity = {
        "session_id": session_id,
        "task_id": task_id,
        "runtime": runtime,
        "language": language or runtime,
        "runtime_identity": runtime_identity,
        "worker_source_sha256": worker_source_sha,
        "session_nonce": session_nonce,
        "authority_digest": authority_digest,
    }
    if any(item is not None for item in identity.values()):
        value.update({key: str(item or "") for key, item in identity.items()})
    return value


def validate_handshake(value: Mapping[str, Any]) -> None:
    if value.get("protocol") != PROTOCOL_NAME or value.get("version") != PROTOCOL_VERSION:
        raise SSHProtocolError("unsupported SSH supervisor protocol")
    if not isinstance(value.get("token"), str) or not value["token"]:
        raise SSHProtocolError("SSH supervisor handshake requires a token")
    if not isinstance(value.get("op"), str) or not value["op"]:
        raise SSHProtocolError("SSH supervisor handshake requires an operation")
    if len(value["token"]) > 4096 or len(value["op"]) > 128:
        raise SSHProtocolError("SSH supervisor handshake field is too long")
    identity_keys = (
        "session_id",
        "task_id",
        "runtime",
        "language",
        "runtime_identity",
        "worker_source_sha256",
        "session_nonce",
        "authority_digest",
    )
    present = [key for key in identity_keys if key in value]
    if present and any(
        not isinstance(value.get(key), str) or not value[key] for key in identity_keys
    ):
        raise SSHProtocolError("SSH supervisor handshake identity is incomplete")
    for key in ("worker_source_sha256", "authority_digest"):
        if key in value and re.fullmatch(r"[0-9a-f]{64}", str(value[key])) is None:
            raise SSHProtocolError(f"SSH supervisor {key} is not a SHA-256 digest")


def encode_frame(value: Mapping[str, Any]) -> bytes:
    payload = json.dumps(dict(value), separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise SSHProtocolError("SSH supervisor frame exceeds the maximum size")
    return str(len(payload)).encode("ascii") + b"\n" + payload


def parse_length_line(line: bytes) -> int:
    if len(line) > MAX_LENGTH_LINE_BYTES or not line.endswith(b"\n"):
        raise SSHProtocolError("invalid SSH supervisor frame length line")
    try:
        length = int(line.strip())
    except (TypeError, ValueError) as exc:
        raise SSHProtocolError("invalid SSH supervisor frame length") from exc
    if length < 0 or length > MAX_FRAME_BYTES:
        raise SSHProtocolError("SSH supervisor frame exceeds the maximum size")
    return length


def decode_frame(length_line: bytes, payload: bytes) -> dict[str, Any]:
    """Decode one bounded JSON frame and reject truncation/non-UTF-8 data."""
    length = parse_length_line(length_line)
    if len(payload) != length:
        raise SSHProtocolError("truncated SSH supervisor frame")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SSHProtocolError("invalid SSH supervisor JSON frame") from exc
    if not isinstance(value, dict):
        raise SSHProtocolError("SSH supervisor frame must be an object")
    return value


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_LENGTH_LINE_BYTES",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "SSHProtocolError",
    "encode_frame",
    "decode_frame",
    "handshake",
    "parse_length_line",
    "validate_handshake",
]
