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
import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from athena.protocol.messages import TrustClass, utcnow
from athena.protocol.policy import DEFAULT_PRINCIPAL_ID

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
        principal_id: str = DEFAULT_PRINCIPAL_ID,
    ) -> None:
        self._messages = messages
        self._memory = memory_store
        self._skills = skill_lifecycle
        self._workflows = workflow_store
        self._events = events
        self._principal_id = principal_id
        # Compatibility fallback for lightweight stores that do not implement
        # durable workflow observations. The production WorkflowStore keeps
        # this evidence outside the workflow-definition table.
        self._pending_workflow_traces: dict[str, tuple[str, tuple[Any, ...]]] = {}

    async def __call__(self, task: Any, result: Any) -> None:
        status = getattr(result.status, "value", result.status)
        status = str(status)
        if status not in ("COMPLETE", "PARTIAL", "FAILED", "CANCELLED"):
            return
        objective = str(getattr(task, "objective", "") or "").strip()
        if objective and _is_ephemeral_objective(objective):
            return
        transcript = _task_transcript(task, await self._transcript(task))
        successful_calls = _successful_ordinary_calls(transcript)
        selected_skills = await self._selected_skill_versions(task)
        used_skills = await self._used_skill_versions(task, selected_skills)
        if selected_skills and not used_skills:
            selected_skills = ()
        if selected_skills:
            await self._record_skill_outcomes(
                task,
                result,
                selected_skills,
                passed=status == "COMPLETE",
            )
        if (
            status == "FAILED"
            and selected_skills
            and _skill_learning_eligible(task, transcript, successful_calls)
        ):
            # A failed reuse is refinement evidence, not a reason to replace
            # the active skill. The candidate lifecycle validates/preserves
            # the exact source revision until explicit promotion.
            await self._propose_skill(
                task,
                result,
                transcript=transcript,
                target_skill_versions=selected_skills,
            )
            return
        # A completed turn is not automatically a learning event. Greetings,
        # thanks, and ordinary conversational questions remain ephemeral; the
        # specialized candidate gates still decide whether evidence is strong
        # enough for a durable memory or reusable procedure.
        if _memory_learning_eligible(task, transcript, successful_calls, result):
            await self._ingest_memory(task, result, transcript=transcript)
        if str(status) == "COMPLETE":
            await self._ingest_job_memory(task, result)
        if _skill_learning_eligible(task, transcript, successful_calls):
            await self._propose_skill(task, result, transcript=transcript)
        # A partial task can contain useful history for memory/skill review,
        # but it is not proof that the complete procedure succeeded. Workflow
        # induction therefore requires the stronger terminal status.
        if str(status) == "COMPLETE" and _workflow_learning_eligible(
            task, transcript, successful_calls, result
        ):
            await self._propose_workflow(task, result, transcript=transcript)

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
            from athena.protocol.affordances import AffordanceScope
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
                    await record_observation(
                        existing.id,
                        task_id=task_id,
                        steps=steps,
                        verification=verification,
                        observed_at=utcnow().isoformat(),
                    )
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
            task_loader = getattr(self._messages, "list_task_messages", None)
            if task_loader is not None and getattr(task, "id", None):
                return list(
                    await task_loader(
                        task.session_id,
                        task.id,
                    )
                )
            recent_loader = getattr(self._messages, "list_recent_session_messages", None)
            if recent_loader is not None:
                return list(await recent_loader(task.session_id))
            return list(await self._messages.list_session_messages(task.session_id))
        except Exception as exc:
            _logger.warning("knowledge pipeline transcript load failed: %s", exc)
            return []

    async def _selected_skill_versions(self, task: Any) -> tuple[tuple[str, int], ...]:
        """Recover exact skill revisions injected during this task."""
        if self._events is None or not getattr(task, "id", None):
            return ()
        try:
            events = await self._events.list_for_task(task.id)
        except Exception as exc:
            _logger.warning("skill selection evidence lookup failed: %s", exc)
            return ()
        selected: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for event in events:
            if getattr(event, "type", None) != "SkillContextSelected":
                continue
            payload = getattr(event, "payload", {}) or {}
            for item in payload.get("skills", ()):
                if not isinstance(item, Mapping) or not item.get("skill_id"):
                    continue
                try:
                    ref = (str(item["skill_id"]), int(item.get("version") or 1))
                except (TypeError, ValueError):
                    continue
                if ref not in seen:
                    seen.add(ref)
                    selected.append(ref)
        return tuple(selected)

    async def _used_skill_versions(
        self, task: Any, selected: tuple[tuple[str, int], ...]
    ) -> tuple[tuple[str, int], ...]:
        if not selected or self._events is None:
            return ()
        selected_ids = {skill_id for skill_id, _ in selected}
        try:
            events = await self._events.list_for_task(getattr(task, "id", ""))
        except Exception as exc:
            _logger.warning("skill usage evidence lookup failed: %s", exc)
            return ()
        used: set[tuple[str, int]] = set()
        for event in events:
            if getattr(event, "type", None) != "SkillApplied":
                continue
            payload = getattr(event, "payload", {}) or {}
            skill_id = str(payload.get("skill_id") or "")
            if skill_id not in selected_ids:
                continue
            try:
                version = int(payload.get("version") or 0)
            except (TypeError, ValueError):
                continue
            if (skill_id, version) in selected:
                used.add((skill_id, version))
        return tuple(item for item in selected if item in used)

    async def _record_skill_outcomes(
        self,
        task: Any,
        result: Any,
        selected: tuple[tuple[str, int], ...],
        *,
        passed: bool,
    ) -> None:
        if self._skills is None:
            return
        failure = {
            "status": getattr(
                getattr(result, "status", None), "value", getattr(result, "status", "")
            ),
            "summary": str(getattr(result, "summary", "") or "")[:1000],
            "unresolved": [str(item) for item in (getattr(result, "unresolved", ()) or ())[:8]],
        }
        for skill_id, version in selected:
            try:
                await self._skills.record_outcome(
                    skill_id,
                    version=version,
                    passed=passed,
                    task_id=getattr(task, "id", None),
                    failure={} if passed else failure,
                )
            except Exception as exc:
                _logger.warning("skill outcome recording failed for %s: %s", skill_id, exc)

    async def _ingest_memory(
        self, task: Any, result: Any, *, transcript: list[Any] | None = None
    ) -> None:
        from athena.memory.candidates import candidates_from_task

        candidates = await candidates_from_task(
            task,
            transcript if transcript is not None else await self._transcript(task),
            result,
            principal_id=self._principal_id,
        )
        saved = 0
        for rec in candidates:
            if self._memory is None:
                break
            try:
                promotion = (rec.metadata or {}).get("promotion")
                candidate_type = (rec.metadata or {}).get("candidate_type")
                if promotion == "required" and candidate_type == "explicit_user_fact":
                    # A direct user assertion is already authoritative input
                    # for this session. It must be retrievable immediately;
                    # the pending-candidate lifecycle is for agent-derived
                    # lessons, not for making the user repeat a fact.
                    rec = rec.__class__(
                        id=rec.id,
                        kind=rec.kind,
                        scope=rec.scope,
                        content=rec.content,
                        summary=rec.summary,
                        source=rec.source,
                        trust=TrustClass.USER_CONTENT,
                        created_at=rec.created_at,
                        metadata={
                            **dict(rec.metadata),
                            "promotion": "automatic_user_content",
                            "pending_promotion": False,
                        },
                    )
                elif promotion == "required":
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
        if saved and self._memory is not None:
            # Pending lessons are review material, not an unbounded second
            # memory store. Keep a bounded recent queue and age out stale
            # candidates while leaving explicit user facts untouched.
            try:
                expire = getattr(self._memory, "expire_pending_candidates", None)
                if callable(expire):
                    await expire(utcnow() - timedelta(days=30))
                compact = getattr(self._memory, "compact_pending_candidates", None)
                if callable(compact):
                    await compact(512)
            except Exception as exc:
                _logger.warning("pending memory candidate retention failed: %s", exc)

    async def _ingest_job_memory(self, task: Any, result: Any) -> None:
        """Persist compact, successful recurring-job state in JOB scope.

        This is intentionally separate from ordinary lesson candidates: only
        schedules that explicitly selected ``job_memory`` can create it, and
        the record is bounded to the last few occurrences so a recurring job
        cannot grow an unbounded transcript-shaped memory stream.
        """
        if self._memory is None:
            return
        metadata = getattr(task, "metadata", None) or {}
        lineage = metadata.get("_schedule_lineage") if isinstance(metadata, Mapping) else None
        if not isinstance(lineage, Mapping) or lineage.get("continuity") != "job_memory":
            return
        job_id = str(lineage.get("job_id") or "").strip()
        if not job_id:
            return
        try:
            from athena.memory.store import new_memory_id
            from athena.protocol.memory import MemoryKind, MemoryScope, MemoryRecord
            from athena.protocol.messages import Provenance, SourceType, TrustClass

            status = getattr(result.status, "value", result.status)
            unresolved = tuple(str(item) for item in (getattr(result, "unresolved", ()) or ()))
            summary = str(getattr(result, "summary", "") or "").strip()
            content = (
                f"scheduled job {job_id} completed: {summary[:1200]}"
                + (f"; unresolved: {', '.join(unresolved[:8])}" if unresolved else "")
            )[:2000]
            record = MemoryRecord(
                id=new_memory_id(MemoryKind.EPISODIC),
                kind=MemoryKind.EPISODIC,
                scope=MemoryScope.JOB,
                content=content,
                summary="compact successful scheduled-job state",
                source=Provenance(
                    source_type=SourceType.TASK,
                    source_id=str(getattr(task, "id", "")),
                    trust=TrustClass.AGENT_CURATED,
                    scope=f"job:{job_id}",
                ),
                trust=TrustClass.AGENT_CURATED,
                metadata={
                    "scope_id": job_id,
                    "job_id": job_id,
                    "task_id": getattr(task, "id", None),
                    "status": status,
                    "promotion": "automatic_job_state",
                    "candidate_type": "scheduled_job_state",
                    "pending_promotion": False,
                },
            )
            await self._memory.save_with_outcome(record)
            list_by_scope = getattr(self._memory, "list_by_scope", None)
            delete = getattr(self._memory, "delete", None)
            if callable(list_by_scope) and callable(delete):
                existing = await list_by_scope(MemoryScope.JOB, job_id)
                states = [
                    item
                    for item in existing
                    if (getattr(item, "metadata", {}) or {}).get("candidate_type")
                    == "scheduled_job_state"
                ]
                states.sort(key=lambda item: (getattr(item, "created_at", utcnow()), item.id))
                for stale in states[:-8]:
                    await delete(stale.id)
        except Exception as exc:
            _logger.warning("scheduled job memory ingestion failed: %s", exc)

    async def _propose_skill(
        self,
        task: Any,
        result: Any,
        *,
        transcript: list[Any] | None = None,
        target_skill_versions: tuple[tuple[str, int], ...] = (),
    ) -> None:
        if self._skills is None:
            return
        try:
            from athena.skills.candidates import candidates_from_task

            transcript = transcript if transcript is not None else await self._transcript(task)
            targets = []
            if target_skill_versions:
                for skill_id, version in target_skill_versions:
                    skill = await self._skills.get(skill_id)
                    if skill is not None and skill.version == version:
                        targets.append(skill)
            if targets:
                drafts = []
                for target in targets:
                    drafts.extend(
                        await candidates_from_task(task, transcript, result, target_skill=target)
                    )
            else:
                drafts = await candidates_from_task(task, transcript, result)
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

    async def _propose_workflow(
        self, task: Any, result: Any, *, transcript: list[Any] | None = None
    ) -> None:
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
            from athena.protocol.affordances import AffordanceScope
            from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock
            from athena.workflows.models import Workflow, WorkflowStep

            transcript = transcript if transcript is not None else await self._transcript(task)
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
            workspace = getattr(task, "workspace", None)
            status = getattr(result.status, "value", result.status)
            verification = {
                "status": str(status or "unknown"),
                "evidence_count": len(getattr(result, "evidence", ()) or ()),
                "artifact_count": len(getattr(result, "artifacts", ()) or ()),
                "mutation_count": len(getattr(result, "mutations", ()) or ()),
            }
            observation_kwargs = {
                "workspace_id": getattr(workspace, "id", None),
                "workspace_revision": getattr(workspace, "revision", None),
                "verification": verification,
                "observed_at": utcnow().isoformat(),
            }
            find_candidate = getattr(self._workflows, "find_candidate_by_signature", None)
            if find_candidate is not None:
                existing = await find_candidate(signature)
                if existing is not None:
                    record_observation = getattr(
                        self._workflows, "record_candidate_observation", None
                    )
                    updated = (
                        await record_observation(
                            existing.id,
                            task_id=task.id,
                            steps=steps,
                            **observation_kwargs,
                        )
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
            persist_pending = getattr(self._workflows, "save_pending_observation", None)
            if callable(persist_pending):
                observed = await persist_pending(
                    signature,
                    task_id=task.id,
                    steps=steps,
                    **observation_kwargs,
                )
                lifecycle = str(getattr(observed, "lifecycle_state", ""))
                if lifecycle == "CANDIDATE":
                    if self._events is not None:
                        await self._events.append_event(
                            "WorkflowCandidateRecorded",
                            {
                                "workflow_id": getattr(observed, "id", None),
                                "task_id": task.id,
                                "steps": len(steps),
                                "trace_signature": signature,
                                "successful_observations": getattr(observed, "provenance", {}).get(
                                    "successful_observations", 2
                                ),
                                "source": "repeated_successful_task_traces",
                            },
                            task_id=task.id,
                            session_id=getattr(task, "session_id", None),
                        )
                return

            pending = self._pending_workflow_traces.get(signature)
            if pending is None or pending[0] == task.id:
                self._pending_workflow_traces[signature] = (task.id, steps)
                return

            # Two independent successful traces are the minimum repeatability
            # proof for task-derived workflow induction. Generalize only now,
            # after the second observation, and keep both observations in the
            # candidate's provenance for review.
            from athena.workflows.mining import merge_observation

            first_task_id, first_steps = pending
            seed = Workflow.create(
                name=f"task-{first_task_id[:12]} procedure",
                description="Candidate workflow induced from repeated successful capability calls",
                steps=first_steps,
                scope=AffordanceScope.CANDIDATE,
                task_scope=first_task_id,
                provenance={
                    "origin": "successful_task_trace",
                    "task_id": first_task_id,
                    "call_count": len(first_steps),
                    "trace_signature": signature,
                    "observed_task_ids": [first_task_id],
                    "successful_observations": 1,
                    "observations": [
                        {
                            "task_id": first_task_id,
                            "steps": [step.to_record() for step in first_steps],
                        }
                    ],
                },
            )
            workflow = merge_observation(seed, task_id=task.id, steps=steps)
            self._pending_workflow_traces.pop(signature, None)
            await self._workflows.save(workflow)
            if self._events is not None:
                await self._events.append_event(
                    "WorkflowCandidateRecorded",
                    {
                        "workflow_id": workflow.id,
                        "steps": len(workflow.steps),
                        "successful_observations": workflow.provenance.get(
                            "successful_observations", 2
                        ),
                        "source": "repeated_successful_task_traces",
                    },
                    task_id=task.id,
                    session_id=getattr(task, "session_id", None),
                )
            return
        except Exception as exc:
            _logger.warning("workflow candidate proposal failed: %s", exc)


def _successful_ordinary_calls(transcript: list[Any]) -> list[Any]:
    from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock

    calls: dict[str, Any] = {}
    results: dict[str, CapabilityResultBlock] = {}
    for message in transcript:
        for block in getattr(message, "blocks", ()):
            if isinstance(block, CapabilityCallBlock):
                calls[block.call_id] = block
            elif isinstance(block, CapabilityResultBlock):
                results[block.call_id] = block
    return [
        call
        for call in calls.values()
        if call.capability_id not in _NON_PROCEDURAL_CAPABILITIES
        and results.get(call.call_id) is not None
        and results[call.call_id].ok
    ]


def _task_transcript(task: Any, transcript: list[Any]) -> list[Any]:
    """Limit learning to the current task's turn in a shared session."""
    task_id = str(getattr(task, "id", "") or "")
    if not task_id:
        return transcript
    for index, message in enumerate(transcript):
        metadata = getattr(message, "metadata", {}) or {}
        if (
            metadata.get("canonical_user_turn") is True
            and str(metadata.get("task_id") or "") == task_id
        ):
            # A shared session may already contain the next queued turn when
            # this task finalizes. Stop at that next service-owned intake
            # marker so one task cannot learn from a neighboring task.
            for end, following in enumerate(transcript[index + 1 :], index + 1):
                following_metadata = getattr(following, "metadata", {}) or {}
                if following_metadata.get("canonical_user_turn") is True:
                    return transcript[index:end]
            return transcript[index:]
    # Compatibility fixtures may not include the service-owned intake marker.
    return transcript


def _is_ephemeral_objective(objective: Any) -> bool:
    text = " ".join(str(objective or "").casefold().split())
    if not text:
        return True
    return bool(
        re.fullmatch(
            r"(?:hi|hello|hey|yo|thanks|thank you|good morning|good afternoon|"
            r"good evening|goodbye|bye|how are you|what(?:'s| is) up)[!.? ]*",
            text,
        )
    )


def _memory_learning_eligible(
    task: Any, transcript: list[Any], successful_calls: list[Any], result: Any
) -> bool:
    """Allow memory learning only for explicit facts or observed outcomes."""
    if _is_ephemeral_objective(getattr(task, "objective", "")):
        return False
    objective = str(getattr(task, "objective", "") or "").casefold()
    if re.search(r"\b(remember|i prefer|my preference|i use|i am|i'm)\b", objective):
        return True
    if successful_calls:
        return True
    return bool(
        getattr(result, "evidence", ())
        or getattr(result, "artifacts", ())
        or getattr(result, "mutations", ())
    )


def _workflow_learning_eligible(
    task: Any, transcript: list[Any], successful_calls: list[Any], result: Any
) -> bool:
    """Require process intent, validation evidence, and a nontrivial trace."""
    if len(successful_calls) < 2:
        return False
    objective = str(getattr(task, "objective", "") or "").casefold()
    process_signal = any(
        marker in objective
        for marker in (
            "procedure",
            "recipe",
            "repeatable",
            "reusable",
            "steps",
            "workflow",
            "pipeline",
            "checklist",
        )
    )
    transcript_text = "\n".join(
        str(getattr(message, "conversation_text", lambda: "")() or "")
        for message in transcript
        if hasattr(message, "conversation_text")
    ).casefold()
    validation_signal = any(
        marker in objective or marker in transcript_text
        for marker in ("verify", "validated", "validation", "passed", "test", "check", "replay")
    )
    if not validation_signal:
        for call in successful_calls:
            arguments = getattr(call, "arguments", {}) or {}
            command = str(arguments.get("command") or arguments.get("operation") or "").casefold()
            if any(
                marker in command for marker in ("test", "pytest", "compile", "check", "verify")
            ):
                validation_signal = True
                break
    return process_signal and validation_signal


def _skill_learning_eligible(task: Any, transcript: list[Any], successful_calls: list[Any]) -> bool:
    if not successful_calls:
        return False
    objective = str(getattr(task, "objective", "") or "").casefold()
    text = "\n".join(
        getattr(message, "conversation_text", lambda: "")()
        for message in transcript
        if hasattr(message, "conversation_text")
    ).casefold()
    has_procedure = any(
        marker in objective or marker in text
        for marker in ("procedure", "steps", "repeat", "reusable", "recipe", "checklist")
    )
    has_verification = any(
        marker in objective or marker in text
        for marker in ("verify", "verified", "validated", "passed", "test", "exit_code")
    )
    if not has_verification:
        has_verification = any(
            any(
                marker
                in str(
                    (getattr(call, "arguments", {}) or {}).get("command")
                    or (getattr(call, "arguments", {}) or {}).get("operation")
                    or ""
                ).casefold()
                for marker in ("test", "pytest", "compile", "check", "verify")
            )
            for call in successful_calls
        )
    return has_procedure and has_verification and len(successful_calls) >= 2


def _trace_signature(steps: tuple[Any, ...]) -> str:
    """Stable shape signature retaining operation and argument schemas."""
    import hashlib
    import json

    shape = [
        {
            "capability": getattr(step, "capability_id", None),
            "workflow": getattr(step, "workflow_id", None),
            "arguments": _argument_shape(getattr(step, "arguments", {}) or {}),
        }
        for step in steps
    ]
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _argument_shape(value: Any, *, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {
            str(name): (
                str(item).casefold()
                if str(name).casefold() in {"operation", "action", "method", "type"}
                else _argument_shape(item, key=str(name))
            )
            for name, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_argument_shape(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_argument_shape(item, key=key) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return type(value).__name__
    return "string"
