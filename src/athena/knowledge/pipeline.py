"""Post-task knowledge pipeline (BUILDSPEC 64/68 — wired, not dormant).

After a task finalizes, this observer:
1. extracts memory candidates (episodic record + conservative lesson
   sentences) from the durable transcript via ``memory.candidates``;
2. saves episodic records directly (they are factual task history) and
   stores semantic lesson candidates flagged ``promotion=required`` so they
   surface in retrieval only as clearly-marked, low-trust candidates;
3. proposes skill drafts from successful transcripts and records them in the
   skill lifecycle catalog as pending validation (never auto-promoted).

Nothing here fabricates facts or bypasses trust/provenance rules (BHV-099,
BHV-102, BHV-107): the same conflict resolver and trust ranking apply as for
any other write.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["KnowledgePipeline"]

_logger = logging.getLogger("athena.knowledge")


class KnowledgePipeline:
    """Finalize-observer feeding memory/skill self-improvement."""

    def __init__(
        self,
        *,
        messages: Any = None,
        memory_store: Any = None,
        skill_lifecycle: Any = None,
        events: Any = None,
    ) -> None:
        self._messages = messages
        self._memory = memory_store
        self._skills = skill_lifecycle
        self._events = events

    async def __call__(self, task: Any, result: Any) -> None:
        status = getattr(result.status, "value", result.status)
        if str(status) not in ("COMPLETE", "PARTIAL"):
            # Failed/cancelled work is not knowledge-worthy.
            return
        await self._ingest_memory(task, result)
        await self._propose_skill(task, result)

    # ------------------------------------------------------------------ #
    async def _transcript(self, task: Any) -> list[Any]:
        if self._messages is None or not getattr(task, "session_id", None):
            return []
        try:
            return list(await self._messages.list_session_messages(task.session_id))
        except Exception as exc:
            _logger.warning("knowledge pipeline transcript load failed: %s", exc)
            return []

    async def _ingest_memory(self, task: Any, result: Any) -> None:
        from athena.memory.candidates import candidates_from_task

        candidates = await candidates_from_task(
            task, await self._transcript(task), result
        )
        saved = 0
        for rec in candidates:
            if self._memory is None:
                break
            try:
                promotion = (rec.metadata or {}).get("promotion")
                if promotion == "required":
                    # Semantic lesson candidate: keep it OUT of the model's
                    # retrieval path until a human/agent promotes it. Store
                    # under TASK scope tagged as pending instead of PROJECT.
                    rec = rec.__class__(
                        id=rec.id,
                        kind=rec.kind,
                        scope=rec.scope.__class__.TASK,
                        content=rec.content,
                        summary=rec.summary,
                        source=rec.source,
                        trust=rec.trust,
                        created_at=rec.created_at,
                        metadata={
                            **dict(rec.metadata),
                            "pending_promotion": True,
                            "task_id": getattr(task, "id", None),
                            "session_id": getattr(task, "session_id", None),
                        },
                    )
                await self._memory.save(rec)
                saved += 1
            except Exception as exc:
                _logger.warning(
                    "memory candidate save failed for %s: %s", rec.id, exc
                )
        if saved and self._events is not None:
            try:
                await self._events.append_event(
                    "MemoryCandidatesRecorded",
                    {"count": saved, "task_id": getattr(task, "id", None)},
                    task_id=getattr(task, "id", None),
                    session_id=getattr(task, "session_id", None),
                )
            except Exception as exc:
                _logger.warning("knowledge event emission failed: %s", exc)

    async def _propose_skill(self, task: Any, result: Any) -> None:
        if self._skills is None:
            return
        try:
            from athena.skills.candidates import candidates_from_task

            drafts = await candidates_from_task(
                task, await self._transcript(task), result
            )
        except Exception as exc:
            _logger.warning("skill proposal failed: %s", exc)
            return
        for draft in drafts:
            try:
                # Draft-only: recorded through promote(authorized=False) which
                # validates and emits a rejected-candidate event, keeping the
                # proposal in the audit trail WITHOUT activating it (BHV-107).
                # A later explicit promote(authorized=True) activates it.
                outcome = await self._skills.promote(
                    draft, task_id=getattr(task, "id", None), authorized=False
                )
                if outcome is None:
                    _logger.info(
                        "skill candidate %s recorded as pending validation",
                        draft.propose_name,
                    )
            except Exception as exc:
                _logger.warning("skill candidate record failed: %s", exc)
