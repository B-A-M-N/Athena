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
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from athena.affordances.validation import ValidationTier
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.errors import CapabilityUnavailable
from athena.protocol.tasks import MutationMode
from athena.synthesis.runtime import GeneratedToolHost
from athena.workspace_manifest import copy_workspace_tree

if TYPE_CHECKING:
    from athena.protocol.tasks import WorkspaceSpec
    from athena.synthesis.engine import SyntheticCapability as SyntheticCapabilityT
    from athena.synthesis.engine import SynthesisEngine


def _mod():
    # Patch seams / engine-module helpers resolve through the engine module.
    from athena.synthesis import engine

    return engine


class Validator:
    """Sandbox validation machinery for generated capabilities.

    Verbatim extraction from ``SynthesisEngine``: engine-owned state
    and helpers resolve through ``self._e`` / ``_mod()`` at call time.
    """

    def __init__(self, engine: SynthesisEngine) -> None:
        self._e = engine

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
        historical_failures: list[dict] = []
        historical_ids: set[str] = set()
        for key in ("live_failure_cases", "regression_cases"):
            for raw_case in cap.validation.get(key) or ():
                case = (
                    _mod()
                    .RegressionCase.from_record(
                        dict(raw_case),
                        capability_family=cap.family_id,
                        revision=cap.revision,
                    )
                    .to_record()
                )
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
        historical_by_id = {str(case["id"]): case for case in historical_failures if case.get("id")}
        passed = 0
        details = []
        observed_values: list[object] = []

        # Static source checks happen before any trial execution.  The source
        # validator is intentionally separate from tool-input repair: it
        # validates generated implementation code, while repair validates one
        # model-produced argument candidate against the capability schema.
        source_validation = self._e._source_validator.validate(cap.code, tier=tier)
        cap.code = source_validation.code
        source_record = source_validation.to_dict()
        if not source_validation.passed:
            cap.lifecycle_state = "REJECTED"
            cap.validation = {
                "tier": source_validation.tier.value,
                "cases_total": len(cases or []),
                "cases_passed": 0,
                "all_passed": False,
                "source": source_record,
                "details": [
                    {"case": "source", "passed": False, "error": check.detail}
                    for check in source_validation.checks
                    if check.status == "failed"
                ],
                "live_failure_cases": historical_failures,
                "regression_cases": historical_failures,
            }
            return cap

        # Schema compilation is the second half of the static contract gate.
        # Registration and dispatch use the same jsonschema implementation.
        try:
            from jsonschema.exceptions import (  # type: ignore[import-untyped]
                SchemaError,
            )

            from athena.capabilities.registry import _compile_validator

            _compile_validator(cap.input_schema)
            if cap.output_schema is not None:
                _compile_validator(cap.output_schema)
        except (SchemaError, SyntaxError, TypeError, ValueError) as exc:
            cap.lifecycle_state = "REJECTED"
            cap.validation = {
                "tier": source_validation.tier.value,
                "cases_total": len(cases or []),
                "cases_passed": 0,
                "all_passed": False,
                "source": source_record,
                "details": [
                    {"case": "static", "passed": False, "error": f"static validation: {exc}"}
                ],
                "live_failure_cases": historical_failures,
                "regression_cases": historical_failures,
            }
            return cap

        # The service owns a small, deterministic negative corpus for every
        # schema it admits.  Keep this separate from authored fixture counts so
        # existing proof remains comparable while promotion can require the
        # stronger malformed-input evidence explicitly.
        negative_cases = _mod()._service_negative_cases(cap.input_schema)
        negative_details: list[dict[str, object]] = []
        negative_executor = self._e._build_executor(cap)
        for index, negative_case in enumerate(negative_cases):
            invalid_input = negative_case.get("input")
            errors = _mod()._admit_generated_input(cap, invalid_input)
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

        # A declared native capability is part of the generated contract even
        # when a fixture does not exercise the corresponding branch. Resolve
        # every declaration before running fixtures so admission cannot hide a
        # missing or unavailable dependency behind partial coverage.
        if cap.required_capabilities:
            registry = (
                getattr(self._e._dispatcher, "registry", None) if self._e._dispatcher else None
            )
            unavailable: list[str] = []
            if registry is None:
                unavailable.append("dispatcher registry is unavailable")
            else:
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
                    if getattr(descriptor, "availability", None) is not None:
                        availability = getattr(descriptor.availability, "value", "")
                        if availability != "available":
                            unavailable.append(f"{capability_id}: {availability}")
            if unavailable:
                cap.lifecycle_state = "REJECTED"
                cap.validation = {
                    "tier": source_validation.tier.value,
                    "cases_total": len(cases or []),
                    "cases_passed": 0,
                    "all_passed": False,
                    "source": source_record,
                    "details": [
                        {
                            "case": "required_capabilities",
                            "passed": False,
                            "error": "; ".join(unavailable),
                        }
                    ],
                    "live_failure_cases": historical_failures,
                    "regression_cases": historical_failures,
                }
                return cap

        validation_parent: str | None = None
        base_workspace_root = workspace.root if workspace is not None else workspace_root
        if base_workspace_root:
            # Every fixture gets its own clone. This prevents one validation
            # case's writes, generated sessions, or temporary artifacts from
            # becoming evidence for the next case.
            try:
                validation_parent = tempfile.mkdtemp(prefix="athena-synth-workspaces-")
                if not os.path.isdir(base_workspace_root):
                    raise OSError(f"workspace is not a directory: {base_workspace_root}")
            except OSError as exc:
                cap.lifecycle_state = "REJECTED"
                cap.validation = {
                    "tier": source_validation.tier.value,
                    "cases_total": len(cases or []),
                    "cases_passed": 0,
                    "all_passed": False,
                    "source": source_record,
                    "details": [
                        {
                            "case": "workspace",
                            "passed": False,
                            "error": f"validation workspace: {exc}",
                        }
                    ],
                    "live_failure_cases": historical_failures,
                    "regression_cases": historical_failures,
                }
                return cap

        try:
            dependency_metadata = self._e._dependency_metadata(
                cap.required_dependencies, base_workspace_root
            )
        except ValueError as exc:
            if validation_parent:
                shutil.rmtree(validation_parent, ignore_errors=True)
            cap.lifecycle_state = "REJECTED"
            cap.validation = {
                "tier": source_validation.tier.value,
                "cases_total": len(cases or []),
                "cases_passed": 0,
                "all_passed": False,
                "source": source_record,
                "details": [
                    {
                        "case": "dependencies",
                        "passed": False,
                        "error": str(exc),
                    }
                ],
                "live_failure_cases": historical_failures,
                "regression_cases": historical_failures,
            }
            return cap
        cap.dependency_lock = {
            **dict(cap.dependency_lock or {}),
            **dependency_metadata,
        }

        child = _mod()._child_code(repr(cap.code))
        hosts: list[GeneratedToolHost] = []

        async def _run_case(case: dict, *, retry_transient_timeout: bool = True):
            execution_root = base_workspace_root
            host: GeneratedToolHost | None = None
            if validation_parent:
                if base_workspace_root is None:
                    raise ValueError("validation workspace root is unavailable")
                execution_root = tempfile.mkdtemp(dir=validation_parent)
                copy_workspace_tree(
                    base_workspace_root,
                    execution_root,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"),
                )
                _mod()._apply_workspace_fixture(case, execution_root)
                if self._e._dispatcher is not None and workspace is not None and task_id:
                    validation_workspace = replace(
                        workspace,
                        id=f"{workspace.id}:synthesis-validation",
                        root=execution_root,
                        readable=(),
                        writable=(),
                        mutation_mode=MutationMode.DIRECT,
                    )
                    host = GeneratedToolHost(
                        dispatcher=self._e._dispatcher,
                        workspace=validation_workspace,
                        task_id=task_id,
                        session_id=session_id,
                        profile=profile,
                        task_policy=task_policy,
                        task_budget=task_budget,
                        call_depth=generated_call_depth,
                        call_chain=(*generated_call_chain, cap.id),
                        inherited_effects=frozenset(
                            EffectClass(effect)
                            for effect in self._e._runtime_effective_effects(cap)
                        ),
                        inherited_capability_id=cap.id,
                        allowed_capabilities=(
                            frozenset(cap.required_capabilities)
                            if cap.required_capabilities
                            else None
                        ),
                    )
                    hosts.append(host)
            before = _mod()._workspace_snapshot(execution_root) if execution_root else None
            dependency_paths = self._e._dependency_paths(
                cap.required_dependencies,
                execution_root,
                expected_fingerprint=None,
            )
            case_input = case["input"] if "input" in case else case.get("args") or {}
            output = await self._e._run_child_async(
                child,
                json.dumps(case_input),
                timeout=timeout,
                workspace_root=execution_root,
                effects=self._e._authority_values(cap),
                python_paths=dependency_paths,
                host=host,
            )
            # Bubblewrap/process startup can occasionally consume the entire
            # validation budget under host contention before a trivial child
            # reaches user code. Retry that infrastructure-shaped timeout once
            # in a fresh isolated workspace; a genuinely non-terminating
            # generated case still times out on the second attempt and is
            # rejected normally. Expected-failure fixtures do not pay this
            # extra retry because their timeout is part of the asserted proof.
            if (
                retry_transient_timeout
                and output[2] == 124
                and output[1].strip() == "synthetic execution timed out"
            ):
                # Re-enter the case builder so the retry receives a fresh
                # workspace clone and a fresh generated host, rather than
                # inheriting any partial state from the timed-out child.
                return await _run_case(case, retry_transient_timeout=False)
            return (*output, execution_root, before, host)

        for i, case in enumerate(cases or []):
            try:
                case_args = case["input"] if "input" in case else case.get("args") or {}
                input_errors = _mod().validate_schema(cap.input_schema, case_args)
                if input_errors:
                    regression_id = str(case.get("id") or "")
                    if case.get("expect_invalid_input") and regression_id in historical_by_id:
                        historical_by_id[regression_id]["resolved_by_revision"] = cap.revision
                    details.append(
                        {
                            "case": i,
                            "passed": bool(case.get("expect_invalid_input")),
                            "error": "input contract: " + "; ".join(input_errors),
                            **({"regression_id": regression_id} if regression_id else {}),
                        }
                    )
                    if case.get("expect_invalid_input"):
                        passed += 1
                    continue
                expected_failure = bool(
                    case.get("expect_failure")
                    or case.get("expect_error_contains") is not None
                    or case.get("expected_error") is not None
                )
                out, err, rc, case_root, before, case_host = await _run_case(
                    case, retry_transient_timeout=not expected_failure
                )
                ok = rc == 0
                marker = "__RESULT__"
                value = None
                output_errors: list[str] = []
                if ok and marker in out:
                    try:
                        line = out.split(marker, 1)[1].splitlines()[0]
                        value = json.loads(line)
                        observed_values.append(value)
                    except (IndexError, json.JSONDecodeError):
                        ok = False
                if ok and cap.output_schema is not None:
                    output_errors = _mod().validate_schema(cap.output_schema, value)
                    ok = not output_errors
                expect = case.get("expect_output_contains")
                if expect is not None:
                    if value is None:
                        ok = False
                    else:
                        ok = ok and str(expect).lower() in json.dumps(value).lower()
                expected_output = case.get("expect_output", _mod()._MISSING)
                if expected_output is not _mod()._MISSING:
                    ok = ok and value == expected_output
                if expected_failure:
                    ok = rc != 0
                    expected_error = case.get("expect_error_contains")
                    if expected_error is not None:
                        ok = ok and str(expected_error).casefold() in (f"{out}\n{err}".casefold())
                    exact_error = case.get("expected_error", _mod()._MISSING)
                    if exact_error is not _mod()._MISSING:
                        ok = ok and str(exact_error) == (err or out).strip()
                host = case_host
                effect_error = _mod()._check_effect_expectations(case, host)
                if effect_error:
                    ok = False
                resource_error, changed_resources = _mod()._check_resource_expectations(
                    case,
                    case_root,
                    before,
                )
                if resource_error:
                    ok = False
                raw_invariants = case.get("invariants")
                raw_verification_requirements = case.get("verification_requirements")
                combined_invariants: list[object] = []
                if isinstance(raw_invariants, (list, tuple)):
                    combined_invariants.extend(raw_invariants)
                if isinstance(raw_verification_requirements, (list, tuple)):
                    combined_invariants.extend(raw_verification_requirements)
                invariant_errors = await _mod()._check_invariants(
                    {
                        **case,
                        "invariants": combined_invariants,
                    },
                    host,
                )
                if invariant_errors:
                    ok = False
                case_error = effect_error or resource_error or invariant_errors
                passed += 1 if ok else 0
                details.append(
                    {
                        "case": i,
                        "passed": ok,
                        "value": value,
                        "rc": rc,
                        "changed_resources": changed_resources,
                        **(
                            {"regression_id": str(case["id"])}
                            if case.get("id") in historical_by_id
                            else {}
                        ),
                        **(
                            {"error": "output contract: " + "; ".join(output_errors)}
                            if output_errors
                            else {}
                        ),
                        **({"error": case_error} if case_error else {}),
                        **({"stderr": err[-300:]} if err else {}),
                    }
                )
                if ok and case.get("id") in historical_by_id:
                    # A successor resolves a historical live failure only
                    # after the inherited fixture itself passes.  Keeping the
                    # revision marker makes future repairs inherit unresolved
                    # failures while preserving the proof trail for resolved
                    # ones.
                    historical_by_id[str(case["id"])]["resolved_by_revision"] = cap.revision
            except (KeyError, OSError, TypeError, ValueError) as exc:
                details.append({"case": i, "passed": False, "error": str(exc)})
            finally:
                await self._e._emit_validation_progress(
                    task_id=task_id or cap.task_id,
                    capability_id=cap.id,
                    completed=i + 1,
                    total=len(cases or []),
                )

        if validation_parent:
            shutil.rmtree(validation_parent, ignore_errors=True)

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
            # A generated capability that omitted an output contract still
            # gets a concrete model-facing contract from successful fixtures.
            # Infer it after execution so it describes the actual boundary.
            cap.output_schema = _mod()._schema_for_values(observed_values)
            output_schema_inferred = True

        total = len(details)
        cap.validation = {
            "tier": source_validation.tier.value,
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
            "risk_tier": _mod()._risk_tier(cap),
            "negative_cases_total": len(negative_details),
            "negative_cases_passed": sum(
                1 for detail in negative_details if detail["passed"] is True
            ),
            "negative_cases": negative_details,
            "live_failure_cases": historical_failures,
            "regression_cases": historical_failures,
        }
        cap.lifecycle_state = "VALIDATED" if cap.validation["all_passed"] else "REJECTED"
        if cap.id_generated and cap.validation["all_passed"]:
            # Assign the public id only after source normalization, dependency
            # resolution, observed capability requirements, and output-schema
            # inference have completed.  Request-side names are not identity.
            cap.id = (
                "synth_"
                + hashlib.sha256(_mod().canonical_generated_identity(cap).encode()).hexdigest()[:20]
            )
            cap.id_generated = False
        return cap

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
