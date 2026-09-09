"""Canonical path-scope algebra for delegated and persisted authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from athena.protocol.tasks import PathRule


def canonicalize_path_rules(
    rules: Iterable[PathRule | Mapping[str, Any]] | None,
    *,
    base: str | Path | None = None,
) -> tuple[PathRule, ...]:
    """Normalize path rules to real absolute prefixes.

    ``/**`` is the serialized recursive-prefix spelling used by policy
    profiles.  It is canonicalized to the corresponding directory prefix;
    every rule remains a prefix rule and explicit denies always win.
    """
    base_path = Path(base).resolve(strict=False) if base else None
    result: list[PathRule] = []
    for raw in rules or ():
        if isinstance(raw, PathRule):
            value, allow = raw.path, raw.allow
        elif isinstance(raw, Mapping):
            value, allow = raw.get("path", ""), raw.get("allow", True)
        else:
            continue
        path = _canonical_path(str(value or ""), base_path)
        if path is not None:
            result.append(PathRule(path=str(path), allow=bool(allow)))
    return tuple(result)


def path_allowed(
    path: str | Path,
    rules: Iterable[PathRule | Mapping[str, Any]] | None,
    *,
    base: str | Path | None = None,
) -> bool:
    """Return the effective decision for one path under a prefix policy."""
    normalized = _canonical_path(str(path), Path(base).resolve(strict=False) if base else None)
    if normalized is None:
        return False
    canonical = canonicalize_path_rules(rules, base=base)
    allows = [Path(rule.path) for rule in canonical if rule.allow]
    denies = [Path(rule.path) for rule in canonical if not rule.allow]
    if not allows:
        allows = [Path(base).resolve(strict=False)] if base else [normalized]
    return any(_within(normalized, root) for root in allows) and not any(
        _within(normalized, root) for root in denies
    )


def path_rules_cover(
    upper: Iterable[PathRule | Mapping[str, Any]] | None,
    lower: Iterable[PathRule | Mapping[str, Any]] | None,
    *,
    upper_base: str | Path | None = None,
    lower_base: str | Path | None = None,
) -> bool:
    """Return whether every path allowed by ``lower`` is allowed by ``upper``.

    The check is prefix-aware and accounts for nested denies on both sides.
    This is intentionally an authority subset check, not a check that merely
    compares the listed rule strings.
    """
    upper_base_path = Path(upper_base).resolve(strict=False) if upper_base else None
    lower_base_path = Path(lower_base).resolve(strict=False) if lower_base else None
    upper_rules = list(canonicalize_path_rules(upper, base=upper_base_path))
    lower_rules = list(canonicalize_path_rules(lower, base=lower_base_path))
    upper_allows, upper_denies = _effective_parts(upper_rules, upper_base_path)
    lower_allows, lower_denies = _effective_parts(lower_rules, lower_base_path)

    # A restricted upper scope cannot cover an unrestricted lower scope.
    if upper_rules and not upper_allows:
        return False
    for lower_allow in lower_allows:
        if upper_rules and not any(
            _within(lower_allow, upper_allow) for upper_allow in upper_allows
        ):
            return False
        for upper_deny in upper_denies:
            overlap = _prefix_intersection(lower_allow, upper_deny)
            if overlap is not None and not any(
                _within(overlap, lower_deny) for lower_deny in lower_denies
            ):
                return False
    return True


def intersect_path_rules(
    left: Iterable[PathRule | Mapping[str, Any]] | None,
    right: Iterable[PathRule | Mapping[str, Any]] | None,
    *,
    left_base: str | Path | None = None,
    right_base: str | Path | None = None,
    result_base: str | Path | None = None,
    scope: str | Path | None = None,
) -> tuple[PathRule, ...]:
    """Return the explicit prefix-policy intersection of two rule sets.

    ``scope`` bounds the result to a workspace root.  It is separate from
    ``result_base`` because a base resolves relative rules, while a scope is
    an authority boundary that must also trim absolute rules outside it.
    """
    left_base_path = Path(left_base).resolve(strict=False) if left_base else None
    right_base_path = Path(right_base).resolve(strict=False) if right_base else None
    result_base_path = Path(result_base).resolve(strict=False) if result_base else None
    scope_path = Path(scope).resolve(strict=False) if scope else None
    left_rules = list(canonicalize_path_rules(left, base=left_base_path))
    right_rules = list(canonicalize_path_rules(right, base=right_base_path))
    left_allows, left_denies = _effective_parts(left_rules, left_base_path)
    right_allows, right_denies = _effective_parts(right_rules, right_base_path)
    candidates = [
        scoped
        for first in left_allows
        for second in right_allows
        if (overlap := _prefix_intersection(first, second)) is not None
        and (
            scoped := (
                _prefix_intersection(overlap, scope_path) if scope_path is not None else overlap
            )
        )
        is not None
    ]
    denies = [
        scoped
        for deny in (*left_denies, *right_denies)
        if (scoped := (_prefix_intersection(deny, scope_path) if scope_path is not None else deny))
        is not None
    ]
    surviving = [
        path for path in _unique(candidates) if not any(_within(path, deny) for deny in denies)
    ]
    output = [PathRule(path=str(path), allow=True) for path in surviving]
    output.extend(
        PathRule(path=str(deny), allow=False)
        for deny in _unique(denies)
        if any(_within(deny, allowed) for allowed in surviving)
    )
    if not output:
        fallback = result_base_path or right_base_path or left_base_path
        if fallback is not None:
            output.append(PathRule(path=str(fallback), allow=False))
    return tuple(output)


def _effective_parts(rules: list[PathRule], base: Path | None) -> tuple[list[Path], list[Path]]:
    allows = [Path(rule.path) for rule in rules if rule.allow]
    denies = [Path(rule.path) for rule in rules if not rule.allow]
    if not allows:
        # No positive rule means the base is unrestricted, even when explicit
        # denies are present.  With serialized absolute rules and no workspace
        # root, the protocol's safe universal reference is the filesystem root.
        allows = [base or Path("/")]
    return allows, denies


def _canonical_path(value: str, base: Path | None) -> Path | None:
    if not value:
        return None
    raw = value[:-3] if value.endswith("/**") else value
    path = Path(raw)
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve(strict=False)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _prefix_intersection(left: Path, right: Path) -> Path | None:
    if _within(left, right):
        return left
    if _within(right, left):
        return right
    return None


def _unique(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        value = str(path)
        if value not in seen:
            seen.add(value)
            result.append(path)
    return result


__all__ = [
    "canonicalize_path_rules",
    "intersect_path_rules",
    "path_allowed",
    "path_rules_cover",
]
