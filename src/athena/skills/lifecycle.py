"""Skill lifecycle (BUILDSPEC 66, SPEC 25/26).

Manages the persistent lifecycle of skills across ``candidate -> draft ->
validated -> active -> deprecated -> archived`` using the ``skills`` and
``skill_versions`` tables. Portable skill content and local lifecycle state are
kept separate (section 66): content lives in the row, lifecycle state in
per-row metadata JSON.

Also exposes :class:`SkillStore`, the single handle the ``skills`` capability
wrapper and the context compiler consume via ``search`` / ``trigger`` /
``load_active``. Promotion is explicit, observable (a canonical event via the
supplied event store, or a log), and reversible via version history.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Mapping

from athena.protocol.events import EventCategory, make_event
from athena.protocol.ids import new_id
from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow
from athena.skills.models import Skill, SkillCandidate
from athena.skills.validator import SkillValidator
from athena.state.database import Database

logger = logging.getLogger(__name__)

_STATE_ENABLED = "enabled"
_STATE_DISABLED = "disabled"
_STATE_ARCHIVED = "archived"
_LIFEKEY = "athena.lifecycle"


def _meta(skill: Skill) -> dict:
    meta = dict(skill.metadata)
    meta[_LIFEKEY] = {
        "state": _STATE_ENABLED,
        "triggers": list(skill.triggers),
        "scope": skill.scope or "user",
    }
    meta["trust"] = skill.trust.value
    athena = dict(meta.get("athena") or {})
    athena["trust"] = skill.trust.value
    athena["scope"] = skill.scope or "user"
    meta["athena"] = athena
    return meta


def _with_fixed(draft: Skill, **kwargs: Any) -> Skill:
    from dataclasses import replace

    return replace(draft, **kwargs)


def _with_trust(draft: Skill, trust: TrustClass) -> Skill:
    return _with_fixed(draft, trust=trust)


def _is_enabled(skill: Skill) -> bool:
    state = skill.metadata.get(_LIFEKEY, {}).get("state", _STATE_ENABLED)
    return state == _STATE_ENABLED


def _row_skill(row: Mapping[str, Any]) -> Skill:
    meta = row.get("metadata")
    if isinstance(meta, str) and meta:
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    life = meta.get(_LIFEKEY) or {}
    trust_raw = meta.get("trust") or ""
    try:
        trust = TrustClass(trust_raw) if trust_raw else TrustClass.AGENT_CURATED
    except ValueError:
        trust = TrustClass.AGENT_CURATED
    source_id = str(row.get("id") or "")
    provenance = Provenance(
        source_type=SourceType.SKILL,
        source_id=source_id,
        trust=trust,
        scope=life.get("scope") or meta.get("scope") or "user",
    )
    skill_id = str(row["id"])
    return Skill(
        id=skill_id,
        name=str(row.get("name") or ""),
        description=str(row.get("description") or ""),
        body=str(row.get("content") or ""),
        triggers=tuple(str(t) for t in (life.get("triggers") or ())),
        version=int(row.get("version") or 1),
        scope=life.get("scope") or meta.get("scope") or "user",
        trust=trust,
        source=provenance,
        metadata=meta,
        enabled=life.get("state", _STATE_ENABLED) == _STATE_ENABLED,
        path=None,
    )


class SkillLifecycle:
    """Persistent lifecycle operations backed by the ``skills`` tables.

    ``events`` is an optional store with an ``append(Event)`` method (see
    :class:`athena.state.events.EventStore`); when absent, transitions are
    logged. Promotion is never silent.
    """

    def __init__(
        self,
        db: Database,
        *,
        events: Any = None,
        validator: SkillValidator | None = None,
    ) -> None:
        self._db = db
        self._events = events
        self._validator = validator or SkillValidator()

    async def install(
        self,
        skill: Skill,
        *,
        task_id: str | None = None,
    ) -> str:
        result = self._validator.validate(skill)
        if not result.ok:
            raise ValueError(
                f"cannot install invalid skill: {skill.name or '?'} ({'; '.join(result.errors)})"
            )
        skill_id = skill.id or new_id("skill")
        now = utcnow().isoformat()
        meta = _meta(skill)
        await self._db.execute(
            "INSERT INTO skills("
            "id, name, description, content, text_content, version, "
            "created_at, updated_at, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                skill_id,
                skill.name,
                skill.description or "",
                skill.body or "",
                (skill.description or "") + "\n" + (skill.body or ""),
                int(skill.version or 1),
                now,
                now,
                json.dumps(meta),
            ),
        )
        await self._db.execute(
            "INSERT OR REPLACE INTO skill_versions("
            "skill_id, version, content, created_at"
            ") VALUES (?, ?, ?, ?)",
            (skill_id, int(skill.version or 1), skill.body or "", now),
        )
        await self._emit(
            EventCategory.SKILL_ACTIVATED.value,
            {"skill_id": skill_id, "op": "install", "version": int(skill.version or 1)},
            task_id=task_id,
        )
        return skill_id

    async def update(self, skill_id: str, skill: Skill) -> int:
        current = await self.get(skill_id)
        if current is None:
            raise KeyError(f"no such skill: {skill_id}")
        base = _with_fixed(skill, id=current.id, version=current.version)
        result = self._validator.validate(base)
        if not result.ok:
            raise ValueError(
                f"cannot update invalid skill: {skill.name or '?'} ({'; '.join(result.errors)})"
            )
        new_version = int(current.version or 1) + 1
        updated = _with_fixed(skill, version=new_version, id=current.id)
        now = utcnow().isoformat()
        await self._db.execute(
            "UPDATE skills SET name = ?, description = ?, content = ?, "
            "text_content = ?, version = ?, updated_at = ?, metadata = ? "
            "WHERE id = ?",
            (
                updated.name,
                updated.description or "",
                updated.body or "",
                (updated.description or "") + "\n" + (updated.body or ""),
                new_version,
                now,
                json.dumps(_meta(updated)),
                skill_id,
            ),
        )
        await self._db.execute(
            "INSERT OR REPLACE INTO skill_versions("
            "skill_id, version, content, created_at"
            ") VALUES (?, ?, ?, ?)",
            (skill_id, new_version, updated.body or "", now),
        )
        await self._emit(
            EventCategory.SKILL_ACTIVATED.value,
            {"skill_id": skill_id, "op": "update", "version": new_version},
        )
        return new_version

    async def get(self, skill_id: str) -> Skill | None:
        row = await self._db.fetch_one("SELECT * FROM skills WHERE id = ?", (skill_id,))
        return _row_skill(row) if row else None

    async def list(self, *, active_only: bool = False) -> list[Skill]:
        rows = await self._db.fetch_all(
            "SELECT * FROM skills ORDER BY name COLLATE NOCASE, version"
        )
        skills = [_row_skill(r) for r in rows]
        if active_only:
            skills = [s for s in skills if _is_enabled(s)]
        return skills

    async def enable(self, skill_id: str) -> bool:
        current = await self.get(skill_id)
        if current is None:
            return False
        await self._update_life_state(skill_id, current, _STATE_ENABLED)
        return True

    async def disable(self, skill_id: str) -> bool:
        current = await self.get(skill_id)
        if current is None:
            return False
        await self._update_life_state(skill_id, current, _STATE_DISABLED)
        return True

    async def archive(self, skill_id: str) -> bool:
        current = await self.get(skill_id)
        if current is None:
            return False
        await self._update_life_state(skill_id, current, _STATE_ARCHIVED)
        await self._emit(
            EventCategory.SKILL_ACTIVATED.value,
            {"skill_id": skill_id, "op": "archive", "version": current.version},
        )
        return True

    async def history(self, skill_id: str) -> List[dict]:
        return await self._db.fetch_all(
            "SELECT version, content, created_at FROM skill_versions "
            "WHERE skill_id = ? ORDER BY version ASC",
            (skill_id,),
        )

    async def _update_life_state(self, skill_id: str, skill: Skill, state: str) -> None:
        meta = dict(skill.metadata)
        life = dict(meta.get(_LIFEKEY) or {})
        life["state"] = state
        meta[_LIFEKEY] = life
        await self._db.execute(
            "UPDATE skills SET metadata = ? WHERE id = ?",
            (json.dumps(meta), skill_id),
        )

    async def promote(
        self,
        candidate: SkillCandidate,
        *,
        task_id: str | None = None,
        authorized: bool = True,
    ) -> str | None:
        """Promote a validated candidate to an active skill (self-improvement).

        ``authorized`` gates promotion by policy; when False, promotion is
        refused and only a rejected candidate event is emitted (never silent).
        """
        if not authorized:
            await self._emit_candidate(candidate, accepted=False, task_id=task_id)
            logger.warning("skill promotion blocked by policy for %s", candidate.propose_name)
            return None

        result = self._validator.validate_candidate(candidate)
        if not result.ok:
            await self._emit_candidate(candidate, accepted=False, task_id=task_id)
            logger.warning("skill candidate invalid: %s", result.errors)
            return None

        draft = _with_trust(candidate.draft, TrustClass.AGENT_CURATED)
        if candidate.target_skill and await self.get(candidate.target_skill):
            # NOTE: latent bug fixed - `update` returns the new *version* (int),
            # not a skill_id (str). Previously _new_version (an int) flowed into
            # the return under the variable name `skill_id`; return the real id.
            await self.update(candidate.target_skill, draft)
            await self._emit_candidate(
                candidate, accepted=True, skill_id=candidate.target_skill, task_id=task_id
            )
            return candidate.target_skill
        skill_id = await self.install(draft, task_id=task_id)
        await self._emit_candidate(candidate, accepted=True, skill_id=skill_id, task_id=task_id)
        return skill_id

    async def search(self, query: str = "", *, limit: int = 10) -> List[Skill]:
        skills = await self.list(active_only=True)
        if not query:
            return skills[:limit]
        q = query.lower().strip()
        scored: list[tuple[int, Skill]] = []
        for s in skills:
            hay = f"{s.name}\n{s.description}\n{' '.join(s.triggers)}\n{s.body}"
            hay_l = hay.lower()
            if q in s.name.lower():
                score = 10 + hay_l.count(q)
            else:
                score = hay_l.count(q)
            if score:
                scored.append((score, s))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [s for _, s in scored[:limit]]

    async def trigger(
        self, skill_id: str, arguments: Mapping[str, Any], *, task_id: str | None = None
    ) -> Skill:
        skill = await self.get(skill_id)
        if skill is None:
            raise KeyError(f"no such skill: {skill_id}")
        if not _is_enabled(skill):
            raise ValueError(f"skill not active: {skill_id}")
        await self._emit(
            EventCategory.SKILL_ACTIVATED.value,
            {"skill_id": skill_id, "op": "trigger", "version": skill.version},
            task_id=task_id,
        )
        return skill

    async def load_active(self) -> List[Skill]:
        return await self.list(active_only=True)

    async def _emit(self, type_name: str, payload: Mapping[str, Any], *, task_id=None):
        if self._events is None:
            logger.info("skill lifecycle event: %s %s", type_name, dict(payload))
            return
        try:
            await self._events.append(make_event(type_name, payload, task_id=task_id))
        except Exception as exc:
            logger.warning("failed to append skill event: %s", exc)

    async def _emit_candidate(self, candidate, *, accepted, skill_id=None, task_id=None):
        payload = {
            "source_task_id": candidate.source_task_id,
            "target_skill": candidate.target_skill,
            "name": candidate.propose_name,
            "confidence": candidate.confidence,
            "accepted": accepted,
        }
        if skill_id:
            payload["skill_id"] = skill_id
        ev_type = (
            EventCategory.SKILL_ACTIVATED.value
            if accepted
            else EventCategory.SKILL_CANDIDATE_CREATED.value
        )
        await self._emit(ev_type, payload, task_id=task_id)


class SkillStore:
    """Facade for the :class:`~athena.capabilities.skills.SkillsCapability`
    wrapper and the context compiler (``search`` / ``trigger`` / ``load_active``).
    Prefers a persistent :class:`SkillLifecycle`; falls back to a loader for
    read-only search.
    """

    def __init__(self, loader=None, lifecycle=None) -> None:
        self._loader = loader
        self._lifecycle = lifecycle

    async def search(self, query: str = "", *, limit: int = 10) -> list[Skill]:
        if self._lifecycle is not None:
            return await self._lifecycle.search(query=query, limit=limit)
        loaded = await self._loader.load() if self._loader else []
        if not query:
            return loaded[:limit]
        q = query.lower()
        return [s for s in loaded if q in f"{s.name} {s.description} {s.body}".lower()][:limit]

    async def trigger(
        self, skill_id: str, arguments: Mapping[str, Any], *, task_id: str | None = None
    ) -> Skill:
        if self._lifecycle is not None:
            return await self._lifecycle.trigger(skill_id, arguments, task_id=task_id)
        raise KeyError(f"no such skill: {skill_id}")

    async def load_active(self) -> list[Skill]:
        if self._lifecycle is not None:
            return await self._lifecycle.list(active_only=True)
        return await self._loader.load_active() if self._loader else []


__all__ = ["SkillLifecycle", "SkillStore"]
