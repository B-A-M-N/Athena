"""Small, deterministic lexical extractors used by the project index."""

from __future__ import annotations

import re

_IMPORT_PATTERNS = (
    re.compile(r"\bfrom\s+([A-Za-z0-9_./-]+)\s+import\b"),
    re.compile(r"\bimport\s+([A-Za-z0-9_./-]+)"),
    re.compile(r"(?:require|include)\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
)
_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE),
    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE),
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)",
        re.MULTILINE,
    ),
    re.compile(r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE),
)


def extract_imports(content: str) -> tuple[str, ...]:
    values: set[str] = set()
    for pattern in _IMPORT_PATTERNS:
        values.update(match.group(1).replace("\\", "/") for match in pattern.finditer(content))
    return tuple(sorted(values))


def extract_symbols(content: str) -> tuple[str, ...]:
    values: set[str] = set()
    for pattern in _SYMBOL_PATTERNS:
        values.update(match.group(1) for match in pattern.finditer(content))
    return tuple(sorted(values))


__all__ = ["extract_imports", "extract_symbols"]
