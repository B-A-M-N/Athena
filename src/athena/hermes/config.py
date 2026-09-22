"""Hermes-local configuration access.

The referee lifecycle owns only its small configuration slice. Keeping this
adapter in the Hermes domain avoids making the service composition module a
dependency of the optional referee boundary.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Mapping

from athena.protocol.hermes import HermesSupervisionMode

try:
    tomllib = import_module("tomllib")
except ModuleNotFoundError:  # pragma: no cover
    tomllib = import_module("tomli")


@dataclass(frozen=True)
class HermesSettings:
    enabled: bool = False
    managed: bool = False
    endpoint: str = "http://127.0.0.1:8643"
    profile: str = "athena-referee"
    timeout_seconds: float = 60.0
    credential_id: str | None = None
    runtime_root: str | None = None
    self_host_supervision: str = HermesSupervisionMode.OFF.value

    @property
    def supervision_mode(self) -> HermesSupervisionMode:
        return HermesSupervisionMode(self.self_host_supervision)

    @property
    def transport_enabled(self) -> bool:
        return self.enabled

    @property
    def hermes_referee(self) -> "HermesSettings":
        """Compatibility view matching the application config shape."""
        return self


def global_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "athena" / "config.toml"
    return Path.home() / ".config" / "athena" / "config.toml"


def load_toml_file(path: str | Path) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_file():
        return {}
    with candidate.open("rb") as handle:
        return tomllib.load(handle)


def load_settings(path: str | Path) -> HermesSettings:
    raw = load_toml_file(path).get("hermes_referee", {})
    if not isinstance(raw, Mapping):
        return HermesSettings()
    mode = raw.get("self_host_supervision")
    if mode is None:
        mode = (
            HermesSupervisionMode.REQUIRED.value
            if raw.get("enabled", False)
            else HermesSupervisionMode.OFF.value
        )
    try:
        normalized_mode = HermesSupervisionMode(str(mode).strip().lower()).value
    except ValueError:
        normalized_mode = HermesSupervisionMode.OFF.value
    return HermesSettings(
        enabled=bool(raw.get("enabled", False)),
        managed=bool(raw.get("managed", False)),
        endpoint=str(raw.get("endpoint", HermesSettings.endpoint)),
        profile=str(raw.get("profile", HermesSettings.profile)),
        timeout_seconds=float(raw.get("timeout_seconds", HermesSettings.timeout_seconds)),
        credential_id=(str(raw["credential_id"]) if raw.get("credential_id") else None),
        runtime_root=(str(raw["runtime_root"]) if raw.get("runtime_root") else None),
        self_host_supervision=normalized_mode,
    )


def write_toml_atomic_private(path: str | Path, data: Mapping[str, Any]) -> None:
    """Atomically write Hermes' operator-owned TOML without following links."""
    try:
        tomli_w = import_module("tomli_w")
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Saving config requires tomli_w (pip install tomli_w)") from exc
    target = Path(path)
    if target.is_symlink():
        raise ValueError(f"refusing to replace symlinked config path: {target}")
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            tomli_w.dump(dict(data), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


__all__ = [
    "HermesSettings",
    "global_config_path",
    "load_settings",
    "load_toml_file",
    "write_toml_atomic_private",
]
