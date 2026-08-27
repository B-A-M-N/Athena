"""Conservative Kitty Graphics Protocol support.

The protocol is optional decoration.  ANSI remains the authoritative fallback
and no model/runtime payload is ever sent through the image transport.
"""

from __future__ import annotations

import base64
import os
import re
import select
import sys
import time
from dataclasses import dataclass
from typing import TextIO

try:  # Capability probing is best-effort and must not break Windows/plain CLI.
    import termios
    import tty
except ImportError:  # pragma: no cover - exercised on Windows
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]


@dataclass(frozen=True)
class KittyAsset:
    asset_id: int
    data: bytes
    format: str = "png"


class KittyGraphicsProtocol:
    """Bounded, Athena-owned Kitty image encoder and placer.

    Kitty's ``x``/``y`` placement keys are source-image pixel offsets, not
    terminal-cell coordinates.  The caller moves the cursor to the desired
    cell and this class uses ``c``/``r`` for the destination rectangle.
    """

    MAX_BYTES = 2_000_000
    MAX_CHUNK = 4096
    QUERY_ID = 31

    def __init__(self) -> None:
        self._asset_ids: set[int] = set()

    @staticmethod
    def _format(asset: KittyAsset) -> int:
        if asset.format == "png":
            return 100
        return 24

    def _validate(self, asset: KittyAsset) -> None:
        if asset.asset_id <= 0 or len(asset.data) > self.MAX_BYTES:
            raise ValueError("Kitty asset is outside the safe resource limits")
        if asset.format not in {"png", "jpeg", "jpg"}:
            raise ValueError("unsupported Kitty asset format")

    @staticmethod
    def _escape(control: str, payload: str = "") -> str:
        return f"\x1b_G{control};{payload}\x1b\\"

    def encode(self, asset: KittyAsset, *, x: int = 0, y: int = 0) -> str:
        """Transmit an image using direct, chunked Kitty graphics data.

        ``x`` and ``y`` are retained as ignored compatibility parameters from
        the prototype API.  Placement coordinates belong to ``present`` and
        are expressed with a cursor move, never as source-image offsets.
        """
        del x, y
        self._validate(asset)
        self._asset_ids.add(asset.asset_id)
        encoded = base64.b64encode(asset.data).decode("ascii")
        chunks = [
            encoded[index:index + self.MAX_CHUNK]
            for index in range(0, len(encoded), self.MAX_CHUNK)
        ] or [""]
        commands: list[str] = []
        for index, chunk in enumerate(chunks):
            more = 1 if index < len(chunks) - 1 else 0
            if index == 0:
                control = (
                    f"a=t,f={self._format(asset)},i={asset.asset_id},"
                    f"q=2,m={more}"
                )
            else:
                control = f"q=2,m={more}"
            commands.append(self._escape(control, chunk))
        return "".join(commands)

    def present(self, asset: KittyAsset, *, x: int, y: int, columns: int, rows: int) -> str:
        """Transmit and place one owned image at a fixed CRT viewport."""
        self._validate(asset)
        return (
            self.encode(asset)
            + f"\x1b[{max(y, 0) + 1};{max(x, 0) + 1}H"
            + self._escape(
                f"a=p,i={asset.asset_id},c={max(columns, 1)},"
                f"r={max(rows, 1)},C=1,z=-1,q=2"
            )
        )

    def delete(self, asset_id: int) -> str:
        if asset_id not in self._asset_ids:
            return ""
        self._asset_ids.remove(asset_id)
        return self._escape(f"a=d,d=i,i={asset_id},q=2")

    def cleanup(self) -> str:
        payload = "".join(self.delete(asset_id) for asset_id in sorted(self._asset_ids))
        return payload

    @classmethod
    def query_support(cls) -> str:
        """Return Kitty's non-destructive graphics support query.

        The following primary-device-attributes query lets a caller
        distinguish a graphics response from a terminal that merely echoes or
        ignores APC sequences.
        """
        return (
            f"\x1b_Ga=q,i={cls.QUERY_ID},s=1,v=1,t=d,f=24,q=0;AAAA\x1b\\"
            "\x1b[c"
        )


class KittyCapabilityProbe:
    """Probe and parse an actual Kitty graphics response."""

    _RESPONSE = re.compile(
        r"\x1b_G(?P<control>[^;]*)?;(?P<body>[^\x1b]*)\x1b\\"
    )

    @classmethod
    def confirmed(cls, response: str | bytes | None) -> bool:
        if not response:
            return False
        text = response.decode("utf-8", "ignore") if isinstance(response, bytes) else response
        for match in cls._RESPONSE.finditer(text):
            control = match.group("control") or ""
            body = match.group("body").strip()
            controls = {
                key: value
                for key, _, value in (part.partition("=") for part in control.split(","))
                if key and value
            }
            if controls.get("i") == str(KittyGraphicsProtocol.QUERY_ID) and body == "OK":
                return True
        return False

    @classmethod
    def probe(
        cls,
        output: TextIO,
        input_stream: TextIO | None = None,
        *,
        timeout: float = 0.12,
    ) -> bool:
        """Safely probe a TTY and restore its line discipline afterward.

        Probing is deliberately disabled for non-TTY streams.  The short
        cbreak window happens before prompt input begins; unsupported
        terminals receive no graphics payload beyond the documented query,
        and all exceptions fall back to ANSI.
        """
        input_stream = input_stream or sys.stdin
        if not (
            getattr(output, "isatty", lambda: False)()
            and getattr(input_stream, "isatty", lambda: False)()
        ):
            return False
        try:
            fd = input_stream.fileno()
        except (AttributeError, OSError, ValueError):
            return False
        if termios is None or tty is None:
            return False
        try:
            original = termios.tcgetattr(fd)
        except (OSError, termios.error):
            return False
        response = bytearray()
        deadline = time.monotonic() + max(float(timeout), 0.01)
        try:
            tty.setcbreak(fd)
            output.write(KittyGraphicsProtocol.query_support())
            output.flush()
            while time.monotonic() < deadline:
                remaining = max(deadline - time.monotonic(), 0.0)
                readable, _, _ = select.select([fd], [], [], remaining)
                if not readable:
                    break
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                response.extend(chunk)
                if cls.confirmed(bytes(response)):
                    return True
        except (OSError, ValueError, termios.error):
            return False
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, original)
            except (OSError, termios.error):
                pass
        return False


def select_renderer(requested: str, *, capability_confirmed: bool = False) -> str:
    """Resolve auto/glass/ansi/plain without optimistic protocol claims."""
    normalized = str(requested or "auto").lower()
    if normalized == "plain":
        return "plain"
    if normalized == "ansi":
        return "ansi"
    if normalized == "glass":
        return "glass" if capability_confirmed else "ansi"
    return "glass" if capability_confirmed else "ansi"


__all__ = ["KittyAsset", "KittyCapabilityProbe", "KittyGraphicsProtocol", "select_renderer"]
