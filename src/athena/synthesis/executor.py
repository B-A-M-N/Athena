"""Validated generated-capability executor.

The executor owns generated invocation mechanics and failure/proof projection.
The synthesis engine remains the authority for validation, dependency
resolution, child-runtime execution, and durable generated records.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from athena.affordances.models import AffordanceScope
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.schema import validate_schema
from athena.synthesis.models import SyntheticCapability

_logger = logging.getLogger("athena.synthesis.runtime")


class GeneratedExecutor:
    """Execute one already-validated generated capability record."""

    # Generated code can re-enter the canonical dispatcher through its framed
    # host API.  The child operation, not this envelope, must own concrete
    # resource ordering while that nested dispatch is awaited.
    mediates_nested_dispatch = True

    def __init__(
        self,
        engine: Any,
        cap: SyntheticCapability,
        *,
        child: str,
        runtime_effects: frozenset[str],
        proof_sink: Any = None,
        candidate_sink: Any = None,
    ) -> None:
        self.engine = engine
        self.cap = cap
        self.child = child
        self.proof_sink = proof_sink
        self.candidate_sink = candidate_sink
        self.descriptor = CapabilityDescriptor(
            id=cap.id,
            description=f"[synthetic] {cap.description} "
            f"(validated {cap.validation.get('cases_passed', 0)}/"
            f"{cap.validation.get('cases_total', 0)})",
            input_schema=cap.input_schema,
            output_schema=cap.output_schema,
            # The executable is sandboxed with this effective authority; the
            # generated declaration remains audit metadata only.
            effects=frozenset(EffectClass(effect) for effect in runtime_effects),
            # Generated calls have no operation argument from which the legacy
            # dispatcher heuristic could recover their complete envelope.
            effect_resolver=lambda _arguments: frozenset(
                EffectClass(effect) for effect in runtime_effects
            ),
            origin=CapabilityOrigin.PROJECT if cap.task_id is None else CapabilityOrigin.GENERATED,
            version="0",
        )

    async def invoke(self, request, output_accumulator=None, context=None):
        admission_result = await self._admission_result(request)
        if admission_result is not None:
            return admission_result
        try:
            stdout, stderr, returncode = await self._run(request, context)
        except (OSError, RuntimeError, ValueError) as exc:
            return await self._environment_failure(request, context, exc)
        environment_signature, repeated_input = self._record_usage(request, context)
        if returncode == 0 and "__RESULT__" in stdout:
            return await self._successful_result(
                request,
                stdout,
                environment_signature,
                repeated_input,
            )
        return await self._failed_result(
            request,
            stderr,
            environment_signature,
        )

    async def _admission_result(self, request):
        from athena.synthesis.engine import (
            _UNUSABLE_LIFECYCLE_STATES,
            _admit_generated_input,
            _generated_failure,
        )

        cap = self.cap
        input_errors = _admit_generated_input(cap, request.arguments)
        if input_errors:
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error="generated input rejected at admission: " + "; ".join(input_errors),
                metadata={
                    "admission_rejected": True,
                    "admission_boundary": "generated_input_schema",
                },
            )
        if cap.lifecycle_state in _UNUSABLE_LIFECYCLE_STATES:
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=(
                    f"synthetic capability {cap.id} requires revalidation "
                    f"({cap.lifecycle_state.lower()})"
                ),
                metadata={
                    "generated_failure": _generated_failure(
                        cap,
                        "lifecycle_unavailable",
                        repairable=False,
                    )
                },
            )
        if cap.task_id and request.task_id != cap.task_id:
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=f"synthetic capability {cap.id} is scoped to task {cap.task_id}",
            )
        if not cap.evidence_dependencies:
            return None
        evidence = await self.engine.evidence_status(cap, self.engine._research_store)
        if evidence["status"] == "CURRENT":
            return None
        cap.lifecycle_state = "REVALIDATION_REQUIRED"
        cap.validation["evidence"] = evidence
        if self.proof_sink is not None:
            try:
                await self.proof_sink(cap.id, self.engine._proof_record(cap))
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                _logger.warning("could not persist stale evidence status for %s", cap.id)
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.FAILED,
            error="generated capability evidence is stale",
            metadata={
                "evidence": evidence,
                "generated_failure": _generated_failure(
                    cap,
                    "provenance_stale",
                    repairable=False,
                    evidence=evidence,
                ),
            },
        )

    async def _run(self, request, context):
        payload = json.dumps(dict(request.arguments or {}))
        workspace_root = context.workspace.root if context is not None else None
        dependency_paths = self.engine._dependency_paths(
            self.cap.required_dependencies,
            workspace_root,
            expected_fingerprint=(
                self.cap.dependency_lock.get("environment_fingerprint")
                if self.cap.dependency_lock
                else None
            ),
        )
        return await self.engine._run_generated_child(
            cap=self.cap,
            child=self.child,
            payload=payload,
            timeout=30,
            workspace_root=workspace_root,
            effects=self.engine._authority_values(self.cap),
            python_paths=dependency_paths,
            context=context,
            request=request,
        )

    def _record_usage(self, request, context) -> tuple[str, bool]:
        from athena.synthesis.engine import _input_signature
        from athena.protocol.messages import utcnow

        cap = self.cap
        cap.uses += 1
        input_signature = _input_signature(request.arguments)
        repeated_input = input_signature in cap.input_signatures
        cap.input_signatures.add(input_signature)
        cap.task_context_signatures.add(
            _input_signature(
                {
                    "task_id": request.task_id,
                    "session_id": getattr(request, "session_id", None),
                }
            )
        )
        environment_signature = self.engine._environment_signature(context, cap)
        cap.environment_fingerprints.add(environment_signature)
        cap.last_used_at = utcnow().isoformat()
        return environment_signature, repeated_input

    async def _environment_failure(self, request, context, exc):
        from athena.synthesis.engine import _failure_lifecycle_state, _generated_failure
        from athena.synthesis.engine import _remember_live_failure
        from athena.protocol.messages import utcnow

        cap = self.cap
        failure_class = "environment_changed"
        cap.failures += 1
        cap.lifecycle_state = _failure_lifecycle_state(failure_class)
        environment_signature = self.engine._environment_signature(context, cap)
        cap.environment_fingerprints.add(environment_signature)
        cap.last_used_at = utcnow().isoformat()
        _remember_live_failure(
            cap,
            request.arguments,
            failure_class=failure_class,
            observed_failure=str(exc),
            environment_fingerprint=environment_signature,
        )
        proof_error = None
        if self.proof_sink is not None:
            try:
                await self.proof_sink(cap.id, self.engine._proof_record(cap))
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as proof_exc:
                _logger.error(
                    "generated capability proof persistence failed for %s: %s",
                    cap.id,
                    proof_exc,
                )
                proof_error = str(proof_exc)
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.FAILED,
            error=f"generated execution environment unavailable: {exc}",
            metadata={
                **({"proof_persistence_error": proof_error} if proof_error else {}),
                "generated_failure": _generated_failure(
                    cap,
                    failure_class,
                    repairable=False,
                ),
            },
        )

    async def _persist_proof(self) -> str | None:
        from athena.synthesis.engine import _candidate_ready

        cap = self.cap
        errors: list[str] = []
        if self.proof_sink is not None:
            try:
                await self.proof_sink(cap.id, self.engine._proof_record(cap))
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.error(
                    "generated capability proof persistence failed for %s: %s",
                    cap.id,
                    exc,
                )
                errors.append(str(exc))
        if self.candidate_sink is not None and cap.task_id and _candidate_ready(cap):
            cap.lifecycle_state = "CANDIDATE"
            try:
                await self.candidate_sink(
                    self.engine._generated_record(
                        cap,
                        scope=AffordanceScope.CANDIDATE,
                    )
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.error(
                    "generated candidate persistence failed for %s",
                    cap.id,
                )
                errors.append(str(exc))
        return "; ".join(errors) or None

    async def _successful_result(
        self,
        request,
        stdout,
        environment_signature,
        repeated_input,
    ):
        from athena.synthesis.engine import (
            _failure_lifecycle_state,
            _generated_failure,
            _remember_live_failure,
        )

        cap = self.cap
        try:
            value = json.loads(stdout.split("__RESULT__", 1)[1].splitlines()[0])
        except (IndexError, json.JSONDecodeError) as exc:
            cap.failures += 1
            cap.lifecycle_state = _failure_lifecycle_state("implementation_failure")
            _remember_live_failure(
                cap,
                request.arguments,
                failure_class="implementation_failure",
                observed_failure=str(exc),
                environment_fingerprint=environment_signature,
            )
            proof_error = await self._persist_proof()
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.FAILED,
                error=f"synthetic returned invalid JSON: {exc}",
                metadata={
                    **({"proof_persistence_error": proof_error} if proof_error else {}),
                    "generated_failure": _generated_failure(
                        cap,
                        "implementation_failure",
                        repairable=True,
                    ),
                },
            )
        if cap.output_schema is not None:
            errors = validate_schema(cap.output_schema, value)
            if errors:
                cap.failures += 1
                cap.lifecycle_state = _failure_lifecycle_state("contract_mismatch")
                _remember_live_failure(
                    cap,
                    request.arguments,
                    failure_class="contract_mismatch",
                    observed_failure="; ".join(errors),
                    environment_fingerprint=environment_signature,
                )
                proof_error = await self._persist_proof()
                return CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="generated output validation failed: " + "; ".join(errors),
                    metadata={
                        **({"proof_persistence_error": proof_error} if proof_error else {}),
                        "generated_failure": _generated_failure(
                            cap,
                            "contract_mismatch",
                            repairable=True,
                        ),
                    },
                )
        cap.successes += 1
        if repeated_input:
            cap.reuse_count += 1
        proof_error = await self._persist_proof()
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output=json.dumps(value),
            metadata=({"proof_persistence_error": proof_error} if proof_error else {}),
        )

    async def _failed_result(self, request, stderr, environment_signature):
        from athena.synthesis.engine import (
            _failure_lifecycle_state,
            _generated_failure,
            _remember_live_failure,
        )

        cap = self.cap
        cap.failures += 1
        structured_failure: dict[str, Any] = {}
        marker = "__ATHENA_FAILURE__"
        if marker in (stderr or ""):
            raw = str(stderr).split(marker, 1)[1].splitlines()[0]
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    structured_failure = parsed
            except json.JSONDecodeError:
                structured_failure = {}
        failure_class = str(
            structured_failure.get("failure_class")
            or (
                "governance_failure"
                if "host call" in (stderr or "").lower()
                else "implementation_failure"
            )
        )
        recovery_action = str(
            structured_failure.get("recovery_action")
            or (
                "request_authority_or_fail"
                if failure_class == "governance_failure"
                else "source_repair"
            )
        )
        cap.lifecycle_state = _failure_lifecycle_state(failure_class)
        if failure_class == "implementation_failure":
            _remember_live_failure(
                cap,
                request.arguments,
                failure_class=failure_class,
                observed_failure=(stderr or "synthetic failed")[-500:],
                environment_fingerprint=environment_signature,
            )
        proof_error = await self._persist_proof()
        generated_failure = _generated_failure(
            cap,
            failure_class,
            repairable=failure_class in {"implementation_failure", "contract_mismatch"},
            evidence={
                **structured_failure,
                "recovery_action": recovery_action,
            },
        )
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.FAILED,
            error=(stderr or "synthetic failed")[-500:],
            metadata={
                **({"proof_persistence_error": proof_error} if proof_error else {}),
                "generated_failure": generated_failure,
                "diagnostic": generated_failure.get("diagnostic", {}),
            },
        )
