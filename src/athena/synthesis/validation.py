"""Sandbox validation mechanism (P1-10 extraction).

Sandbox case generation and execution, effect/invariant/resource
expectation checking, and validation progress reporting, moved
verbatim from :mod:`athena.synthesis.engine`. Subordinate to
SynthesisEngine: this module holds no authority of its own — admission
and promotion decisions stay on the engine.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from athena.affordances.validation import ValidationTier
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
)
from athena.protocol.errors import CapabilityUnavailable
from athena.synthesis.verifier import GeneratedCapabilityVerifier
from athena.synthesis.proof_corpus import corpus_digest, derive_proof_cases
from athena.schema import compile_validator

if TYPE_CHECKING:
    from athena.protocol.tasks import WorkspaceSpec
    from athena.synthesis.models import SyntheticCapability as SyntheticCapabilityT
    from athena.synthesis.engine import SynthesisEngine


from athena.synthesis.helpers import (
    _admit_generated_input,
    _schema_for_values,
    _service_negative_cases,
    _verification_evidence,
)
from athena.synthesis.sandbox_runner import ValidationSandboxRunner
from athena.synthesis.helpers import _risk_tier, canonical_generated_identity


class Validator:
    """Sandbox validation machinery for generated capabilities.

    Verbatim extraction from ``SynthesisEngine``: engine-owned state
    and helpers resolve through ``self._e`` at call time.
    """

    def __init__(self, engine: SynthesisEngine) -> None:
        self._e = engine

    def _historical_cases(self, cap: SyntheticCapabilityT) -> tuple[list[dict], dict[str, dict]]:
        """Load and normalize inherited regression evidence exactly once."""
        historical_failures: list[dict] = []
        historical_ids: set[str] = set()
        for key in ("live_failure_cases", "regression_cases"):
            for raw_case in cap.validation.get(key) or ():
                from athena.synthesis.models import RegressionCase

                case = RegressionCase.from_record(
                    dict(raw_case),
                    capability_family=cap.family_id,
                    revision=cap.revision,
                ).to_record()
                if not case["id"]:
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            case, sort_keys=True, separators=(",", ":"), default=str
                        ).encode()
                    ).hexdigest()
                    case["id"] = f"regression:{fingerprint[:24]}"
                identity = str(case.get("id") or json.dumps(case, sort_keys=True, default=str))
                if identity in historical_ids:
                    continue
                historical_ids.add(identity)
                historical_failures.append(case)
        return historical_failures, {
            str(case["id"]): case for case in historical_failures if case.get("id")
        }

    async def _unrequired_capabilities(self, cap: SyntheticCapabilityT) -> list[str]:
        """Resolve every declared native dependency before fixture execution."""
        if not cap.required_capabilities:
            return []
        registry = getattr(self._e._dispatcher, "registry", None) if self._e._dispatcher else None
        unavailable: list[str] = []
        if registry is None:
            unavailable.append("dispatcher registry is unavailable")
            return unavailable
        for capability_id in cap.required_capabilities:
            try:
                descriptor = registry.resolve(capability_id)
            except (
                CapabilityUnavailable,
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                unavailable.append(f"{capability_id}: {exc}")
                continue
            availability = getattr(descriptor, "availability", None)
            if availability is not None:
                value = getattr(availability, "value", "")
                if value != "available":
                    unavailable.append(f"{capability_id}: {value}")
        return unavailable

    async def _negative_corpus(
        self, cap: SyntheticCapabilityT, *, task_id: str | None
    ) -> list[dict[str, object]]:
        """Run the deterministic malformed-input admission corpus."""
        negative_cases = _service_negative_cases(cap.input_schema)
        negative_details: list[dict[str, object]] = []
        negative_executor = self._e._build_executor(cap)
        for index, negative_case in enumerate(negative_cases):
            invalid_input = negative_case.get("input")
            errors = _admit_generated_input(cap, invalid_input)
            probe = await negative_executor.invoke(
                CapabilityRequest(
                    capability_id=cap.id,
                    arguments=cast(Mapping[str, Any], invalid_input),
                    task_id=task_id,
                    call_id=f"synthesis-negative-{cap.id}-{index}",
                )
            )
            probe_metadata = dict(probe.metadata or {})
            admitted_rejection = (
                probe.status is CapabilityResultStatus.FAILED
                and probe_metadata.get("admission_rejected") is True
            )
            negative_details.append(
                {
                    "source": "service_negative",
                    "mutator": negative_case.get("mutator", "wrong_root_type"),
                    "field": negative_case.get("field"),
                    "passed": bool(errors) and admitted_rejection,
                    "admission_boundary": probe_metadata.get(
                        "admission_boundary", "generated_input_schema"
                    ),
                    "executor_invoked": not admitted_rejection,
                    "errors": errors,
                }
            )
        return negative_details

    def _static_rejection(
        self,
        cap: SyntheticCapabilityT,
        cases: list[dict],
        *,
        tier: ValidationTier | str,
        source_record: dict,
        historical_failures: list[dict],
        case_name: str,
        error: str,
    ) -> SyntheticCapabilityT:
        """Record a fail-closed static/dependency rejection."""
        cap.lifecycle_state = "REJECTED"
        cap.validation = {
            "tier": tier,
            "cases_total": len(cases or []),
            "cases_passed": 0,
            "all_passed": False,
            "source": source_record,
            "details": [{"case": case_name, "passed": False, "error": error}],
            "live_failure_cases": historical_failures,
            "regression_cases": historical_failures,
        }
        return cap

    def _normalize_source(
        self,
        cap: SyntheticCapabilityT,
        *,
        tier: ValidationTier | str,
    ) -> tuple[dict, str | None]:
        """Normalize source and return ``(static_record, rejection_reason)``."""
        validation = self._e._source_validator.validate(cap.code, tier=tier)
        cap.code = validation.code
        record = validation.to_dict()
        if not validation.passed:
            failed = "; ".join(
                check.detail for check in validation.checks if check.status == "failed"
            )
            return record, failed or "generated source failed static validation"
        return record, None

    def _compile_contracts(self, cap: SyntheticCapabilityT) -> str | None:
        """Compile input/output contracts and return a static rejection reason."""
        try:
            from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

            compile_validator(cap.input_schema)
            if cap.output_schema is not None:
                compile_validator(cap.output_schema)
        except (SchemaError, SyntaxError, TypeError, ValueError) as exc:
            return f"static validation: {exc}"
        return None

    async def validate(
        self,
        cap: SyntheticCapabilityT,
        cases: list[dict],
        *,
        timeout: float = 15.0,
        tier: ValidationTier | str = ValidationTier.TASK,
        workspace_root: str | None = None,
        workspace: WorkspaceSpec | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        profile: str | None = None,
        task_policy=None,
        task_budget=None,
        generated_call_depth: int = 0,
        generated_call_chain: tuple[str, ...] = (),
    ) -> SyntheticCapabilityT:
        """Run each case in an isolated interpreter; record evidence.

        workspace_root is mounted read-only for validation when supplied.
        This lets a task-local analyzer inspect the task workspace without
        turning validation into host execution. Writes/network remain denied
        by the fixed scratch/read-only authority profile.
        """
        # Validation is itself durable proof. Retain the exact replay corpus
        # even when callers supplied cases separately from synthesize(); a
        # later promotion, repair, or procedure-capsule import must be able
        # to rerun the same behavioral evidence.
        cap.validation_cases = [dict(case) for case in cases or []]
        historical_failures, historical_by_id = self._historical_cases(cap)

        # Static gates run before any trial execution.
        source_record, source_error = self._normalize_source(cap, tier=tier)
        if source_error:
            return self._static_rejection(
                cap,
                cases,
                tier=ValidationTier(tier).value,
                source_record=source_record,
                historical_failures=historical_failures,
                case_name="source",
                error=source_error,
            )
        static_error = self._compile_contracts(cap)
        if static_error:
            return self._static_rejection(
                cap,
                cases,
                tier=ValidationTier(tier).value,
                source_record=source_record,
                historical_failures=historical_failures,
                case_name="static",
                error=static_error,
            )

        # The service owns a small, deterministic negative corpus for every
        # schema it admits.  Keep this separate from authored fixture counts so
        # existing proof remains comparable while promotion can require the
        # stronger malformed-input evidence explicitly.
        negative_details = await self._negative_corpus(cap, task_id=task_id)

        # A declared native capability is part of the generated contract even
        # when a fixture does not exercise the corresponding branch. Resolve
        # every declaration before running fixtures so admission cannot hide a
        # missing or unavailable dependency behind partial coverage.
        unavailable = await self._unrequired_capabilities(cap)
        if unavailable:
            return self._static_rejection(
                cap,
                cases,
                tier=tier if isinstance(tier, str) else tier.value,
                source_record=source_record,
                historical_failures=historical_failures,
                case_name="required_capabilities",
                error="; ".join(unavailable),
            )

        try:
            dependency_metadata = self._e._dependency_metadata(
                cap.required_dependencies,
                workspace.root if workspace is not None else workspace_root,
            )
        except ValueError as exc:
            cap.lifecycle_state = "REJECTED"
            cap.validation = {
                "tier": tier if isinstance(tier, str) else tier.value,
                "cases_total": len(cases or []),
                "cases_passed": 0,
                "all_passed": False,
                "source": source_record,
                "details": [{"case": "dependencies", "passed": False, "error": str(exc)}],
                "live_failure_cases": historical_failures,
                "regression_cases": historical_failures,
            }
            return cap
        cap.dependency_lock = {
            **dict(cap.dependency_lock or {}),
            **dependency_metadata,
        }

        runner = ValidationSandboxRunner(
            self._e,
            cap=cap,
            cases=list(cases or []),
            historical_by_id=historical_by_id,
            tier=tier,
            workspace_root=workspace_root,
            workspace=workspace,
            task_id=task_id,
            session_id=session_id,
            profile=profile,
            task_policy=task_policy,
            task_budget=task_budget,
            generated_call_depth=generated_call_depth,
            generated_call_chain=generated_call_chain,
            timeout=timeout,
        )
        sandbox = await runner.run()
        if sandbox.details and sandbox.details[0].get("case") == "workspace":
            cap.lifecycle_state = "REJECTED"
            cap.validation = {
                "tier": tier if isinstance(tier, str) else tier.value,
                "cases_total": len(cases or []),
                "cases_passed": 0,
                "all_passed": False,
                "source": source_record,
                "details": sandbox.details,
                "live_failure_cases": historical_failures,
                "regression_cases": historical_failures,
            }
            return cap

        passed = sandbox.passed
        details = sandbox.details
        observed_values = sandbox.observed_values
        hosts = sandbox.hosts

        if hosts:
            observed_capabilities = {
                str(call.get("capability_id"))
                for host in hosts
                for call in host.calls
                if call.get("capability_id")
            }
            # Declared dependencies are part of the capability contract even
            # when a fixture does not exercise every branch.
            cap.required_capabilities = tuple(
                sorted(set(cap.required_capabilities) | observed_capabilities)
            )

        output_schema_inferred = False
        if cap.output_schema is None and observed_values:
            cap.output_schema = _schema_for_values(observed_values)
            output_schema_inferred = True

        total = len(details)
        semantic_cases = 0
        for index, detail in enumerate(details):
            evidence = _verification_evidence(cases[index] if index < len(cases) else {})
            detail["verification_category"] = evidence["category"]
            detail["verification_source"] = evidence["source"]
            detail["semantic_verification"] = evidence["independent"] is True
            if detail["semantic_verification"] and detail.get("passed") is True:
                semantic_cases += 1
        derived_cases = derive_proof_cases(cap.input_schema, cap.effective_effects)
        derived_records = await self._run_derived_proof_cases(
            cap,
            derived_cases,
            authored_cases=cases,
            authored_details=details,
            tier=tier,
            workspace_root=workspace_root,
            workspace=workspace,
            task_id=task_id,
            session_id=session_id,
            profile=profile,
            task_policy=task_policy,
            task_budget=task_budget,
            generated_call_depth=generated_call_depth,
            generated_call_chain=generated_call_chain,
            timeout=timeout,
        )
        cap.validation = {
            "tier": tier if isinstance(tier, str) else tier.value,
            "cases_total": total,
            "cases_passed": passed,
            "all_passed": (
                total > 0
                and passed == total
                and all(detail["passed"] is True for detail in negative_details)
            ),
            "output_schema_inferred": output_schema_inferred,
            "source": source_record,
            "details": details,
            "risk_tier": _risk_tier(cap),
            "negative_cases_total": len(negative_details),
            "negative_cases_passed": sum(
                1 for detail in negative_details if detail["passed"] is True
            ),
            "negative_cases": negative_details,
            "live_failure_cases": historical_failures,
            "regression_cases": historical_failures,
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
        independent_review = GeneratedCapabilityVerifier.review(cap)
        cap.validation["independent_verification"] = independent_review
        if not independent_review["passed"]:
            cap.validation["all_passed"] = False
        cap.lifecycle_state = "VALIDATED" if cap.validation["all_passed"] else "REJECTED"
        if cap.id_generated and cap.validation["all_passed"]:
            # Assign the public id only after source normalization, dependency
            # resolution, observed capability requirements, and output-schema
            # inference have completed.  Request-side names are not identity.
            cap.id = (
                "synth_"
                + hashlib.sha256(canonical_generated_identity(cap).encode()).hexdigest()[:20]
            )
            cap.id_generated = False
        return cap

    async def _run_derived_proof_cases(
        self,
        cap: SyntheticCapabilityT,
        derived_cases,
        *,
        authored_cases: list[dict],
        authored_details: list[dict],
        tier,
        workspace_root,
        workspace,
        task_id,
        session_id,
        profile,
        task_policy,
        task_budget,
        generated_call_depth,
        generated_call_chain,
        timeout,
    ) -> list[dict[str, Any]]:
        """Execute derived schema cases without merging them into authored proof."""
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
        schema = cap.input_schema if isinstance(cap.input_schema, Mapping) else {}
        unconstrained_object = (
            schema.get("type", "object") == "object"
            and not schema.get("required")
            and not schema.get("properties")
        )
        workspace_bound = workspace_root is not None or workspace is not None
        for case in derived_cases:
            record = case.to_record()
            if case.kind == "positive" and case.expected == "accept":
                input_key = json.dumps(
                    case.input, sort_keys=True, separators=(",", ":"), default=str
                )
                if input_key in authored_inputs:
                    record["analyzer_status"] = "passed"
                    record["result"] = {
                        "passed": True,
                        "covered_by_authored_case": authored_inputs[input_key],
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
        records_by_id = {str(record["id"]): record for record in records}

        runner = ValidationSandboxRunner(
            self._e,
            cap=cap,
            cases=[case for _derived, case in executable],
            historical_by_id={},
            tier=tier,
            workspace_root=workspace_root,
            workspace=workspace,
            task_id=task_id,
            session_id=session_id,
            profile=profile,
            task_policy=task_policy,
            task_budget=task_budget,
            generated_call_depth=generated_call_depth,
            generated_call_chain=generated_call_chain,
            timeout=timeout,
            emit_progress=False,
        )
        result = await runner.run()
        details = iter(result.details)
        for case, _case_input in executable:
            record = records_by_id[str(case.id)]
            detail = next(details, {"passed": False, "error": "derived case produced no receipt"})
            record["analyzer_status"] = "passed" if detail.get("passed") is True else "failed"
            record["result"] = dict(detail)
        return records

    async def _emit_validation_progress(
        self,
        *,
        task_id: str | None,
        capability_id: str,
        completed: int,
        total: int,
    ) -> None:
        """Publish fixture progress using the real validation denominator."""
        if self._e._dispatcher is None or total <= 0:
            return
        emit = getattr(self._e._dispatcher, "emit_progress", None)
        if emit is None:
            return
        try:
            await emit(
                task_id=task_id,
                call_id=f"{capability_id}:validation",
                capability_id=capability_id,
                value=completed,
                total=total,
                unit="fixtures",
                message=(f"generated validation fixture {completed}/{total} complete"),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            # Progress is observational; it cannot change validation truth.
            return
