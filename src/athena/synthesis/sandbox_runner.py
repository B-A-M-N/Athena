"""Isolated sandbox execution for generated-capability validation.

Subordinate to :class:`athena.synthesis.validation.Validator`. This module owns
disposable workspace preparation, mediated host construction, generated child
execution, and bounded retry policy. It does not decide admission, promotion,
or proof acceptance.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from athena.affordances.validation import ValidationTier
from athena.protocol.capabilities import EffectClass
from athena.protocol.tasks import MutationMode
from athena.synthesis.helpers import (
    _MISSING,
    _apply_workspace_fixture,
    _check_effect_expectations,
    _check_invariants,
    _check_resource_expectations,
)
from athena.schema import validate_schema
from athena.synthesis.helpers import _child_code, _workspace_snapshot
from athena.synthesis.runtime import GeneratedToolHost
from athena.workspace_manifest import copy_workspace_tree_async, rmtree_async

if TYPE_CHECKING:
    from athena.protocol.tasks import WorkspaceSpec
    from athena.synthesis.models import SyntheticCapability as SyntheticCapabilityT
    from athena.synthesis.engine import SynthesisEngine


__all__ = ["ValidationSandboxRunner"]


@dataclass
class SandboxResult:
    details: list[dict] = field(default_factory=list)
    passed: int = 0
    observed_values: list[object] = field(default_factory=list)
    hosts: list[GeneratedToolHost] = field(default_factory=list)


class ValidationSandboxRunner:
    """Run authored fixtures inside disposable, mediated sandboxes."""

    def __init__(
        self,
        engine: SynthesisEngine,
        *,
        cap: SyntheticCapabilityT,
        cases: list[dict],
        historical_by_id: dict[str, dict],
        tier: ValidationTier | str,
        workspace_root: str | None,
        workspace: WorkspaceSpec | None,
        task_id: str | None,
        session_id: str | None,
        profile: str | None,
        task_policy,
        task_budget,
        generated_call_depth: int,
        generated_call_chain: tuple[str, ...],
        timeout: float = 15.0,
        emit_progress: bool = True,
    ) -> None:
        self._e = engine
        self.cap = cap
        self.cases = cases
        self.historical_by_id = historical_by_id
        self.tier = tier
        self.base_workspace_root = workspace.root if workspace is not None else workspace_root
        self.workspace = workspace
        self.task_id = task_id
        self.session_id = session_id
        self.profile = profile
        self.task_policy = task_policy
        self.task_budget = task_budget
        self.generated_call_depth = generated_call_depth
        self.generated_call_chain = generated_call_chain
        self.timeout = timeout
        self.emit_progress = emit_progress
        self.validation_parent: str | None = None
        self.result = SandboxResult()

    async def run(self) -> SandboxResult:
        """Prepare, execute, and clean one disposable validation session."""
        try:
            if self.base_workspace_root:
                self.validation_parent = tempfile.mkdtemp(prefix="athena-synth-workspaces-")
                if not os.path.isdir(self.base_workspace_root):
                    raise OSError(f"workspace is not a directory: {self.base_workspace_root}")
            await self._prepare_dependencies()
            for index, case in enumerate(self.cases):
                await self._run_one(index, case)
        except OSError as exc:
            self.result.details.append(
                {"case": "workspace", "passed": False, "error": f"validation sandbox: {exc}"}
            )
        finally:
            if self.validation_parent:
                await rmtree_async(self.validation_parent, ignore_errors=True)
        return self.result

    async def _prepare_dependencies(self) -> None:
        dependency_metadata = self._e._dependency_metadata(
            self.cap.required_dependencies,
            self.base_workspace_root,
        )
        self.cap.dependency_lock = {
            **dict(self.cap.dependency_lock or {}),
            **dependency_metadata,
        }

    def _host(self, execution_root: str) -> GeneratedToolHost | None:
        if self._e._dispatcher is None or self.workspace is None or not self.task_id:
            return None
        validation_workspace = replace(
            self.workspace,
            id=f"{self.workspace.id}:synthesis-validation",
            root=execution_root,
            readable=(),
            writable=(),
            mutation_mode=MutationMode.DIRECT,
        )
        host = GeneratedToolHost(
            dispatcher=self._e._dispatcher,
            workspace=validation_workspace,
            task_id=self.task_id,
            session_id=self.session_id,
            profile=self.profile,
            task_policy=self.task_policy,
            task_budget=self.task_budget,
            call_depth=self.generated_call_depth,
            call_chain=(*self.generated_call_chain, self.cap.id),
            inherited_effects=frozenset(
                EffectClass(effect) for effect in self._e._runtime_effective_effects(self.cap)
            ),
            inherited_capability_id=self.cap.id,
            allowed_capabilities=(
                frozenset(self.cap.required_capabilities)
                if self.cap.required_capabilities
                else None
            ),
        )
        self.result.hosts.append(host)
        return host

    async def _run_case_sandbox(self, case: dict):
        execution_root = self.base_workspace_root
        host = None
        if self.validation_parent:
            if self.base_workspace_root is None:
                raise ValueError("validation workspace root is unavailable")
            execution_root = tempfile.mkdtemp(dir=self.validation_parent)
            await copy_workspace_tree_async(
                self.base_workspace_root,
                execution_root,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__"),
            )
            _apply_workspace_fixture(case, execution_root)
            host = self._host(execution_root)
        before = _workspace_snapshot(execution_root) if execution_root else None
        dependency_paths = self._e._dependency_paths(
            self.cap.required_dependencies,
            execution_root,
            expected_fingerprint=None,
        )
        case_input = case["input"] if "input" in case else case.get("args") or {}
        child = _child_code(repr(self.cap.code))
        output = await self._e._run_child_async(
            child,
            json.dumps(case_input),
            timeout=self.timeout,
            workspace_root=execution_root,
            effects=self._e._authority_values(self.cap),
            python_paths=dependency_paths,
            host=host,
        )
        # Sandbox startup can consume the first budget. Retry infrastructure
        # timeouts once; expected failures never retry because timeout can be
        # asserted proof.
        expected_failure = bool(
            case.get("expect_failure")
            or case.get("expect_error_contains") is not None
            or case.get("expected_error") is not None
        )
        if (
            output[2] == 124
            and output[1].strip() == "synthetic execution timed out"
            and not expected_failure
        ):
            output = await self._e._run_child_async(
                child,
                json.dumps(case_input),
                timeout=self.timeout * 2,
                workspace_root=execution_root,
                effects=self._e._authority_values(self.cap),
                python_paths=dependency_paths,
                host=host,
            )
        return (*output, execution_root, before, host)

    async def _run_one(self, index: int, case: dict) -> None:
        try:
            case_args = case["input"] if "input" in case else case.get("args") or {}
            input_errors = validate_schema(self.cap.input_schema, case_args)
            if input_errors:
                self._record_invalid_input(index, case, input_errors)
                return
            out, err, rc, case_root, before, case_host = await self._run_case_sandbox(case)
            await self._record_result_with_invariants(
                index, case, out, err, rc, case_root, before, case_host
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            self.result.details.append({"case": index, "passed": False, "error": str(exc)})
        finally:
            if self.emit_progress:
                await self._e._emit_validation_progress(
                    task_id=self.task_id or self.cap.task_id,
                    capability_id=self.cap.id,
                    completed=index + 1,
                    total=len(self.cases),
                )

    def _record_invalid_input(self, index: int, case: dict, input_errors: list[str]) -> None:
        regression_id = str(case.get("id") or "")
        replay = self._record_historical_replay(
            regression_id, passed=bool(case.get("expect_invalid_input"))
        )
        self.result.details.append(
            {
                "case": index,
                "passed": bool(case.get("expect_invalid_input")),
                "error": "input contract: " + "; ".join(input_errors),
                **({"regression_id": regression_id} if regression_id else {}),
                **replay,
            }
        )
        if case.get("expect_invalid_input"):
            self.result.passed += 1

    async def _record_result_with_invariants(
        self, index, case, out, err, rc, case_root, before, case_host
    ) -> None:
        raw_invariants = case.get("invariants")
        raw_requirements = case.get("verification_requirements")
        oracle = case.get("behavioral_oracle")
        combined: list[object] = []
        if isinstance(raw_invariants, (list, tuple)):
            combined.extend(raw_invariants)
        if isinstance(raw_requirements, (list, tuple)):
            combined.extend(raw_requirements)
        if isinstance(oracle, Mapping) and isinstance(oracle.get("invariants"), (list, tuple)):
            combined.extend(oracle["invariants"])
        invariant_errors = await _check_invariants({**case, "invariants": combined}, case_host)
        if invariant_errors:
            regression_id = str(case.get("id") or "")
            self.result.details.append(
                {
                    "case": index,
                    "passed": False,
                    "value": None,
                    "rc": rc,
                    "error": invariant_errors,
                    **({"regression_id": regression_id} if regression_id else {}),
                    **self._record_historical_replay(regression_id, passed=False),
                }
            )
            return
        self._record_result(index, case, out, err, rc, case_root, before, case_host)

    def _record_result(self, index, case, out, err, rc, case_root, before, case_host) -> None:
        """Interpret one sandbox execution and append deterministic evidence."""
        ok = rc == 0
        value = None
        output_errors: list[str] = []
        expected_failure = bool(
            case.get("expect_failure")
            or case.get("expect_error_contains") is not None
            or case.get("expected_error") is not None
        )
        if ok and not expected_failure:
            lines = [line for line in out.splitlines() if line.strip()]
            result_lines = [line for line in lines if line.startswith("__RESULT__")]
            if not result_lines:
                ok = False
                output_errors.append("missing result envelope")
            elif len(result_lines) != 1 or len(lines) != 1:
                ok = False
                output_errors.append("extra incompatible protocol output")
        if ok and "__RESULT__" in out:
            try:
                line = out.split("__RESULT__", 1)[1].splitlines()[0]
                value = json.loads(line)
                self.result.observed_values.append(value)
            except (IndexError, json.JSONDecodeError):
                ok = False
                output_errors.append("malformed result envelope")
        if ok and self.cap.output_schema is not None:
            output_errors = validate_schema(self.cap.output_schema, value)
            ok = not output_errors
        oracle = case.get("behavioral_oracle")
        oracle_map = oracle if isinstance(oracle, Mapping) else {}
        expect = case.get("expect_output_contains", oracle_map.get("expect_output_contains"))
        if expect is not None:
            ok = value is not None and ok and str(expect).lower() in json.dumps(value).lower()
        expected_output = case.get(
            "expect_output",
            oracle_map.get("expect_output", oracle_map.get("expected_output", _MISSING)),
        )
        if expected_output is not _MISSING:
            ok = ok and value == expected_output
        if expected_failure:
            ok = rc != 0
            expected_error = case.get("expect_error_contains")
            if expected_error is not None:
                ok = ok and str(expected_error).casefold() in (f"{out}\n{err}".casefold())
            exact_error = case.get("expected_error", _MISSING)
            if exact_error is not _MISSING:
                ok = ok and str(exact_error) == (err or out).strip()
        effect_error = _check_effect_expectations(case, case_host)
        resource_error, changed_resources = _check_resource_expectations(case, case_root, before)
        case_error = effect_error or resource_error
        if case_error:
            ok = False
        self.result.details.append(
            {
                "case": index,
                "passed": ok,
                "value": value,
                "rc": rc,
                "changed_resources": changed_resources,
                **(
                    {"regression_id": str(case["id"])}
                    if case.get("id") in self.historical_by_id
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
        if ok:
            self.result.passed += 1
        regression_id = str(case.get("id") or "")
        replay = self._record_historical_replay(regression_id, passed=ok)
        if replay:
            self.result.details[-1].update(replay)

    def _record_historical_replay(self, regression_id: str, *, passed: bool) -> dict[str, object]:
        """Record replay truth; execution recovery alone is not semantic proof."""
        record = self.historical_by_id.get(regression_id)
        if record is None:
            return {}
        oracle = record.get("behavioral_oracle")
        has_oracle = isinstance(oracle, Mapping) and bool(oracle)
        record["last_replay"] = {
            "revision": self.cap.revision,
            "reproduced": not passed,
            "semantic_oracle_available": has_oracle,
            "status": (
                "reproduced"
                if not passed
                else "resolved"
                if has_oracle
                else "execution_recovered_semantically_unverified"
            ),
        }
        if passed and has_oracle:
            record["resolved_by_revision"] = self.cap.revision
        return {
            "regression_id": regression_id,
            "regression_reproduced": not passed,
            "regression_resolution": (
                "proven"
                if passed and has_oracle
                else "execution_recovered_semantically_unverified"
                if passed
                else "reproduced"
            ),
        }
