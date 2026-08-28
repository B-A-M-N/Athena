"""Normalize common tool diagnostics into stable, machine-readable records."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

_LOCATION = re.compile(
    r"^(?P<file>[^:\n()]+?)(?:\((?P<paren_line>\d+),(?P<paren_col>\d+)\)"
    r"|:(?P<line>\d+)(?::(?P<column>\d+))?):\s*"
    r"(?:(?P<severity>error|warning|note|info)\b\s*:?\s*)?"
    r"(?:(?P<code>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*)?"
    r"(?P<message>.+?)\s*$",
    re.IGNORECASE,
)
_CARGO_HEADER = re.compile(
    r"^\s*(?P<severity>error|warning|note)"
    r"(?:\[(?P<code>[^\]]+)\])?\s*:\s*(?P<message>.+?)\s*$",
    re.IGNORECASE,
)
_CARGO_LOCATION = re.compile(
    r"^\s*-->\s*(?P<file>[^:\n]+):(?P<line>\d+)(?::(?P<column>\d+))?"
)
_TRACEBACK_LOCATION = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)'
)
_FLAKE8 = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?P<code>[A-Z]\d{3})\s+(?P<message>.+?)\s*$"
)
_GENERIC = re.compile(
    r"^\s*(?P<severity>error|warning|note|info)\s*:?\s*"
    r"(?:(?P<code>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*)?"
    r"(?P<message>.+?)\s*$",
    re.IGNORECASE,
)
_FLAKE8_CODE = re.compile(r"^(?P<code>[A-Z]\d{3})\s+(?P<message>.+)$")


@dataclass(frozen=True)
class Diagnostic:
    tool: str
    severity: str
    message: str
    code: str | None = None
    file: str | None = None
    line: int | None = None
    column: int | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning", "note", "info"}:
            raise ValueError(f"invalid diagnostic severity: {self.severity}")
        if not self.message.strip():
            raise ValueError("diagnostic message must not be empty")
        if not self.fingerprint:
            value = "\x1f".join((
                self.tool.casefold(),
                self.severity,
                self.code or "",
                self.file or "",
                str(self.line or ""),
                str(self.column or ""),
                " ".join(self.message.split()).casefold(),
            ))
            object.__setattr__(self, "fingerprint", hashlib.sha256(
                value.encode("utf-8")
            ).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "severity": self.severity,
            "code": self.code,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "fingerprint": self.fingerprint,
        }


def normalize_diagnostics(
    text: str,
    *,
    tool: str = "unknown",
    cwd: str | None = None,
) -> tuple[Diagnostic, ...]:
    """Parse common compiler, linter, test, and traceback locations.

    The parser is deliberately conservative. It emits a record only when a
    line has a recognizable severity or a known tool-location shape, and
    deduplicates identical records while preserving first-seen order.
    """
    del cwd  # Reserved for future path canonicalization; records keep tool paths.
    tool_name = str(tool or "unknown")
    found: list[Diagnostic] = []
    pending: dict[str, Any] | None = None

    def emit(fields: dict[str, Any]) -> None:
        try:
            found.append(Diagnostic(tool=tool_name, **fields))
        except ValueError:
            return

    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        cargo = _CARGO_HEADER.match(line)
        if cargo:
            if pending is not None:
                emit(pending)
            pending = {
                "severity": cargo.group("severity").casefold(),
                "code": cargo.group("code"),
                "message": cargo.group("message").strip(),
            }
            continue

        location = _CARGO_LOCATION.match(line)
        if location and pending is not None:
            emit({
                **pending,
                "file": location.group("file"),
                "line": int(location.group("line")),
                "column": (
                    int(location.group("column"))
                    if location.group("column") else None
                ),
            })
            pending = None
            continue

        traceback = _TRACEBACK_LOCATION.match(line)
        if traceback:
            if pending is not None:
                emit(pending)
            pending = {
                "severity": "error",
                "file": traceback.group("file"),
                "line": int(traceback.group("line")),
                "message": "",
            }
            continue

        if pending is not None and not pending["message"]:
            if line.strip():
                emit({
                    **pending,
                    "message": line.strip(),
                })
                pending = None
            continue

        flake8 = _FLAKE8.match(line)
        if flake8:
            emit({
                "severity": "error" if flake8.group("code")[0] in {"E", "F"}
                else "warning",
                "code": flake8.group("code"),
                "file": flake8.group("file"),
                "line": int(flake8.group("line")),
                "column": int(flake8.group("column")),
                "message": flake8.group("message").strip(),
            })
            continue

        match = _LOCATION.match(line)
        if match:
            severity = (match.group("severity") or "").casefold()
            code = match.group("code")
            message = match.group("message").strip()
            # flake8 embeds severity in its code rather than spelling it out.
            if not severity and code:
                severity = "error" if code[0] in {"E", "F"} else "warning"
            if severity:
                emit({
                    "severity": severity,
                    "code": code,
                    "file": match.group("file"),
                    "line": int(match.group("paren_line") or match.group("line")),
                    "column": (
                        int(match.group("paren_col") or match.group("column"))
                        if (match.group("paren_col") or match.group("column"))
                        else None
                    ),
                    "message": message,
                })
                continue

        generic = _GENERIC.match(line)
        if generic:
            emit({
                "severity": generic.group("severity").casefold(),
                "code": generic.group("code"),
                "message": generic.group("message").strip(),
            })
            continue

        if ":" in line:
            prefix, _, message = line.partition(":")
            flake8 = _FLAKE8_CODE.match(message.strip())
            if flake8 and prefix and not prefix.startswith(("http", "https")):
                emit({
                    "severity": "error" if flake8.group("code")[0] in {"E", "F"}
                    else "warning",
                    "code": flake8.group("code"),
                    "file": prefix,
                    "message": flake8.group("message").strip(),
                })

    if pending is not None:
        emit(pending)

    unique: dict[str, Diagnostic] = {}
    for diagnostic in found:
        unique.setdefault(diagnostic.fingerprint, diagnostic)
    return tuple(unique.values())


__all__ = ["Diagnostic", "normalize_diagnostics"]
