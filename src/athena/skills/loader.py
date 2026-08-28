"""Skill discovery and parsing (BUILDSPEC 67, SPEC 25).

Discovers portable ``SKILL.md`` conventions from bundled, project, and user
search paths, parses YAML front-matter defensively, and produces validated
:class:`Skill` objects. Skill files are untrusted input: parse errors are
skipped and reported, never fatal (BHV-106).

Precedence for a duplicated name+version (highest wins):
    project > user > bundled
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable, Iterator

from athena.protocol.messages import Provenance, SourceType, TrustClass
from athena.skills.models import Skill
from athena.skills.validator import SkillValidator

try:  # pyyaml is optional; degrade to a minimal front-matter parser.
    import yaml as _yaml  # type: ignore

    def _safe_load_frontmatter(text: str) -> dict | None:
        try:
            loaded = _yaml.safe_load(text)
        except Exception:
            return None
        return loaded if isinstance(loaded, dict) else None

except Exception:  # pragma: no cover - exercised only when pyyaml is absent.
    _yaml = None

    def _safe_load_frontmatter(text: str) -> dict | None:
        return _parse_frontmatter_minimal(text)


_FRONTMATTER_RE = re.compile(r"\A\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)
_SKILL_FILENAMES = frozenset({"SKILL.md", "skill.md"})
_SCOPE_ORDER = {"bundled": 0, "user": 1, "project": 2}
_DEFAULT_SCOPE_TRUST = {
    "bundled": TrustClass.AUTHORITY,
    "project": TrustClass.CONFIGURED_INSTRUCTION,
    "user": TrustClass.USER_CONTENT,
}

logger = logging.getLogger(__name__)


def _parse_frontmatter_minimal(text: str) -> dict | None:
    """Fallback front-matter parser for environments without pyyaml.

    Accepts either the full text (with ``---`` delimiters) or just the
    inner front-matter content between the delimiters.
    """
    match = _FRONTMATTER_RE.match(text)
    if match:
        inner = match.group(1)
    else:
        inner = text
    result: dict = {}
    for line in inner.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip().strip("'\"")
    return result


def _triggers_from(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    if isinstance(raw, (list, tuple)):
        return tuple(str(i).strip() for i in raw if str(i).strip())
    return ()


def _int_version(raw: object, fallback: int = 1) -> int:
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return max(1, raw)
    if isinstance(raw, str) and raw.isdigit():
        return max(1, int(raw))
    return fallback


def _trust_from(meta: dict, scope: str, explicit: TrustClass | None) -> TrustClass:
    if explicit is not None:
        return explicit
    trust_raw = (meta.get("trust") or "").lower()
    if trust_raw:
        try:
            return TrustClass(trust_raw)
        except ValueError:
            pass
    return _DEFAULT_SCOPE_TRUST.get(scope, TrustClass.USER_CONTENT)


def _json_safe(raw: object) -> dict:
    try:
        return json.loads(json.dumps(raw or {}))
    except Exception:
        return {}


class SkillLoader:
    """Discovers, parses, dedupes, and validates skills from search paths.

    ``project_dir`` / ``user_dir`` / ``bundled_dir`` and ``search_paths`` are
    directory trees, each expected to contain ``SKILL.md`` (or ``skill.md``)
    files (recursively). Higher-precedence roots shadow lower for equal
    name+version.
    """

    def __init__(
        self,
        *,
        search_paths: Iterable[str | Path] = (),
        bundled_dir: str | Path | None = None,
        project_dir: str | Path | None = None,
        user_dir: str | Path | None = None,
        validator: SkillValidator | None = None,
    ) -> None:
        self._validator = validator or SkillValidator()
        roots: list[tuple[int, Path, str]] = []
        for scope, cfg in (
            ("project", project_dir),
            ("user", user_dir),
            ("bundled", bundled_dir),
        ):
            if cfg is not None:
                roots.append((_SCOPE_ORDER[scope], Path(cfg), scope))
        for i, p in enumerate(search_paths or ()):
            pp = Path(p)
            scope = _scope_for(pp, project_dir, user_dir, bundled_dir)
            roots.append((i + 100, pp, scope))
        roots.sort(key=lambda t: t[0], reverse=True)
        self._roots: list[tuple[int, Path, str]] = roots
        self._skills: list[Skill] = []
        self.errors: list[str] = []
        self._loaded = False

    async def load(self) -> list[Skill]:
        """Parse all skills, deduped by (name, version)."""
        seen: dict[tuple[str, int], Skill] = {}
        ranked: dict[tuple[str, int], int] = {}
        self.errors = []
        for rank, root, scope in self._roots:
            for parsed in self._parse_root(root, scope):
                key = (parsed.name, parsed.version)
                if key in seen and rank <= ranked[key]:
                    continue
                seen[key] = parsed
                ranked[key] = rank
        for skill in seen.values():
            result = self._validator.validate(skill)
            if not result.ok:
                self._record_error(
                    f"{skill.path or skill.name}: invalid ({'; '.join(result.errors)})"
                )
                continue
            self._skills.append(skill)
        self._loaded = True
        return list(self._skills)

    async def load_active(self) -> list[Skill]:
        """Enabled, validated skills for context injection (progressive disclosure)."""
        return [s for s in await self.load() if s.enabled]

    def _parse_root(self, root: Path, scope: str) -> Iterator[Skill]:
        for skill_root in _iter_skill_roots(root):
            skill = self.parse_skill_file(skill_root / _skill_filename(skill_root), scope=scope)
            if skill is not None:
                yield skill

    def parse_skill_file(
        self,
        path: Path,
        *,
        scope: str = "user",
        trust: TrustClass | None = None,
    ) -> Skill | None:
        """Parse + validate a single ``SKILL.md`` file, or None on failure."""
        text = _read_text(path)
        if text is None:
            self._record_error(f"{path}: unreadable")
            return None
        match = _FRONTMATTER_RE.match(text)
        body_start = match.end() if match else 0
        raw = _safe_load_frontmatter(match.group(1)) if match else None
        if raw is None:
            raw = {}
        body = text[body_start:].strip()
        if not body:
            self._record_error(f"{path}: empty body")
            return None

        name = str(raw.get("name") or path.parent.name or "").strip()
        description = str(raw.get("description") or "").strip()
        triggers = _triggers_from(raw.get("triggers") or raw.get("keywords"))
        scope_meta = str(raw.get("scope") or scope or "user").strip()
        version = _int_version(raw.get("version"), 1)
        trust_class = _trust_from(raw, scope_meta, trust)

        meta = _json_safe(raw.get("metadata"))
        ath = _json_safe(meta.get("athena"))
        if ath.get("version") is not None:
            version = _int_version(ath["version"], version)
        if ath.get("trust"):
            try:
                trust_class = TrustClass(str(ath["trust"]).lower())
            except ValueError:
                pass
        if ath.get("scope"):
            scope_meta = str(ath["scope"])

        provenance = Provenance(
            source_type=SourceType.SKILL,
            source_id=str(path.resolve()),
            trust=trust_class,
            scope=scope_meta,
        )
        skill = Skill(
            id=str(path.resolve()),
            name=name or "",
            description=description or "",
            body=body,
            triggers=triggers,
            scope=scope_meta,
            trust=trust_class,
            version=version,
            path=str(path.resolve()),
            source=provenance,
            enabled=True,
            metadata={"athena": ath} if ath else {},
        )
        result = self._validator.validate(skill)
        if not result.ok:
            self._record_error(f"{path}: invalid skill ({'; '.join(result.errors)})")
            return None
        return skill

    def _record_error(self, message: str) -> None:
        logger.warning("skill loader: %s", message)
        self.errors.append(message)


def _scope_for(pth, project_dir, user_dir, bundled_dir) -> str:
    def under(cfg) -> bool:
        try:
            return cfg is not None and Path(pth).resolve().is_relative_to(Path(cfg).resolve())
        except Exception:
            return False

    if under(project_dir):
        return "project"
    if under(user_dir):
        return "user"
    if under(bundled_dir):
        return "bundled"
    return "local"


def _iter_skill_roots(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for candidate in root.rglob("*"):
        if candidate.is_file() and candidate.name in _SKILL_FILENAMES:
            yield candidate.parent


def _skill_filename(skill_root: Path) -> str:
    for name in ("SKILL.md", "skill.md"):
        if (skill_root / name).is_file():
            return name
    return "SKILL.md"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


__all__ = ["SkillLoader"]
