"""Skill candidate persistence, evidence resolution, and promotion mechanics."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Mapping

from athena.protocol.messages import TrustClass, utcnow
from athena.skills.models import Skill, SkillCandidate
from athena.state.database import Database

logger = logging.getLogger(__name__)

__all__ = ["SkillCandidateLifecycle"]

_SCOPE_RANK = {"task": 1, "project": 2, "user": 3, "global": 4, "system": 4}
_STATE_ENABLED = "enabled"
_LIFEKEY = "athena.lifecycle"


class SkillCandidateLifecycle:
    """Own candidate queue and promotion while the parent owns active skills."""

    def __init__(
        self,
        *,
        db: Database,
        events: Any,
        tasks: Any,
        validator: Any,
        emit: Callable[..., Awaitable[None]],
        emit_candidate: Callable[..., Awaitable[None]],
        get_skill: Callable[[str], Awaitable[Skill | None]],
        install: Callable[..., Awaitable[str]],
        update: Callable[[str, Skill], Awaitable[int]],
        draft_record: Callable[[Skill], dict[str, Any]],
        record_codec: Callable[[Mapping[str, Any]], dict[str, Any]],
        record_from_codec: Callable[[Mapping[str, Any]], SkillCandidate],
    ) -> None:
        self._db = db
        self._events = events
        self._tasks = tasks
        self._validator = validator
        self._emit = emit
        self._emit_candidate = emit_candidate
        self._get_skill = get_skill
        self._install = install
        self._update = update
        self._draft_record = draft_record
        self._record_codec = record_codec
        self._record_from_codec = record_from_codec

    async def resolve_evidence(self, candidate: SkillCandidate) -> dict[str, Any]:
        source = str(candidate.source_task_id or "")
        if not source or self._tasks is None:
            return {"status": "unresolved", "source_task_id": source, "resolved": []}
        source_row = await self._tasks.get(source)
        if source_row is None:
            return {"status": "unresolved", "source_task_id": source, "resolved": []}
        resolved: list[dict[str, str]] = [{"kind": "task", "ref": source}]
        missing: list[str] = []
        if self._events is None:
            missing.extend(candidate.evidence)
        else:
            events = await self._events.list_for_task(source)
            by_id = {str(event.id): event for event in events}
            for ref in candidate.evidence:
                event = by_id.get(str(ref))
                if event is None:
                    missing.append(str(ref))
                else:
                    resolved.append({"kind": "event", "ref": str(event.id), "type": event.type})
        return {
            "status": "resolved" if not missing else "unresolved",
            "source_task_id": source,
            "resolved": resolved,
            "missing": missing,
            "semantic_status": "verified" if not missing else "unverified",
        }

    async def record(
        self,
        candidate: SkillCandidate,
        *,
        task_id: str | None = None,
        lifecycle_state: str = "PENDING_REVIEW",
    ) -> str:
        result = self._validator.validate_candidate(candidate)
        if not result.ok:
            lifecycle_state = "REJECTED"
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO skill_candidates("
            "id, source_task_id, name, draft, rationale, evidence, confidence, "
            "lifecycle_state, promoted_skill_id, target_skill_id, "
            "target_skill_version, created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
            "lifecycle_state=CASE WHEN skill_candidates.lifecycle_state IN "
            "('PROMOTED', 'DEPRECATED') THEN skill_candidates.lifecycle_state "
            "ELSE excluded.lifecycle_state END",
            (
                candidate.id,
                candidate.source_task_id,
                candidate.propose_name,
                json.dumps(self._draft_record(candidate.draft), sort_keys=True),
                candidate.rationale,
                json.dumps(list(candidate.evidence), sort_keys=True),
                float(candidate.confidence),
                lifecycle_state,
                candidate.target_skill,
                candidate.target_skill_version,
                now,
                now,
                json.dumps(
                    {
                        "task_id": task_id or candidate.source_task_id,
                        "confidence_kind": "heuristic_proposal",
                        "validation_errors": list(result.errors),
                        "validation_warnings": list(result.warnings),
                    },
                    sort_keys=True,
                ),
            ),
        )
        await self._emit_candidate(
            candidate,
            accepted=False,
            task_id=task_id,
            candidate_id=candidate.id,
            lifecycle_state=lifecycle_state,
        )
        return candidate.id

    async def list_candidates(self, *, include_reviewed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM skill_candidates"
        if not include_reviewed:
            sql += " WHERE lifecycle_state IN ('PENDING_REVIEW', 'REJECTED')"
        sql += " ORDER BY updated_at DESC, id"
        rows = await self._db.fetch_all(sql)
        return [self._record_codec(row) for row in rows]

    async def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM skill_candidates WHERE id = ?", (candidate_id,)
        )
        return self._record_codec(row) if row else None

    async def discard(self, candidate_id: str) -> bool:
        row = await self._db.fetch_one(
            "SELECT lifecycle_state FROM skill_candidates WHERE id = ?", (candidate_id,)
        )
        if row is None or str(row.get("lifecycle_state")) not in {"PENDING_REVIEW", "REJECTED"}:
            return False
        await self._db.execute(
            "UPDATE skill_candidates SET lifecycle_state = 'DEPRECATED', updated_at = ? WHERE id = ?",
            (utcnow().isoformat(), candidate_id),
        )
        return True

    async def promote_candidate(
        self,
        candidate_id: str,
        *,
        task_id: str | None = None,
        authorized: bool = True,
    ) -> str | None:
        row = await self._db.fetch_one(
            "SELECT * FROM skill_candidates WHERE id = ?", (candidate_id,)
        )
        if row is None or str(row.get("lifecycle_state")) != "PENDING_REVIEW":
            return None
        skill_id = await self.promote(
            self._record_from_codec(row), task_id=task_id, authorized=authorized
        )
        if skill_id is None:
            return None
        await self._db.execute(
            "UPDATE skill_candidates SET lifecycle_state = 'PROMOTED', "
            "promoted_skill_id = ?, updated_at = ? WHERE id = ?",
            (skill_id, utcnow().isoformat(), candidate_id),
        )
        return skill_id

    async def promote(
        self,
        candidate: SkillCandidate,
        *,
        task_id: str | None = None,
        authorized: bool = True,
    ) -> str | None:
        if not authorized:
            await self.record(candidate, task_id=task_id)
            logger.warning("skill promotion blocked by policy for %s", candidate.propose_name)
            return None
        evidence_status = await self.resolve_evidence(candidate)
        if evidence_status["status"] != "resolved":
            await self._emit_candidate(
                candidate,
                accepted=False,
                task_id=task_id,
                lifecycle_state="EVIDENCE_UNRESOLVED",
            )
            logger.warning(
                "skill promotion refused: candidate evidence unresolved (%s)", evidence_status
            )
            return None
        result = self._validator.validate_candidate(candidate)
        if not result.ok:
            await self._emit_candidate(candidate, accepted=False, task_id=task_id)
            logger.warning("skill candidate invalid: %s", result.errors)
            return None
        draft = Skill(**{**candidate.draft.__dict__, "trust": TrustClass.AGENT_CURATED})
        current = await self._get_skill(candidate.target_skill) if candidate.target_skill else None
        if candidate.target_skill and current is not None:
            current_scope = str(current.scope or "user")
            proposed_scope = str(draft.scope or "user")
            if _SCOPE_RANK.get(proposed_scope, 0) > _SCOPE_RANK.get(current_scope, 0):
                observed = evidence_status.get("observed_environments", ())
                if len(evidence_status.get("resolved") or ()) < 3 or len(observed) < 2:
                    await self._emit_candidate(
                        candidate,
                        accepted=False,
                        task_id=task_id,
                        lifecycle_state="INSUFFICIENT_WIDENING_EVIDENCE",
                    )
                    logger.warning(
                        "skill promotion refused: scope widening from %s to %s needs "
                        "multiple observed environments",
                        current_scope,
                        proposed_scope,
                    )
                    return None
            if (
                candidate.target_skill_version is not None
                and current.version != candidate.target_skill_version
            ):
                await self._emit_candidate(
                    candidate, accepted=False, task_id=task_id, lifecycle_state="REVISION_CONFLICT"
                )
                logger.warning(
                    "skill promotion target changed for %s: expected version %s, found %s",
                    candidate.target_skill,
                    candidate.target_skill_version,
                    current.version,
                )
                return None
            if candidate.target_skill_version is None:
                await self._emit_candidate(
                    candidate, accepted=False, task_id=task_id, lifecycle_state="REVISION_CONFLICT"
                )
                logger.warning(
                    "skill promotion target has no expected version: %s", candidate.target_skill
                )
                return None
            await self._update(candidate.target_skill, draft)
            await self._emit_candidate(
                candidate, accepted=True, skill_id=candidate.target_skill, task_id=task_id
            )
            return candidate.target_skill
        if candidate.target_skill:
            await self._emit_candidate(
                candidate, accepted=False, task_id=task_id, lifecycle_state="TARGET_MISSING"
            )
            logger.warning("skill promotion target no longer exists: %s", candidate.target_skill)
            return None
        skill_id = await self._install(draft, task_id=task_id)
        await self._emit_candidate(candidate, accepted=True, skill_id=skill_id, task_id=task_id)
        return skill_id
