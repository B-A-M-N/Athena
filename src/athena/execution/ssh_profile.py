"""Operator-owned SSH profile validation for the SSH execution backend."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SAFE_REMOTE_PATH = re.compile(r"^[A-Za-z0-9_./~-]+$")


@dataclass(frozen=True)
class SSHProfile:
    """An operator-configured, known-host-verified SSH backend identity."""

    name: str
    host: str
    user: str
    port: int = 22
    credential_id: str | None = None
    identity_file: str | None = None
    known_hosts: str = ""
    remote_root: str = "~/athena-workspaces"
    connect_timeout: float = 15.0

    def __post_init__(self) -> None:
        for field_name in ("name", "host", "user"):
            value = str(getattr(self, field_name)).strip()
            if not value or any(char.isspace() for char in value):
                raise ValueError(f"SSH profile {field_name} must be a non-empty token")
            object.__setattr__(self, field_name, value)
        if _SAFE_HOST.fullmatch(self.host) is None or _SAFE_SEGMENT.fullmatch(self.user) is None:
            raise ValueError("SSH profile host/user contains unsafe characters")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("SSH profile port must be between 1 and 65535")
        if not str(self.known_hosts).strip():
            raise ValueError("SSH profile requires a known_hosts path")
        root = str(self.remote_root).strip()
        if (
            not root
            or "\x00" in root
            or ".." in root.split("/")
            or _SAFE_REMOTE_PATH.fullmatch(root) is None
        ):
            raise ValueError("SSH profile remote_root is invalid")
        object.__setattr__(self, "remote_root", root)
        if self.credential_id is None and self.identity_file is None:
            raise ValueError("SSH profile requires credential_id or identity_file")
        object.__setattr__(self, "connect_timeout", max(1.0, float(self.connect_timeout)))


__all__ = ["SSHProfile"]
