"""Typed validation context and explicit generated-validation phases.

The :class:`ValidationContext` carries already-normalized inputs between
phases.  Each phase performs one validation responsibility and returns either
updated context or a rejected capability.  ``Validator`` remains the engine
adapter and coordinator; these classes do not own admission or promotion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from athena.affordances.validation import ValidationTier
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.synthesis.helpers import (
    _admit_generated_input,
    _schema_for_values,
    _service_negative_cases,
    _verification_evidence,
)
from athena.synthesis.sandbox_runner import ValidationSandboxRunner
from athena.synthesis.verifier import GeneratedCapabilityVerifier
from athena.synthesis.proof_corpus import corpus_digest, derive_proof_cases
from athena.synthesis.helpers import _risk_tier, canonical_generated_identity

if TYPE_CHECKING:
    from athena.protocol.tasks import WorkspaceSpec
    from athena.synthesis.models import SyntheticCapability


@dataclass(frozen=True)
class ValidationContext:
    """Canonical inputs and mutable state shared by validation phases."""

    engine: Any
    cap: SyntheticCapability
    cases: tuple[dict, ...]
    tier: ValidationTier
    timeout: float = 15.0
    workspace_root: str | None = None
    workspace: WorkspaceSpec | None = None
    task_id: str | None = None
    session_id: str | None = None
    profile: str | None = None
    task_policy: Any = None
    task_budget: Any = None
    generated_call_depth: int = 0
    generated_call_chain: tuple[str, ...] = ()
    historical_failures: tuple[dict, ...] = ()
    historical_by_id: dict[str, dict] | None = None
    source_record: dict | None = None
    negative_details: tuple[dict, ...] = ()


def reject(context: ValidationContext, case_name: str, error: str) -> SyntheticCapability:
    """Apply a fail-closed validation rejection with retained history."""
    cap = context.cap
    cap.lifecycle_state = "REJECTED"
    cap.validation = {
        "tier": context.tier.value,
        "cases_total": len(context.cases),
        "cases_passed": 0,
        "all_passed": False,
        "source": context.source_record or {},
        "details": [{"case": case_name, "passed": False, "error": error}],
        "live_failure_cases": list(context.historical_failures),
        "regression_cases": list(context.historical_failures),
    }
    return cap


class StaticValidationPhase:
    """Normalize source and compile contracts before trial execution."""

    def __init__(self, context: ValidationContext) -> None:
        self.context = context

    def run(self) -> ValidationContext | SyntheticCapability:
        engine = self.context.engine
        result = engine._source_validator.validate(self.context.cap.code, tier=self.context.tier)
        self.context.cap.code = result.code
        source_record = result.to_dict()
        if not result.passed:
            failed = "; ".join(check.detail for check in result.checks if check.status == "failed")
            updated = replace(self.context, source_record=source_record)
            return reject(
                updated,
                "source",
                failed or "generated source failed static validation",
            )
        from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

        from athena.schema import compile_validator

        try:
            compile_validator(self.context.cap.input_schema)
            if self.context.cap.output_schema is not None:
                compile_validator(self.context.cap.output_schema)
        except (SchemaError, SyntaxError, TypeError, ValueError) as exc:
            updated = replace(self.context, source_record=source_record)
            return reject(updated, "static", f"static validation: {exc}")
        return replace(self.context, source_record=source_record)


class DependencyValidationPhase:
    """Resolve native and filesystem dependencies before fixture execution."""

    def __init__(self, context: ValidationContext) -> None:
        self.context = context

    async def run(self) -> ValidationContext | SyntheticCapability:
        cap = self.context.cap
        engine = self.context.engine
        registry = getattr(engine._dispatcher, "registry", None) if engine._dispatcher else None
        unavailable: list[str] = []
        if cap.required_capabilities:
            if registry is None:
                unavailable.append("dispatcher registry is unavailable")
            else:
                for capability_id in cap.required_capabilities:
                    try:
                        descriptor = registry.resolve(capability_id)
                    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                        unavailable.append(f"{capability_id}: {exc}")
                        continue
                    availability = getattr(descriptor, "availability", None)
                    if (
                        availability is not None
                        and getattr(availability, "value", "") != "available"
                    ):
                        unavailable.append(f"{capability_id}: {getattr(availability, 'value', '')}")
        if unavailable:
            return reject(
                self.context,
                "required_capabilities",
                "; ".join(unavailable),
            )
        try:
            metadata = engine._dependency_metadata(
                cap.required_dependencies,
                self.context.workspace.root
                if self.context.workspace is not None
                else self.context.workspace_root,
            )
        except ValueError as exc:
            return reject(self.context, "dependencies", str(exc))
        cap.dependency_lock = {**dict(cap.dependency_lock or {}), **metadata}
        return replace(self.context, negative_details=tuple(await self._negative_corpus()))

    async def _negative_corpus(self) -> list[dict[str, object]]:
        context = self.context
        cap = context.cap
        details: list[dict[str, object]] = []
        negative_executor = context.engine._build_executor(cap)
        for index, negative_case in enumerate(_service_negative_cases(cap.input_schema)):
            invalid_input = negative_case.get("input")
            errors = _admit_generated_input(cap, invalid_input)
            probe = await negative_executor.invoke(
                CapabilityRequest(
                    capability_id=cap.id,
                    arguments=cast("dict[str, Any]", invalid_input),
                    task_id=context.task_id,
                    call_id=f"synthesis-negative-{cap.id}-{index}",
                )
            )
            metadata = dict(probe.metadata or {})
            admitted_rejection = (
                probe.status is CapabilityResultStatus.FAILED
                and metadata.get("admission_rejected") is True
            )
            details.append(
                {
                    "source": "service_negative",
                    "mutator": negative_case.get("mutator", "wrong_root_type"),
                    "field": negative_case.get("field"),
                    "passed": bool(errors) and admitted_rejection,
                    "admission_boundary": metadata.get(
                        "admission_boundary", "generated_input_schema"
                    ),
                    "executor_invoked": not admitted_rejection,
                    "errors": errors,
                }
            )
        return details


class SandboxValidationPhase:
    """Execute authored fixtures in the canonical validation sandbox."""

    def __init__(self, context: ValidationContext) -> None:
        self.context = context

    async def run(self) -> tuple[ValidationContext, Any] | SyntheticCapability:
        context = self.context
        runner = ValidationSandboxRunner(
            context.engine,
            cap=context.cap,
            cases=list(context.cases),
            historical_by_id=dict(context.historical_by_id or {}),
            tier=context.tier,
            workspace_root=context.workspace_root,
            workspace=context.workspace,
            task_id=context.task_id,
            session_id=context.session_id,
            profile=context.profile,
            task_policy=context.task_policy,
            task_budget=context.task_budget,
            generated_call_depth=context.generated_call_depth,
            generated_call_chain=context.generated_call_chain,
            timeout=context.timeout,
        )
        result = await runner.run()
        if result.details and result.details[0].get("case") == "workspace":
            cap = context.cap
            cap.lifecycle_state = "REJECTED"
            cap.validation = {
                "tier": context.tier.value,
                "cases_total": len(context.cases),
                "cases_passed": 0,
                "all_passed": False,
                "source": context.source_record or {},
                "details": result.details,
                "live_failure_cases": list(context.historical_failures),
                "regression_cases": list(context.historical_failures),
            }
            return cap
        return replace(context), result


class EvidenceValidationPhase:
    """Assemble behavioral, derived, and independent proof evidence."""

    def __init__(self, context: ValidationContext, sandbox: Any) -> None:
        self.context = context
        self.sandbox = sandbox

    async def run(self) -> SyntheticCapability:
        context = self.context
        cap = context.cap
        details = self.sandbox.details
        if self.sandbox.hosts:
            observed = {
                str(call.get("capability_id"))
                for host in self.sandbox.hosts
                for call in host.calls
                if call.get("capability_id")
            }
            cap.required_capabilities = tuple(sorted(set(cap.required_capabilities) | observed))
        output_schema_inferred = False
        if cap.output_schema is None and self.sandbox.observed_values:
            cap.output_schema = _schema_for_values(self.sandbox.observed_values)
            output_schema_inferred = True
        semantic_cases = 0
        for index, detail in enumerate(details):
            evidence = _verification_evidence(
                context.cases[index] if index < len(context.cases) else {}
            )
            detail["verification_category"] = evidence["category"]
            detail["verification_source"] = evidence["source"]
            detail["semantic_verification"] = evidence["independent"] is True
            if detail["semantic_verification"] and detail.get("passed") is True:
                semantic_cases += 1
        derived_cases = derive_proof_cases(cap.input_schema, cap.effective_effects)
        derived_records = await self._run_derived_cases(
            derived_cases,
            authored_cases=list(context.cases),
            authored_details=details,
        )
        total = len(details)
        cap.validation = {
            "tier": context.tier.value,
            "cases_total": total,
            "cases_passed": self.sandbox.passed,
            "all_passed": (
                total > 0
                and self.sandbox.passed == total
                and all(detail["passed"] is True for detail in context.negative_details)
            ),
            "output_schema_inferred": output_schema_inferred,
            "source": context.source_record or {},
            "details": details,
            "risk_tier": _risk_tier(cap),
            "negative_cases_total": len(context.negative_details),
            "negative_cases_passed": sum(
                1 for detail in context.negative_details if detail["passed"] is True
            ),
            "negative_cases": list(context.negative_details),
            "live_failure_cases": list(context.historical_failures),
            "regression_cases": list(context.historical_failures),
            "semantic_verification": {
                "verified_cases": semantic_cases,
                "total_cases": total,
                "unverified_cases": max(total - semantic_cases, 0),
                "promotion_required": True,
                "status": "verified" if semantic_cases else "execution_only",
            },
        }
        cap.validation["derived_proof_corpus"] = {
            "source": "schema_and_effects",
            "digest": corpus_digest(derived_cases),
            "cases": derived_records,
            "executed": sum(
                1 for case in derived_records if case.get("analyzer_status") in {"passed", "failed"}
            ),
            "passed": sum(1 for case in derived_records if case.get("analyzer_status") == "passed"),
        }
        independent = GeneratedCapabilityVerifier.review(cap)
        cap.validation["independent_verification"] = independent
        if not independent["passed"]:
            cap.validation["all_passed"] = False
        cap.lifecycle_state = "VALIDATED" if cap.validation["all_passed"] else "REJECTED"
        if cap.id_generated and cap.validation["all_passed"]:
            cap.id = (
                "synth_"
                + hashlib.sha256(canonical_generated_identity(cap).encode()).hexdigest()[:20]
            )
            cap.id_generated = False
        return cap

    async def _run_derived_cases(
        self,
        derived_cases: tuple[Any, ...] | list[Any],
        *,
        authored_cases: list[dict],
        authored_details: list[dict],
    ) -> list[dict[str, Any]]:
        context = self.context
        cap = context.cap
        executable: list[tuple[Any, dict[str, Any]]] = []
        records: list[dict[str, Any]] = []
        authored_inputs = {
            json.dumps(
                item.get("input") if "input" in item else item.get("args") or {},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ): index
            for index, item in enumerate(authored_cases)
            if index < len(authored_details)
            and authored_details[index].get("passed") is True
            and not (
                item.get("expect_failure")
                or item.get("expect_error_contains") is not None
                or item.get("expected_error") is not None
            )
        }
        has_authored_positive = bool(authored_inputs)
        native_dependencies = bool(cap.required_capabilities)
        schema = cap.input_schema
        unconstrained_object = (
            schema.get("type", "object") == "object"
            and not schema.get("required")
            and not schema.get("properties")
        )
        workspace_bound = context.workspace_root is not None or context.workspace is not None
        for case in derived_cases:
            record = case.to_record()
            if case.kind == "positive" and case.expected == "accept":
                key = json.dumps(case.input, sort_keys=True, separators=(",", ":"), default=str)
                if key in authored_inputs:
                    record["analyzer_status"] = "passed"
                    record["result"] = {
                        "passed": True,
                        "covered_by_authored_case": authored_inputs[key],
                    }
                elif (
                    native_dependencies
                    or workspace_bound
                    or unconstrained_object
                    or (authored_cases and not has_authored_positive)
                ):
                    record["analyzer_status"] = "unverified"
                    record["verification_note"] = (
                        "no standalone derived input was safe to execute; "
                        "authored validation evidence remains the behavioral oracle"
                    )
                else:
                    executable.append((case, {"input": case.input}))
            elif case.kind == "negative" and case.expected == "reject":
                executable.append((case, {"input": case.input, "expect_invalid_input": True}))
            else:
                record["analyzer_status"] = "unverified"
                record["verification_note"] = "no executable behavioral oracle was derivable"
            records.append(record)
        if not executable:
            return records
        runner = ValidationSandboxRunner(
            context.engine,
            cap=cap,
            cases=[case for _derived, case in executable],
            historical_by_id={},
            tier=context.tier,
            workspace_root=context.workspace_root,
            workspace=context.workspace,
            task_id=context.task_id,
            session_id=context.session_id,
            profile=context.profile,
            task_policy=context.task_policy,
            task_budget=context.task_budget,
            generated_call_depth=context.generated_call_depth,
            generated_call_chain=context.generated_call_chain,
            timeout=context.timeout,
            emit_progress=False,
        )
        result = await runner.run()
        details = iter(result.details)
        by_id = {str(record["id"]): record for record in records}
        for case, _input in executable:
            detail = next(details, {"passed": False, "error": "derived case produced no receipt"})
            record = by_id[str(case.id)]
            record["analyzer_status"] = "passed" if detail.get("passed") is True else "failed"
            record["result"] = dict(detail)
        return records


__all__ = [
    "DependencyValidationPhase",
    "EvidenceValidationPhase",
    "SandboxValidationPhase",
    "StaticValidationPhase",
    "ValidationContext",
    "reject",
]
