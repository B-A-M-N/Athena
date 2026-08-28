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
_NON_PROCEDURAL_CAPABILITIES = frozenset(
    {"delegate", "workflow", "scratch", "synthesis", "capsule"}
)


class KnowledgePipeline:
    """Finalize-observer feeding memory/skill self-improvement."""

    def __init__(
        self,
        *,
        messages: Any = None,
        memory_store: Any = None,
        skill_lifecycle: Any = None,
        workflow_store: Any = None,
        events: Any = None,
    ) -> None:
        self._messages = messages
        self._memory = memory_store
        self._skills = skill_lifecycle
        self._workflows = workflow_store
        self._events = events

    async def __call__(self, task: Any, result: Any) -> None:
        status = getattr(result.status, "value", result.status)
        if str(status) not in ("COMPLETE", "PARTIAL"):
            # Failed/cancelled work is not knowledge-worthy.
            return
        await self._ingest_memory(task, result)
        await self._propose_skill(task, result)
        # A partial task can contain useful history for memory/skill review,
        # but it is not proof that the complete procedure succeeded. Workflow
        # induction therefore requires the stronger terminal status.
        if str(status) == "COMPLETE":
            await self._propose_workflow(task, result)

    async def observe_workflow_execution(
        self,
        *,
        task_id: str | None,
        workflow: Any,
        outcome: Any,
    ) -> None:
        """Learn from a completed workflow through the same candidate store.

        Workflow internals are not guaranteed to be transcript messages, so
        the normal post-task transcript learner cannot see them reliably.  A
        successful run therefore reports its declarative definition here.
        This records a candidate observation only; promotion still requires
        the existing diverse-observation and replay gates.
        """
        if (
            self._workflows is None
            or not task_id
            or getattr(outcome, "status", None) != "completed"
        ):
            return
        try:
            steps = tuple(getattr(workflow, "steps", ()) or ())
            # A candidate that points at a task-local nested workflow would not
            # be portable after promotion.  Keep this learner precise until
            # nested graph packaging exists.
            if not steps or any(getattr(step, "workflow_id", None) for step in steps):
                return
            if any(
                not getattr(step, "capability_id", None)
                or getattr(step, "capability_id", None) in _NON_PROCEDURAL_CAPABILITIES
                for step in steps
            ):
                return
            from athena.affordances.models import AffordanceScope
            from athena.workflows.models import Workflow

            signature = _trace_signature(steps)
            run_id = getattr(outcome, "run_id", None)
            verification = {
                "status": "completed",
                "run_id": run_id,
                "output_keys": sorted(str(key) for key in (getattr(outcome, "outputs", {}) or {})),
            }
            find_candidate = getattr(
                self._workflows,
                "find_candidate_by_signature",
                None,
            )
            existing = await find_candidate(signature) if find_candidate is not None else None
            if existing is not None:
                record_observation = getattr(
                    self._workflows,
                    "record_candidate_observation",
                    None,
                )
                updated = (
                    await record_observation(existing.id, task_id=task_id, steps=steps)
                    if record_observation is not None
                    else existing
                )
                if self._events is not None:
                    await self._events.append_event(
                        "WorkflowCandidateObserved",
                        {
                            "workflow_id": existing.id,
                            "source_workflow_id": getattr(workflow, "id", None),
                            "task_id": task_id,
                            "source_run_id": run_id,
                            "verification": verification,
                            "successful_observations": (
                                (updated or existing).provenance.get("successful_observations", 1)
                            ),
                            "source": "workflow_execution",
                        },
                        task_id=task_id,
                    )
                return
            candidate = Workflow.create(
                name=f"workflow-{str(getattr(workflow, 'id', 'run'))[:12]} procedure",
                description="Candidate workflow learned from a verified workflow execution",
                steps=steps,
                scope=AffordanceScope.CANDIDATE,
                task_scope=task_id,
                provenance={
                    "origin": "successful_workflow_execution",
                    "source_workflow_id": getattr(workflow, "id", None),
                    "source_run_id": run_id,
                    "trace_signature": signature,
                    "verification": verification,
                    "observed_task_ids": [task_id],
                    "successful_observations": 1,
                    "observations": [
                        {
                            "task_id": task_id,
                            "source_workflow_id": getattr(workflow, "id", None),
                            "source_run_id": run_id,
                            "verification": verification,
                            "steps": [step.to_record() for step in steps],
                        }
                    ],
                },
            )
            await self._workflows.save(candidate)
            if self._events is not None:
                await self._events.append_event(
                    "WorkflowCandidateRecorded",
                    {
                        "workflow_id": candidate.id,
                        "source_workflow_id": getattr(workflow, "id", None),
                        "task_id": task_id,
                        "steps": len(steps),
                        "source": "workflow_execution",
                    },
                    task_id=task_id,
                )
        except Exception as exc:
            _logger.warning("workflow execution learning failed: %s", exc)

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

        candidates = await candidates_from_task(task, await self._transcript(task), result)
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
                _logger.warning("memory candidate save failed for %s: %s", rec.id, exc)
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

            drafts = await candidates_from_task(task, await self._transcript(task), result)
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

    async def _propose_workflow(self, task: Any, result: Any) -> None:
        """Retain a successful deterministic call sequence as a workflow candidate.

        This is deliberately conservative: only successful, ordinary
        capability calls with JSON arguments are retained.  Creation,
        delegation, workflow control, and scratch/synthesis calls are
        excluded because replaying those automatically would create an
        unbounded or task-owned loop.  The candidate is durable but not
        promoted or injected into another task without explicit review.
        """
        if self._workflows is None or not getattr(task, "id", None):
            return
        try:
            from athena.affordances.models import AffordanceScope
            from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock
            from athena.workflows.models import Workflow, WorkflowStep

            transcript = await self._transcript(task)
            calls: list[CapabilityCallBlock] = []
            results: dict[str, CapabilityResultBlock] = {}
            for message in transcript:
                for block in getattr(message, "blocks", ()):
                    if isinstance(block, CapabilityCallBlock):
                        calls.append(block)
                    elif isinstance(block, CapabilityResultBlock):
                        results[block.call_id] = block
            eligible = [
                call
                for call in calls
                if results.get(call.call_id) is not None
                and results[call.call_id].ok
                and call.capability_id not in _NON_PROCEDURAL_CAPABILITIES
            ]
            if len(eligible) < 2 or len(eligible) > 32:
                return
            steps = tuple(
                WorkflowStep(
                    id=f"step_{index}",
                    capability_id=call.capability_id,
                    arguments=dict(call.arguments or {}),
                )
                for index, call in enumerate(eligible, 1)
            )
            signature = _trace_signature(steps)
            find_candidate = getattr(self._workflows, "find_candidate_by_signature", None)
            if find_candidate is not None:
                existing = await find_candidate(signature)
                if existing is not None:
                    record_observation = getattr(
                        self._workflows, "record_candidate_observation", None
                    )
                    updated = (
                        await record_observation(existing.id, task_id=task.id, steps=steps)
                        if record_observation is not None
                        else existing
                    )
                    if self._events is not None:
                        await self._events.append_event(
                            "WorkflowCandidateObserved",
                            {
                                "workflow_id": existing.id,
                                "task_id": task.id,
                                "successful_observations": (
                                    (updated or existing).provenance.get(
                                        "successful_observations", 1
                                    )
                                ),
                            },
                            task_id=task.id,
                            session_id=getattr(task, "session_id", None),
                        )
                    return
            workflow = Workflow.create(
                name=f"task-{task.id[:12]} procedure",
                description="Candidate workflow induced from successful capability calls",
                steps=steps,
                scope=AffordanceScope.CANDIDATE,
                task_scope=task.id,
                provenance={
                    "origin": "successful_task_trace",
                    "task_id": task.id,
                    "call_count": len(steps),
                    "trace_signature": signature,
                    "observed_task_ids": [task.id],
                    "successful_observations": 1,
                    "observations": [
                        {
                            "task_id": task.id,
                            "steps": [step.to_record() for step in steps],
                        }
                    ],
                },
            )
            await self._workflows.save(workflow)
            if self._events is not None:
                await self._events.append_event(
                    "WorkflowCandidateRecorded",
                    {
                        "workflow_id": workflow.id,
                        "steps": len(workflow.steps),
                    },
                    task_id=task.id,
                    session_id=getattr(task, "session_id", None),
                )
        except Exception as exc:
            _logger.warning("workflow candidate proposal failed: %s", exc)


def _trace_signature(steps: tuple[Any, ...]) -> str:
    """Stable procedural signature that excludes task-specific arguments."""
    import hashlib
    import json

    shape = [
        {
            "capability": getattr(step, "capability_id", None),
            "workflow": getattr(step, "workflow_id", None),
            "operation": (getattr(step, "arguments", {}) or {}).get("operation"),
        }
        for step in steps
    ]
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
