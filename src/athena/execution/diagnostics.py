"""Normalize common tool diagnostics into stable, machine-readable records."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

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
_CARGO_LOCATION = re.compile(r"^\s*-->\s*(?P<file>[^:\n]+):(?P<line>\d+)(?::(?P<column>\d+))?")
_TRACEBACK_LOCATION = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)')
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
_PATH_IN_MESSAGE = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s:'\"]+\.[A-Za-z0-9]+")
_QUOTED_VALUE = re.compile(r"(['\"]).*?\1")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


@dataclass(frozen=True)
class Diagnostic:
    tool: str
    severity: str
    message: str
    code: str | None = None
    file: str | None = None
    line: int | None = None
    column: int | None = None
    symbol: str | None = None
    related: tuple[Mapping[str, Any], ...] = ()
    source_tool_version: str | None = None
    occurrence_fingerprint: str = ""
    signature_fingerprint: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning", "note", "info"}:
            raise ValueError(f"invalid diagnostic severity: {self.severity}")
        if not self.message.strip():
            raise ValueError("diagnostic message must not be empty")
        occurrence = self.occurrence_fingerprint or self.fingerprint
        if not occurrence:
            value = "\x1f".join(
                (
                    self.tool.casefold(),
                    self.severity,
                    self.code or "",
                    self.file or "",
                    str(self.line or ""),
                    str(self.column or ""),
                    _normalize_exact(self.message),
                )
            )
            occurrence = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if not self.occurrence_fingerprint:
            object.__setattr__(self, "occurrence_fingerprint", occurrence)
        if not self.signature_fingerprint:
            signature_value = "\x1f".join(
                (
                    self.tool.casefold(),
                    self.source_tool_version or "",
                    self.severity,
                    self.code or "",
                    _normalize_semantic(self.message),
                    self.symbol or "",
                )
            )
            object.__setattr__(
                self,
                "signature_fingerprint",
                hashlib.sha256(signature_value.encode("utf-8")).hexdigest(),
            )
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", occurrence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "severity": self.severity,
            "code": self.code,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "symbol": self.symbol,
            "related": [dict(item) for item in self.related],
            "source_tool_version": self.source_tool_version,
            "message": self.message,
            "occurrence_fingerprint": self.occurrence_fingerprint,
            "signature_fingerprint": self.signature_fingerprint,
            # Kept as a compatibility alias for existing consumers.
            "fingerprint": self.fingerprint,
        }


def normalize_diagnostics(
    text: str,
    *,
    tool: str = "unknown",
    cwd: str | None = None,
    source_tool_version: str | None = None,
) -> tuple[Diagnostic, ...]:
    """Parse common compiler, linter, test, and traceback locations.

    The parser is deliberately conservative. It emits a record only when a
    line has a recognizable severity or a known tool-location shape, and
    deduplicates identical records while preserving first-seen order.
    """
    del cwd  # Reserved for future path canonicalization; records keep tool paths.
    tool_name = str(tool or "unknown")
    raw_text = str(text or "")
    try:
        decoded = json.loads(raw_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, (list, dict)):
        return normalize_diagnostics_payload(
            decoded,
            tool=tool_name,
            source_tool_version=source_tool_version,
        )
    found: list[Diagnostic] = []
    pending: dict[str, Any] | None = None

    def emit(fields: dict[str, Any]) -> None:
        try:
            found.append(
                Diagnostic(tool=tool_name, source_tool_version=source_tool_version, **fields)
            )
        except ValueError:
            return

    for raw_line in raw_text.splitlines():
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
            emit(
                {
                    **pending,
                    "file": location.group("file"),
                    "line": int(location.group("line")),
                    "column": (int(location.group("column")) if location.group("column") else None),
                }
            )
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
                emit(
                    {
                        **pending,
                        "message": line.strip(),
                    }
                )
                pending = None
            continue

        flake8 = _FLAKE8.match(line)
        if flake8:
            emit(
                {
                    "severity": "error" if flake8.group("code")[0] in {"E", "F"} else "warning",
                    "code": flake8.group("code"),
                    "file": flake8.group("file"),
                    "line": int(flake8.group("line")),
                    "column": int(flake8.group("column")),
                    "message": flake8.group("message").strip(),
                }
            )
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
                emit(
                    {
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
                    }
                )
                continue

        generic = _GENERIC.match(line)
        if generic:
            emit(
                {
                    "severity": generic.group("severity").casefold(),
                    "code": generic.group("code"),
                    "message": generic.group("message").strip(),
                }
            )
            continue

        if ":" in line:
            prefix, _, message = line.partition(":")
            flake8 = _FLAKE8_CODE.match(message.strip())
            if flake8 and prefix and not prefix.startswith(("http", "https")):
                emit(
                    {
                        "severity": "error" if flake8.group("code")[0] in {"E", "F"} else "warning",
                        "code": flake8.group("code"),
                        "file": prefix,
                        "message": flake8.group("message").strip(),
                    }
                )

    if pending is not None:
        emit(pending)

    unique: dict[str, Diagnostic] = {}
    for diagnostic in found:
        unique.setdefault(diagnostic.occurrence_fingerprint, diagnostic)
    return tuple(unique.values())


def normalize_diagnostics_payload(
    payload: Any,
    *,
    tool: str = "unknown",
    source_tool_version: str | None = None,
) -> tuple[Diagnostic, ...]:
    """Normalize a native JSON diagnostics payload before text heuristics.

    Tools use several common envelopes (``diagnostics``, ``errors``, and
    ``issues``). Unknown fields are retained only where they are useful for
    deterministic repair: location, symbol, and related locations.
    """
    if isinstance(payload, Mapping):
        for key in ("diagnostics", "errors", "issues", "messages"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return ()
    result: list[Diagnostic] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        message = item.get("message") or item.get("detail") or item.get("text")
        if not str(message or "").strip():
            continue
        raw_severity = str(item.get("severity") or item.get("level") or "error").lower()
        severity = raw_severity if raw_severity in {"error", "warning", "note", "info"} else "error"
        line = _as_int(item.get("line") or item.get("startLine"))
        column = _as_int(item.get("column") or item.get("startColumn"))
        location = item.get("location")
        if isinstance(location, Mapping):
            file = location.get("file") or location.get("path")
            line = line or _as_int(location.get("line"))
            column = column or _as_int(location.get("column"))
        else:
            file = item.get("file") or item.get("path") or item.get("filename")
        related = item.get("related") or item.get("relatedInformation") or ()
        if not isinstance(related, list):
            related = ()
        try:
            result.append(
                Diagnostic(
                    tool=str(item.get("tool") or tool or "unknown"),
                    severity=severity,
                    message=str(message).strip(),
                    code=str(item.get("code")) if item.get("code") is not None else None,
                    file=str(file) if file is not None else None,
                    line=line,
                    column=column,
                    symbol=(str(item.get("symbol")) if item.get("symbol") is not None else None),
                    related=tuple(dict(value) for value in related if isinstance(value, Mapping)),
                    source_tool_version=(
                        str(item.get("source_tool_version") or source_tool_version)
                        if (item.get("source_tool_version") or source_tool_version)
                        else None
                    ),
                )
            )
        except ValueError:
            continue
    unique: dict[str, Diagnostic] = {}
    for diagnostic in result:
        unique.setdefault(diagnostic.occurrence_fingerprint, diagnostic)
    return tuple(unique.values())


def _normalize_exact(message: str) -> str:
    return " ".join(message.split()).casefold()


def _normalize_semantic(message: str) -> str:
    value = _normalize_exact(message)
    value = _PATH_IN_MESSAGE.sub("<path>", value)
    value = _QUOTED_VALUE.sub("<value>", value)
    return _NUMBER.sub("<n>", value)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["Diagnostic", "normalize_diagnostics", "normalize_diagnostics_payload"]
