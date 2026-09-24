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
from athena.skills.candidate_lifecycle import SkillCandidateLifecycle
from athena.skills.validator import SkillValidator
from athena.state.database import Database

logger = logging.getLogger(__name__)

_STATE_ENABLED = "enabled"
_STATE_DISABLED = "disabled"
_STATE_ARCHIVED = "archived"
_LIFEKEY = "athena.lifecycle"
_SCOPE_RANK = {"task": 1, "project": 2, "user": 3, "global": 4, "system": 4}


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
        tasks: Any = None,
        validator: SkillValidator | None = None,
        refresh_event_sink: Any = None,
    ) -> None:
        self._db = db
        self._events = events
        self._tasks = tasks
        self._refresh_event_sink = refresh_event_sink
        self._validator = validator or SkillValidator()
        self._candidates = SkillCandidateLifecycle(
            db=db,
            events=events,
            tasks=tasks,
            validator=self._validator,
            emit=self._emit,
            emit_candidate=self._emit_candidate,
            get_skill=self.get,
            install=self.install,
            update=self.update,
            draft_record=_candidate_draft_record,
            record_codec=_candidate_record,
            record_from_codec=_candidate_from_record,
        )

    async def _resolve_candidate_evidence(self, candidate: SkillCandidate) -> dict[str, Any]:
        return await self._candidates.resolve_evidence(candidate)

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

    async def record_selection_evidence(
        self,
        records: List[dict[str, Any]],
        *,
        task_id: str,
    ) -> int:
        """Persist deterministic skill opportunities and selections.

        Selection evidence is append-only and contains no task transcript or
        tool arguments.  It is a qualification record, never an authority to
        activate a skill.
        """
        count = 0
        for record in records:
            skill_id = str(record.get("skill_id") or "").strip()
            if not skill_id:
                continue
            try:
                version = int(record.get("version") or 1)
            except (TypeError, ValueError):
                continue
            payload = {
                "record_kind": "skill_selection",
                "skill_id": skill_id,
                "version": version,
                "task_id": str(task_id),
                "task_class": str(record.get("task_class") or "general"),
                "environment_fingerprint": str(record.get("environment_fingerprint") or ""),
                "score": float(record.get("score") or 0.0),
                "applicable": bool(record.get("applicable")),
                "selected": bool(record.get("selected")),
                "reason": str(record.get("reason") or ""),
                "evidence": [str(item) for item in (record.get("evidence") or ())],
            }
            await self._db.execute(
                "INSERT INTO skill_evidence("
                "id, skill_id, version, task_id, task_class, "
                "environment_fingerprint, evidence_kind, outcome, passed, "
                "cancelled, verified, payload, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("skill_evidence"),
                    skill_id,
                    version,
                    str(task_id),
                    payload["task_class"],
                    payload["environment_fingerprint"],
                    "selection",
                    "selected" if payload["selected"] else "eligible",
                    None,
                    False,
                    None,
                    json.dumps(payload, sort_keys=True),
                    utcnow().isoformat(),
                ),
            )
            count += 1
        return count

    async def record_outcome(
        self,
        skill_id: str,
        *,
        version: int,
        passed: bool,
        task_id: str | None = None,
        failure: Mapping[str, Any] | None = None,
        task_class: str = "unknown",
        environment_fingerprint: str = "",
        materially_contributed: bool | None = None,
    ) -> dict[str, Any]:
        """Record a terminal outcome for one exact skill revision.

        The evidence row records task outcome and verification state, while
        the skill metadata remains a compact ranking signal.  Cancellation is
        preserved rather than counted as a success or failure.
        """
        skill = await self.get(skill_id)
        if skill is None:
            return {"status": "missing", "skill_id": skill_id, "version": version}
        if skill.version != version:
            return {
                "status": "revision_mismatch",
                "skill_id": skill_id,
                "expected_version": version,
                "active_version": skill.version,
            }
        metadata = dict(skill.metadata)
        athena = dict(metadata.get("athena") or {})
        evidence = dict(athena.get("evidence") or {})
        status = str((failure or {}).get("status") or "COMPLETE")
        cancelled = status.casefold() == "cancelled"
        verified = bool((failure or {}).get("verified", status == "COMPLETE"))
        outcome_kind = (
            "cancelled"
            if cancelled
            else "verification_passed"
            if verified and passed
            else "verification_failed"
        )
        evidence_key = (
            "cancelled_reuses" if cancelled else "verified_reuses" if passed else "failed_reuses"
        )
        evidence[evidence_key] = int(evidence.get(evidence_key) or 0) + 1
        history = list(evidence.get("outcomes") or [])
        history.append(
            {
                "task_id": task_id,
                "passed": passed if not cancelled else None,
                "cancelled": cancelled,
                "verified": verified,
                "failure": dict(failure or {}),
                "version": version,
                "at": utcnow().isoformat(),
            }
        )
        evidence["outcomes"] = history[-32:]
        evidence["refinement_required"] = bool(evidence.get("failed_reuses"))
        evidence["reliability"] = _reliability_score(evidence)
        athena["evidence"] = evidence
        metadata["athena"] = athena
        await self._db.execute(
            "UPDATE skills SET metadata = ?, updated_at = ? WHERE id = ? AND version = ?",
            (json.dumps(metadata), utcnow().isoformat(), skill_id, version),
        )
        await self._db.execute(
            "INSERT INTO skill_evidence("
            "id, skill_id, version, task_id, task_class, "
            "environment_fingerprint, evidence_kind, outcome, passed, "
            "cancelled, verified, materially_contributed, payload, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("skill_evidence"),
                skill_id,
                version,
                str(task_id or ""),
                str(task_class or "unknown"),
                str(environment_fingerprint or ""),
                "outcome",
                outcome_kind,
                None if cancelled else passed,
                cancelled,
                verified,
                materially_contributed,
                json.dumps({"failure": dict(failure or {})}, sort_keys=True),
                utcnow().isoformat(),
            ),
        )
        await self._emit(
            EventCategory.SKILL_ACTIVATED.value,
            {
                "skill_id": skill_id,
                "op": "outcome",
                "version": version,
                "passed": None if cancelled else passed,
                "cancelled": cancelled,
                "verified": verified,
                "refinement_required": evidence["refinement_required"],
            },
            task_id=task_id,
        )
        return {
            "status": "recorded",
            "skill_id": skill_id,
            "version": version,
            "passed": None if cancelled else passed,
            "cancelled": cancelled,
            "verified": verified,
            "materially_contributed": materially_contributed,
            "refinement_required": evidence["refinement_required"],
            "reliability": evidence["reliability"],
        }

    async def record_candidate(
        self,
        candidate: SkillCandidate,
        *,
        task_id: str | None = None,
        lifecycle_state: str = "PENDING_REVIEW",
    ) -> str:
        return await self._candidates.record(
            candidate, task_id=task_id, lifecycle_state=lifecycle_state
        )

    async def list_candidates(self, *, include_reviewed: bool = False) -> List[dict[str, Any]]:
        return await self._candidates.list_candidates(include_reviewed=include_reviewed)

    async def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        return await self._candidates.get_candidate(candidate_id)

    async def discard_candidate(self, candidate_id: str) -> bool:
        return await self._candidates.discard(candidate_id)

    async def promote_candidate(
        self, candidate_id: str, *, task_id: str | None = None, authorized: bool = True
    ) -> str | None:
        return await self._candidates.promote_candidate(
            candidate_id, task_id=task_id, authorized=authorized
        )

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
        self, candidate: SkillCandidate, *, task_id: str | None = None, authorized: bool = True
    ) -> str | None:
        return await self._candidates.promote(candidate, task_id=task_id, authorized=authorized)

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

    async def _emit_candidate(
        self,
        candidate,
        *,
        accepted,
        skill_id=None,
        task_id=None,
        candidate_id=None,
        lifecycle_state=None,
    ):
        payload = {
            "source_task_id": candidate.source_task_id,
            "target_skill": candidate.target_skill,
            "target_skill_version": candidate.target_skill_version,
            "name": candidate.propose_name,
            "confidence": candidate.confidence,
            "confidence_kind": "heuristic_proposal",
            "accepted": accepted,
            "candidate_id": candidate_id or candidate.id,
            "lifecycle_state": lifecycle_state or ("PROMOTED" if accepted else "PENDING_REVIEW"),
        }
        if skill_id:
            payload["skill_id"] = skill_id
        ev_type = (
            EventCategory.SKILL_ACTIVATED.value
            if accepted
            else EventCategory.SKILL_CANDIDATE_CREATED.value
        )
        await self._emit(ev_type, payload, task_id=task_id)


def _reliability_score(evidence: Mapping[str, Any]) -> float:
    """Return a bounded empirical reuse signal, never proposal confidence."""
    passed = int(evidence.get("verified_reuses") or 0)
    failed = int(evidence.get("failed_reuses") or 0)
    total = passed + failed
    if total == 0:
        return 0.0
    return round(max(0.0, min(1.0, (passed + 0.5) / (total + 1.0))), 6)


def _candidate_draft_record(skill: Skill) -> dict[str, Any]:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "body": skill.body,
        "triggers": list(skill.triggers),
        "scope": skill.scope,
        "trust": skill.trust.value,
        "version": skill.version,
        "metadata": dict(skill.metadata),
    }


def _candidate_record(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        draft = json.loads(str(row.get("draft") or "{}"))
    except (TypeError, ValueError):
        draft = {}
    try:
        evidence = json.loads(str(row.get("evidence") or "[]"))
    except (TypeError, ValueError):
        evidence = []
    try:
        metadata = json.loads(str(row.get("metadata") or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    return {
        "id": str(row.get("id") or ""),
        "type": "skill",
        "name": str(row.get("name") or draft.get("name") or ""),
        "draft": draft,
        "source_task": str(row.get("source_task_id") or ""),
        "evidence": evidence if isinstance(evidence, list) else [],
        "observation_count": 1,
        "last_observed_at": row.get("updated_at"),
        "proposed_scope": draft.get("scope") or "user",
        "trust": draft.get("trust") or TrustClass.AGENT_CURATED.value,
        "conflicts": [],
        "required_action": "operator_review",
        "lifecycle_state": str(row.get("lifecycle_state") or "PENDING_REVIEW"),
        "rationale": str(row.get("rationale") or ""),
        "confidence": float(row.get("confidence") or 0.0),
        "promoted_skill_id": row.get("promoted_skill_id"),
        "target_skill": row.get("target_skill_id"),
        "target_skill_version": row.get("target_skill_version"),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def _candidate_from_record(row: Mapping[str, Any]) -> SkillCandidate:
    raw = json.loads(str(row.get("draft") or "{}"))
    try:
        trust = TrustClass(str(raw.get("trust") or TrustClass.AGENT_CURATED.value))
    except ValueError:
        trust = TrustClass.AGENT_CURATED
    draft = Skill(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or row.get("name") or ""),
        description=str(raw.get("description") or ""),
        body=str(raw.get("body") or ""),
        triggers=tuple(str(item) for item in raw.get("triggers") or ()),
        scope=str(raw.get("scope") or "user"),
        trust=trust,
        version=int(raw.get("version") or 1),
        metadata=dict(raw.get("metadata") or {}),
    )
    try:
        evidence = json.loads(str(row.get("evidence") or "[]"))
    except (TypeError, ValueError):
        evidence = []
    return SkillCandidate(
        draft=draft,
        source_task_id=str(row.get("source_task_id") or ""),
        target_skill=(str(row["target_skill_id"]) if row.get("target_skill_id") else None),
        rationale=str(row.get("rationale") or ""),
        evidence=tuple(str(item) for item in evidence or ()),
        confidence=float(row.get("confidence") or 0.0),
        target_skill_version=(
            int(row["target_skill_version"])
            if row.get("target_skill_version") is not None
            else None
        ),
    )


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

    async def record_selection_evidence(
        self, records: list[dict[str, Any]], *, task_id: str
    ) -> int:
        if self._lifecycle is not None:
            return await self._lifecycle.record_selection_evidence(records, task_id=task_id)
        return 0

    async def record_outcome(self, skill_id: str, **kwargs: Any) -> dict[str, Any]:
        if self._lifecycle is not None:
            return await self._lifecycle.record_outcome(skill_id, **kwargs)
        return {"status": "unavailable", "skill_id": skill_id}

    async def load_active(self) -> list[Skill]:
        if self._lifecycle is not None:
            return await self._lifecycle.list(active_only=True)
        return await self._loader.load_active() if self._loader else []

    async def refresh_file_backed(self) -> dict[str, Any]:
        """Refresh file-backed skills without mutating the current turn.

        The loader owns parsing and conflict detection. This facade only
        reconciles newly discovered name/version pairs into the durable
        lifecycle; unchanged-version content conflicts remain report-only.
        """
        if self._loader is None:
            return {"status": "unavailable", "refreshed": 0, "installed": 0, "conflicts": []}
        refreshed, conflicts = self._loader.refresh()
        installed = 0
        if self._lifecycle is not None:
            existing = await self._lifecycle.list()
            known = {(skill.name, skill.version) for skill in existing}
            for skill in refreshed:
                key = (skill.name, skill.version)
                if key in known:
                    continue
                if any(
                    conflict.get("name") == skill.name and conflict.get("version") == skill.version
                    for conflict in conflicts
                ):
                    continue
                await self._lifecycle.install(skill)
                known.add(key)
                installed += 1
        result = {
            "status": "conflicts" if conflicts else "refreshed",
            "refreshed": len(refreshed),
            "installed": installed,
            "conflicts": conflicts,
        }
        if self._lifecycle is not None and self._lifecycle._refresh_event_sink is not None:
            await self._lifecycle._refresh_event_sink(result)
        return result


__all__ = ["SkillLifecycle", "SkillStore"]
