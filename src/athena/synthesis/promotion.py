"""Registration + promotion mechanism (P1-10 extraction).

Ephemeral registration, executor restoration after restart, and
proof-carrying promotion to SkillCandidate, moved verbatim from
:mod:`athena.synthesis.engine`. Subordinate to SynthesisEngine: this
module holds no authority of its own — promotion gates, proof
validation, and lifecycle decisions stay on the engine.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.affordances.validation import ValidationTier
from athena.protocol.ids import new_id

if TYPE_CHECKING:
    from athena.synthesis.engine import SyntheticCapability
    from athena.synthesis.engine import SynthesisEngine


def _mod():
    # Patch seams / engine-module helpers resolve through the engine module.
    from athena.synthesis import engine

    return engine


_logger = logging.getLogger(__name__)

__all__ = ["Promotion"]


class Promotion:
    """Registration/promotion machinery for generated capabilities.

    Verbatim extraction from ``SynthesisEngine``: engine-owned state
    and helpers resolve through ``self._e`` at call time.
    """

    def __init__(self, engine: SynthesisEngine) -> None:
        self._e = engine

    def register_ephemeral(self, registry, cap: SyntheticCapability) -> bool:
        """Admit a VALIDATED synthetic capability through the normal path."""
        if not cap.validation.get("all_passed"):
            _logger.warning("refusing unvalidated synthetic %s", cap.name)
            return False

        proof_sink = getattr(registry, "update_generated_proof", None)
        candidate_sink = getattr(registry, "persist_generated_candidate", None)
        executor = self._e._build_executor(
            cap, proof_sink=proof_sink, candidate_sink=candidate_sink
        )
        generated = self._e._generated_record(
            cap,
            scope=AffordanceScope.TASK if cap.task_id else AffordanceScope.PROJECT,
        )
        if hasattr(registry, "register_task") and cap.task_id:
            registry.register_task(cap.task_id, executor, generated=generated)
        else:
            registry.register(executor)
        self._e._synthetic[cap.id] = cap
        self._e._executors[cap.id] = executor
        _logger.info("ephemeral capability registered: %s", cap.id)
        return True

    def restore_executor(
        self,
        generated: GeneratedCapability,
        *,
        proof_sink=None,
        workspace_root: str | None = None,
    ):
        """Rehydrate an already validated project/user capability.

        The persisted source is syntax/schema checked again before an executor
        is returned. Runtime execution still goes through the dispatcher and
        policy engine.
        """
        if generated.runtime not in {"python", "python_persistent"}:
            raise ValueError(f"unsupported generated runtime: {generated.runtime}")
        if generated.validation_state not in {"VALIDATED", "PROMOTED"}:
            raise ValueError("generated capability is not validated")
        from athena.capabilities.registry import _compile_validator

        tier = (
            ValidationTier.PROJECT
            if generated.scope is AffordanceScope.PROJECT
            else ValidationTier.USER
            if generated.scope is AffordanceScope.USER
            else ValidationTier.TASK
        )
        source_validation = self._e._source_validator.validate(
            generated.implementation,
            tier=tier,
        )
        if not source_validation.passed:
            failed = "; ".join(
                check.detail for check in source_validation.checks if check.status == "failed"
            )
            raise ValueError(
                f"persisted generated capability failed {tier.value} source checks: "
                f"{failed or 'unknown validation failure'}"
            )
        # The source hash is part of the persisted identity.  A formatter or
        # validator version change must not silently produce a different
        # executable from the same record during restart recovery.
        if source_validation.code != generated.implementation:
            raise ValueError("persisted generated capability is not in canonical source format")
        persisted_authority = frozenset(generated.effective_authority)
        if not persisted_authority.issubset(_mod()._GENERATED_EFFECTIVE_AUTHORITY):
            raise ValueError(
                "persisted generated capability requests authority outside "
                "the generated sandbox profile"
            )
        _compile_validator(generated.input_schema)
        if generated.output_schema is not None:
            _compile_validator(generated.output_schema)
        if generated.required_dependencies and workspace_root:
            self._e._dependency_paths(
                generated.required_dependencies,
                workspace_root,
                expected_fingerprint=(
                    generated.dependency_lock.get("environment_fingerprint")
                    if generated.dependency_lock
                    else None
                ),
            )
        proof = dict(generated.proof_record)
        usage = dict(proof.pop("usage", {}))
        cap = _mod().SyntheticCapability(
            id=generated.id,
            name=generated.name,
            description=generated.description,
            code=generated.implementation,
            input_schema=dict(generated.input_schema),
            output_schema=dict(generated.output_schema or {}) or None,
            effects=frozenset(generated.declared_effects),
            task_id=generated.task_scope,
            provenance=dict(generated.provenance),
            validation=proof,
            runtime=generated.runtime,
            validation_cases=[dict(case) for case in generated.validation_cases],
            required_dependencies=generated.required_dependencies,
            required_capabilities=generated.required_capabilities,
            evidence_dependencies=generated.evidence_dependencies,
            input_signatures=set(str(value) for value in proof.get("input_signatures") or ()),
            task_context_signatures=set(
                str(value) for value in proof.get("task_context_signatures") or ()
            ),
            environment_fingerprints=set(
                str(value) for value in proof.get("environment_fingerprints") or ()
            ),
            reuse_count=int(proof.get("reuse_count") or 0),
            downstream_verifications=int(proof.get("downstream_verifications") or 0),
            latency_saved_ms=float(proof.get("latency_saved_ms") or 0.0),
            turns_saved=int(proof.get("turns_saved") or 0),
            uses=int(usage.get("uses", 0)),
            successes=int(usage.get("successes", generated.success_count)),
            failures=int(usage.get("failures", generated.failure_count)),
            # Stored metadata is checked above but is never the source of
            # runtime authority. Rehydration derives the envelope from the
            # current Athena profile so old records cannot widen execution.
            effective_effects=_mod()._GENERATED_EFFECTIVE_AUTHORITY,
            lifecycle_state=generated.lifecycle_state,
            family_id=generated.family_id,
            revision=generated.revision,
            parent_revision=generated.parent_revision,
            active_revision=generated.active_revision,
            supersedes=generated.supersedes,
            superseded_by=generated.superseded_by,
            dependency_lock=dict(generated.dependency_lock),
            last_used_at=generated.last_used_at,
        )
        cap.uses = int(usage.get("uses", generated.use_count))
        executor = self._e._build_executor(cap, proof_sink=proof_sink)
        self._e._synthetic[cap.id] = cap
        self._e._executors[cap.id] = executor
        return executor

    async def promote(
        self,
        surface,
        cap_id: str,
        *,
        scope: AffordanceScope,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Explicitly promote validated task machinery to a wider overlay.

        Promotion is never implicit.  SYSTEM promotion is intentionally
        rejected: native/system changes belong to the normal release process.
        """
        if scope not in {AffordanceScope.PROJECT, AffordanceScope.USER}:
            raise ValueError("promotion target must be project or user")
        cap = self._e._synthetic.get(cap_id)
        executor = self._e._executors.get(cap_id)
        promotion_tier = (
            ValidationTier.PROJECT if scope is AffordanceScope.PROJECT else ValidationTier.USER
        )
        if cap is None or executor is None:
            _logger.warning(
                "refusing promotion of %s because it is unknown or not executable",
                cap_id,
            )
            return False
        proof_error = _mod()._promotion_proof_error(cap, promotion_tier)
        if proof_error is not None:
            _logger.warning("refusing promotion of %s: %s", cap_id, proof_error)
            return False
        if cap.evidence_dependencies:
            evidence = cap.validation.get("evidence")
            if not isinstance(evidence, Mapping) or evidence.get("status") != "CURRENT":
                _logger.warning(
                    "refusing promotion of %s without current evidence proof",
                    cap_id,
                )
                return False
        # Task admission is intentionally lightweight. Widening lifetime and
        # visibility requires the stricter source gate for the target scope;
        # promotion must never turn a task-only proof into a project/user
        # proof merely by changing metadata.
        if scope is AffordanceScope.PROJECT and not project_id:
            raise ValueError("project promotion requires project_id")
        if scope is AffordanceScope.USER and not user_id:
            raise ValueError("user promotion requires user_id")
        source_validation = self._e._source_validator.validate(cap.code, tier=promotion_tier)
        if not source_validation.passed:
            _logger.warning(
                "refusing promotion of %s after %s source checks",
                cap.id,
                promotion_tier.value,
            )
            return False
        promoted_validation = dict(cap.validation)
        promoted_validation["tier"] = promotion_tier.value
        promoted_validation["source"] = source_validation.to_dict()
        # Keep the task-scoped record untouched until durable activation has
        # committed.  A failed promotion must leave the prior live executor
        # callable and its lifecycle/proof state intact.
        promoted_cap = replace(
            cap,
            code=source_validation.code,
            lifecycle_state="PROMOTED",
            validation=promoted_validation,
        )
        # Formatting is part of the canonical source contract. Rebuild the
        # executor if promotion normalized the source bytes.
        proof_sink = getattr(surface, "update_generated_proof", None)
        executor = self._e._build_executor(promoted_cap, proof_sink=proof_sink)
        source_task_id = cap.task_id
        proof = self._e._proof_record(promoted_cap)
        generated = GeneratedCapability(
            id=promoted_cap.id,
            name=promoted_cap.name,
            description=promoted_cap.description,
            implementation=promoted_cap.code,
            input_schema=promoted_cap.input_schema,
            runtime=promoted_cap.runtime,
            output_schema=promoted_cap.output_schema,
            required_dependencies=promoted_cap.required_dependencies,
            required_capabilities=promoted_cap.required_capabilities,
            evidence_dependencies=promoted_cap.evidence_dependencies,
            declared_effects=frozenset(self._e._effect_values(promoted_cap)),
            effective_authority=frozenset(self._e._authority_values(promoted_cap)),
            scope=scope,
            project_scope=project_id,
            user_scope=user_id if scope is AffordanceScope.USER else None,
            provenance={**promoted_cap.provenance, "promoted_from": "task"},
            validation_state="PROMOTED",
            proof_record=proof,
            lifecycle_state="PROMOTED",
            family_id=promoted_cap.family_id,
            revision=promoted_cap.revision,
            parent_revision=promoted_cap.parent_revision,
            active_revision=promoted_cap.active_revision,
            supersedes=promoted_cap.supersedes,
            superseded_by=promoted_cap.superseded_by,
            dependency_lock=self._e._dependency_lock(promoted_cap),
            use_count=promoted_cap.uses,
            success_count=promoted_cap.successes,
            failure_count=promoted_cap.failures,
            quality_score=float(proof.get("quality_score") or 0.0),
            last_used_at=promoted_cap.last_used_at,
            validation_cases=tuple(dict(case) for case in (promoted_cap.validation_cases or [])),
        )
        if hasattr(surface, "activate_generated_revision"):
            await surface.activate_generated_revision(
                generated,
                executor,
                owner=project_id if scope is AffordanceScope.PROJECT else user_id,
            )
        else:
            if scope is AffordanceScope.PROJECT:
                surface.register_project(project_id, executor, generated=generated)
            else:
                surface.register_user(user_id, executor, generated=generated)
            flush = getattr(surface, "flush", None)
            if flush is not None:
                await flush()
        self._e._synthetic[cap_id] = promoted_cap
        self._e._executors[cap_id] = executor
        # The executor's closure enforced the task owner until the explicit
        # promotion succeeded. Remove the old overlay before widening it.
        if source_task_id and hasattr(surface, "unregister_task_capability"):
            surface.unregister_task_capability(source_task_id, promoted_cap.id)
        return True

    def to_skill_candidate(self, cap_id: str):
        """Convert a repeatedly-successful synthetic into a SkillCandidate."""
        cap = self._e._synthetic.get(cap_id)
        # Repetition with the same arguments is not evidence that a helper is
        # reusable. Require successful behavioral diversity before turning
        # executable proof into a durable knowledge candidate.
        if cap is None or not _mod()._candidate_ready(cap):
            return None
        cap.lifecycle_state = "CANDIDATE"
        from athena.skills.candidates import SkillCandidate
        from athena.skills.models import Skill

        skill = Skill(
            id=new_id("skill"),
            name=cap.name,
            description=cap.description,
            body=f"```python\n{cap.code}\n```",
            version=1,
        )
        return SkillCandidate(
            draft=skill,
            source_task_id=cap.task_id or "",
            target_skill=None,
            rationale="synthesized capability with proven usage history",
            evidence=(
                (
                    f"validated {cap.validation.get('cases_passed')}/"
                    f"{cap.validation.get('cases_total')} sandbox cases"
                ),
                f"{cap.successes}/{cap.uses} live invocations succeeded",
            ),
            confidence=min(0.4 + 0.2 * cap.successes, 0.95),
        )

    def _generated_record(
        self,
        cap: SyntheticCapability,
        *,
        scope: AffordanceScope,
        project_scope: str | None = None,
        user_scope: str | None = None,
    ) -> GeneratedCapability:
        proof = self._e._proof_record(cap)
        return GeneratedCapability(
            id=cap.id,
            name=cap.name,
            description=cap.description,
            implementation=cap.code,
            runtime=cap.runtime,
            input_schema=cap.input_schema,
            output_schema=cap.output_schema,
            declared_effects=frozenset(self._e._effect_values(cap)),
            effective_authority=frozenset(self._e._authority_values(cap)),
            required_dependencies=cap.required_dependencies,
            required_capabilities=cap.required_capabilities,
            evidence_dependencies=cap.evidence_dependencies,
            scope=scope,
            task_scope=(
                cap.task_id if scope in {AffordanceScope.TASK, AffordanceScope.CANDIDATE} else None
            ),
            project_scope=project_scope,
            user_scope=user_scope,
            provenance=cap.provenance,
            validation_state="VALIDATED",
            proof_record=proof,
            lifecycle_state=cap.lifecycle_state,
            family_id=cap.family_id,
            revision=cap.revision,
            parent_revision=cap.parent_revision,
            active_revision=cap.active_revision,
            supersedes=cap.supersedes,
            superseded_by=cap.superseded_by,
            dependency_lock=self._e._dependency_lock(cap),
            validation_cases=tuple(dict(case) for case in (cap.validation_cases or [])),
            last_used_at=cap.last_used_at,
            use_count=cap.uses,
            success_count=cap.successes,
            failure_count=cap.failures,
            quality_score=float(proof.get("quality_score") or 0.0),
        )

    @staticmethod
    def _environment_signature(context, cap: SyntheticCapability) -> str:
        """Record the actual execution environment used by live proof."""
        workspace = getattr(context, "workspace", None) if context else None
        if workspace is not None:
            try:
                from athena.execution.environment import ProjectEnvironmentFingerprint

                return ProjectEnvironmentFingerprint().fingerprint(
                    workspace,
                    extras={
                        "generated_dependency_fingerprint": cap.dependency_lock.get("fingerprint"),
                    },
                    # Generated capabilities currently execute as Python.
                    # Keep live proof collection bounded while still recording
                    # the real runtime identity; dependency resolution already
                    # verifies any declared package environment separately.
                    toolchain_names=("python", "python3"),
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # Proof remains attributable even when a read-only toolchain
                # probe is unavailable; the fallback is deliberately limited
                # to execution policy identity, never caller metadata.
                pass
        return _mod()._input_signature(
            {
                "workspace": getattr(workspace, "id", None),
                "backend": getattr(workspace, "execution_backend", None),
                "network": str(
                    getattr(
                        getattr(workspace, "network_policy", None),
                        "value",
                        None,
                    )
                ),
                "dependency": cap.dependency_lock.get("fingerprint"),
            }
        )
