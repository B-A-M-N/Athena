"""The single candidate proof-planning and execution authority.

Both ordinary speculative completion and Fusion experiments use this service
to derive criteria, execute them through the canonical candidate verifier,
and retain one verification plan per task. There is deliberately no second
definition of "this code candidate is verified".
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from athena.protocol.reality import CandidateProofPlan
from athena.protocol.tasks import (
    Criterion,
    TaskSpec,
    VerificationSpec,
    VerificationType,
    WorkClass,
    WorkspaceSpec,
)
from athena.verification import (
    VerificationPlanner,
    proof_subsumes,
    verification_proof_id,
)

__all__ = ["CandidateVerificationService"]

_logger = logging.getLogger("athena.reality.candidate_verification")


class CandidateVerificationService:
    """Derive, execute, and retain proof obligations for one task candidate."""

    def __init__(
        self,
        *,
        candidate_verifier: Any,
        default_criteria_source: Any = None,
        verification_planner: VerificationPlanner | None = None,
        project_index_provider: Any = None,
    ) -> None:
        self._candidate_verifier = candidate_verifier
        self._default_source = default_criteria_source
        self.planner = verification_planner or VerificationPlanner()
        self._project_index_provider = project_index_provider
        self._plans: dict[str, dict[str, Any]] = {}
        self._strength_results: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _requires_independent_proof(task: TaskSpec) -> bool:
        plan = task.execution_plan
        if plan is not None:
            return bool(plan.work_class is WorkClass.COMPLEX_CODING)
        metadata = task.metadata or {}
        return bool(metadata.get("_athena_work_class") == WorkClass.COMPLEX_CODING.value)

    @property
    def plans(self) -> dict[str, dict[str, Any]]:
        return self._plans

    def plan_for(self, task_id: str) -> dict[str, Any] | None:
        """Return the retained plan used to issue the candidate certificate."""
        return self._plans.get(task_id)

    async def criteria_for(
        self,
        task: TaskSpec,
        *,
        workspace: WorkspaceSpec | None = None,
        changed_resources: tuple[str, ...] = (),
        impact: Mapping[str, Any] | None = None,
        profile_workspace: WorkspaceSpec | None = None,
    ) -> tuple[Criterion, ...]:
        """Derive one linear proof plan for explicit plus project criteria.

        Complex coding without independently derived baseline evidence returns
        a typed planning failure instead of a synthetic shell command.  The
        failure never reaches the verifier.
        """
        explicit = tuple(c for c in task.acceptance_criteria if c.required)
        metadata = task.metadata or {}
        if bool(metadata.get("_athena_self_host")):
            from athena.self_host.gates import SelfHostGatePolicy

            explicit = SelfHostGatePolicy.select_criteria(
                explicit,
                changed_resources=changed_resources,
                impact=impact,
                task_id=task.id,
            )
            if SelfHostGatePolicy.requires_dependency_proof(changed_resources):
                dependency_command = SelfHostGatePolicy.dependency_environment_command(task.id)
                if not any(
                    getattr(c.verification, "command", None) == dependency_command for c in explicit
                ):
                    explicit += (
                        Criterion(
                            id="self_host_dependency_environment_proof",
                            description=(
                                "self-host dependency proof: isolated offline sync and Python gates "
                                f"{dependency_command}"
                            ),
                            verification=VerificationSpec(
                                type=VerificationType.COMMAND,
                                command=dependency_command,
                            ),
                            required=True,
                        ),
                    )
        baseline = tuple(
            await self._derive_default_criteria(
                task,
                workspace=profile_workspace if profile_workspace is not None else workspace,
                changed_resources=changed_resources,
                impact=impact,
            )
        )
        if explicit and self._requires_independent_proof(task) and not baseline:
            self._plans[task.id] = {
                **dict(self._plans.get(task.id) or {}),
                "plan_id": "verification_unavailable",
                "planning_errors": (
                    "complex coding requires independently derived project "
                    "verification criteria, but none could be obtained; "
                    "explicit criteria alone are insufficient"
                ),
                "required_strength": "strong",
                "explicit_criteria": [criterion.id for criterion in explicit],
            }
            return ()

        criteria = _deduplicate_criteria((*explicit, *baseline))
        plan = dict(self._plans.get(task.id) or {})
        plan.update(
            {
                "plan_id": plan.get("plan_id") or f"explicit:{task.id}",
                "impacted_tests": plan.get("impacted_tests") or list(_impact_tests(impact)),
                "invariants": plan.get("invariants")
                or list((task.metadata or {}).get("invariants") or ()),
                "required_strength": plan.get("required_strength") or "standard",
                "evidence_categories": plan.get("evidence_categories") or (),
                "criterion_categories": plan.get("criterion_categories") or {},
                "rationale": list(plan.get("rationale") or ())
                + ["task supplied explicit acceptance criteria; baseline retained"],
                "index_revision": plan.get("index_revision")
                or (impact or {}).get("index_revision"),
                "explicit_criteria": [criterion.id for criterion in explicit],
            }
        )
        self._plans[task.id] = plan
        if bool(metadata.get("_athena_self_host")):
            from athena.self_host.gates import SelfHostGatePolicy

            criteria = _deduplicate_criteria(
                SelfHostGatePolicy.select_criteria(
                    criteria,
                    changed_resources=changed_resources,
                    impact=impact,
                    task_id=task.id,
                )
            )
        return criteria

    async def proof_plan(
        self,
        task: TaskSpec,
        *,
        workspace: WorkspaceSpec | None = None,
        changed_resources: tuple[str, ...] = (),
        impact: Mapping[str, Any] | None = None,
        profile_workspace: WorkspaceSpec | None = None,
    ) -> CandidateProofPlan:
        """Return the canonical typed result of proof planning."""
        criteria = await self.criteria_for(
            task,
            workspace=workspace,
            changed_resources=changed_resources,
            impact=impact,
            profile_workspace=profile_workspace,
        )
        plan = self._plans.get(task.id) or {}
        required_strength = str(plan.get("required_strength") or "standard")
        errors = tuple(str(item) for item in (plan.get("planning_errors") or ()))
        if errors:
            return CandidateProofPlan((), required_strength, errors)
        if self._requires_independent_proof(task) and not criteria:
            return CandidateProofPlan(
                (),
                required_strength,
                ("complex coding requires independent project-derived proof",),
            )
        return CandidateProofPlan(tuple(criteria), required_strength, ())

    async def verify_candidate(
        self,
        task: TaskSpec,
        *,
        workspace: WorkspaceSpec,
        changed_resources: tuple[str, ...] = (),
        impact: Mapping[str, Any] | None = None,
        profile_workspace: WorkspaceSpec | None = None,
        supplemental_criteria: tuple[Criterion, ...] = (),
        deactivate_branch: Any = None,
        task_id: str | None = None,
    ) -> list[dict]:
        """Derive and execute the canonical candidate proof at a public seam.

        Fusion and RealityCoordinator share this method; neither needs to reach
        through coordinator internals. Optional semantic criteria strengthen,
        but never replace, project-derived proof.
        """
        if callable(deactivate_branch) and task_id:
            await deactivate_branch(task_id)
        explicit = tuple(c for c in task.acceptance_criteria if c.required)
        if not explicit and supplemental_criteria:
            # A model supplied probe is supplemental evidence only. It can
            # strengthen a project-derived plan; it cannot manufacture the
            # sole criterion that lets an otherwise criterion-less candidate
            # certify itself.
            supplemental_criteria = ()
        criteria = await self.criteria_for(
            replace(task, acceptance_criteria=explicit + tuple(supplemental_criteria)),
            workspace=workspace,
            changed_resources=changed_resources,
            impact=impact,
            profile_workspace=profile_workspace,
        )
        return await self.verify_or_fail(task, criteria, workspace)

    async def verify_or_fail(
        self,
        task: TaskSpec,
        criteria: tuple[Criterion, ...],
        workspace: WorkspaceSpec,
    ) -> list[dict]:
        if not criteria:
            if self._requires_independent_proof(task):
                # Complex coding work cannot cross into reality on prose or
                # the model's claim of done. An inability to derive or execute
                # independent proof is verification unavailable, not success.
                # Late runtime escalation must inherit the same proof floor:
                # an admission-time simple task that later became runtime
                # speculative cannot complete on a weaker contract.
                plan = getattr(task, "execution_plan", None)
                floor = getattr(plan, "verification_floor", None)
                return [
                    {
                        "id": "verification_unavailable",
                        "passed": False,
                        "reason": (
                            "complex coding requires independent executable proof; "
                            f"verification floor is {getattr(floor, 'value', 'standard')}"
                        ),
                    }
                ]
            # Trivial work still needs the observable-work evidence already
            # required at the turn-boundary gate. A criteria-less workspace
            # is not itself a failed check.
            return [{"id": "no_criteria_derivable", "passed": True, "obligation": "none"}]
        try:
            results = await self._candidate_verifier.verify_against(task, criteria, workspace)
        except Exception as exc:  # noqa: BLE001 - never accept unverified work
            _logger.warning("candidate verification failed: %s", exc)
            return [{"id": c.id, "passed": False} for c in criteria]
        normalized = list(results or ())
        # Some verifier protocols return bare booleans; normalise once here so
        # downstream strength checks always see the canonical dict shape.
        if normalized and isinstance(normalized[0], bool):
            normalized = [
                {"id": criterion.id, "passed": bool(ok)}
                for criterion, ok in zip(criteria, normalized)
            ]
        if len(normalized) != len(criteria):
            return [{"id": c.id, "passed": False} for c in criteria]

        plan = self._plans.get(task.id) or {}
        required_strength = str(plan.get("required_strength") or "standard")
        criterion_categories = dict(plan.get("criterion_categories") or {})
        passed_categories = {
            criterion_categories.get(str(item.get("id")))
            for item in normalized
            if item.get("passed")
        }
        passed_categories.discard(None)
        planned_categories = set(criterion_categories.values())
        all_explicit_passed = all(item.get("passed") for item in normalized)
        has_independent = bool(passed_categories)
        if required_strength == "standard":
            achieved = "standard" if (all_explicit_passed and has_independent) else "none"
        elif required_strength == "strong":
            if not all_explicit_passed or not has_independent:
                achieved = "none"
            elif len(passed_categories) >= 2:
                achieved = "strong"
            else:
                # Strong requires every planned category and >=2 passing.
                achieved = "standard"
                if planned_categories != passed_categories:
                    normalized.append(
                        {
                            "id": "required_strength_unsatisfied",
                            "passed": False,
                            "reason": (
                                f"strong proof requires evidence from every planned "
                                f"category {sorted(str(item) for item in planned_categories)}; "
                                f"passing categories were "
                                f"{sorted(str(item) for item in passed_categories)}"
                            ),
                        }
                    )
        else:
            achieved = "none"
        self._strength_results[task.id] = {
            "requested_strength": required_strength,
            "achieved_strength": achieved,
            "planned_categories": sorted(planned_categories),
            "passed_categories": sorted(passed_categories),
            "unavailable_categories": sorted(planned_categories - passed_categories),
        }
        if required_strength == "strong" and achieved != "strong":
            normalized.append(
                {
                    "id": "verification_strength_unavailable",
                    "passed": False,
                    "reason": (
                        f"requested strong proof could not be assembled (achieved: {achieved})"
                    ),
                }
            )
        return normalized

    def strength_for(self, task_id: str) -> dict[str, Any] | None:
        """Return the requested-vs-achieved strength result for one task."""
        return self._strength_results.get(task_id)

    async def _derive_default_criteria(
        self,
        task: TaskSpec,
        *,
        workspace: WorkspaceSpec | None = None,
        changed_resources: tuple[str, ...] = (),
        impact: Mapping[str, Any] | None = None,
    ) -> list[Criterion]:
        """Derive a bounded verification plan when the user set no criteria.

        Uses the project profile's configured commands so "the model said it
        looks done" is never the only proof that lets a candidate cross into
        reality.
        """
        if self._default_source is None:
            return []
        try:
            profile_task = replace(task, workspace=workspace) if workspace is not None else task
            profile = self._default_source(profile_task)
            if hasattr(profile, "__await__"):
                profile = await profile
        except Exception as exc:  # noqa: BLE001
            _logger.warning("default criteria derivation failed: %s", exc)
            return []
        if not profile:
            return []
        plan = self.planner.plan(
            task,
            profile,
            changed_resources=changed_resources,
            impact=impact,
            invariants=tuple((task.metadata or {}).get("invariants") or ()),
        )
        self._plans[task.id] = {
            "plan_id": plan.plan_id,
            "impacted_resources": list(plan.impacted_resources),
            "impacted_tests": list(plan.impacted_tests),
            "invariants": list(plan.invariants),
            "evidence_categories": list(plan.evidence_categories),
            "criterion_categories": dict(plan.criterion_categories or {}),
            "required_strength": plan.required_strength,
            "rationale": list(plan.rationale),
            "index_revision": plan.index_revision,
        }
        if plan.skipped_commands:
            _logger.info(
                "verification planner skipped %d unusable project probes",
                len(plan.skipped_commands),
            )
        return list(plan.criteria)

    async def record_reliability(
        self,
        task_id: str,
        *,
        event_sink: Any = None,
        **facts: Any,
    ) -> dict[str, Any]:
        """Record task-level reliability telemetry (review item 31).

        Awaits the event sink before returning so certification telemetry is
        durable-observable, not a naked fire-and-forget future.
        """
        record = {"task_id": task_id, **facts}
        if event_sink is not None:
            try:
                from athena.protocol.events import make_event

                event = make_event(
                    "ReliabilityTelemetry",
                    record,
                    task_id=task_id,
                )
                await event_sink(event)
            except Exception:  # noqa: BLE001 - telemetry must never crash callers
                _logger.debug("reliability telemetry emission failed for %s", task_id)
        return record

    async def impact_for(
        self,
        root: str,
        changed_resources: tuple[str, ...],
    ) -> dict[str, Any]:
        """Use the persisted project-index graph when deriving completion proof."""
        if not root or not changed_resources or self._project_index_provider is None:
            return {}
        try:
            index = self._project_index_provider(root)
            if hasattr(index, "__await__"):
                index = await index
            impact = index.impact(list(changed_resources))
            return dict(impact) if isinstance(impact, Mapping) else {}
        except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
            _logger.warning("project index impact lookup failed: %s", exc)
            return {}


def _impact_tests(impact: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Extract concrete test resources from a project-index impact record."""
    if not isinstance(impact, Mapping):
        return ()
    values = impact.get("affected_tests") or ()
    if not isinstance(values, (list, tuple, set)):
        return ()
    return tuple(sorted({str(value) for value in values if str(value).strip()}))


def _deduplicate_criteria(criteria: tuple[Criterion, ...]) -> tuple[Criterion, ...]:
    """Keep one proof for equivalent checks, preferring stronger proofs."""
    output: list[Criterion] = []
    for criterion in criteria:
        verification = criterion.verification
        proof_id = (
            verification_proof_id(verification)
            if verification is not None
            else f"criterion:{criterion.id}"
        )
        existing_ids = [
            verification_proof_id(existing.verification)
            if existing.verification is not None
            else f"criterion:{existing.id}"
            for existing in output
        ]
        if any(proof_subsumes(existing_id, proof_id) for existing_id in existing_ids):
            continue
        output = [
            existing
            for existing, existing_id in zip(output, existing_ids)
            if not proof_subsumes(proof_id, existing_id)
        ]
        output.append(criterion)
    return tuple(output)
