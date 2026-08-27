"""Ephemeral capability synthesis + proof-carrying promotion (fusion #4/#5).

Athena doesn't just generate skills-as-text: it can synthesize a TEMPORARY
EXECUTABLE CAPABILITY from a proven execution trace and register it through
the same registry -> policy -> executor path as native capabilities.

Lifecycle:
    ad-hoc execution -> synthesized helper -> sandbox validation ->
    effect classification -> EPHEMERAL capability (task-scoped) ->
    repeated success -> SkillCandidate with proof -> explicit promotion.

A promoted synthetic capability is PROOF-CARRYING: it keeps its validation
record, usage count, provenance, and effect envelope, so later retrieval
knows "executed successfully N times under these conditions", not merely
"advice I once wrote".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from athena.affordances.models import (
    AffordanceScope,
    DependencyRequirement,
    GeneratedCapability,
)
from athena.affordances.validation import GeneratedSourceValidator, ValidationTier
from athena.execution.process_tree import kill_tree, sandbox_argv, spawn_owned
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.ids import new_id

__all__ = ["SynthesisEngine", "SyntheticCapability"]

_logger = logging.getLogger("athena.synthesis")

# Generated Python is an untrusted implementation, not an authority
# declaration.  It may compute over the task workspace inside the restricted
# runtime, but it cannot acquire process-spawn, write, delete, network, or
# privileged authority merely by naming those effects in its metadata.  A
# generated implementation that needs those operations must request the
# corresponding native capability through the normal dispatcher.
_GENERATED_EFFECTIVE_AUTHORITY = frozenset({
    "READ_LOCAL", "EXECUTE",
})

def _child_code(cap_code_repr: str) -> str:
    """Build the sandboxed child-process program for one capability."""
    return (
        "import json, sys\n"
        'ARGS = json.loads(sys.stdin.read() or "{}")\n'
        "NS = {}\n"
        f"exec({cap_code_repr}, NS)\n"
        'result = NS["run"](ARGS)\n'
        'print("__RESULT__" + json.dumps(result))\n'
    )


@dataclass
class SyntheticCapability:
    """A generated, validated, task-scoped executable capability."""

    id: str
    name: str
    description: str
    code: str                       # python source defining `def run(args)`
    input_schema: dict
    effects: frozenset              # declared effect envelope
    task_id: str | None
    provenance: dict                # originating task/call ids
    validation: dict                # test results from sandbox run
    uses: int = 0
    successes: int = 0
    failures: int = 0
    validation_cases: list[dict] | None = None
    required_dependencies: tuple[DependencyRequirement, ...] = ()
    # This is calculated by Athena's sandbox contract, not trusted from the
    # generated source or its declared effects.
    effective_effects: frozenset[str] = _GENERATED_EFFECTIVE_AUTHORITY
    output_schema: dict | None = None
    lifecycle_state: str = "DRAFT"
    supersedes: tuple[str, ...] = ()
    dependency_lock: dict = field(default_factory=dict)
    last_used_at: str | None = None


class SynthesisEngine:
    """Registers temporary capabilities born from execution traces."""

    def __init__(
        self,
        *,
        restricted_env: bool = True,
        source_validator: GeneratedSourceValidator | None = None,
    ) -> None:
        self._restricted_env = restricted_env
        self._source_validator = source_validator or GeneratedSourceValidator()
        self._synthetic: dict[str, SyntheticCapability] = {}
        self._executors: dict[str, object] = {}

    def _child_env(self) -> dict:
        if not self._restricted_env:
            return {**os.environ, "PYTHONIOENCODING": "utf-8"}
        allowed = ("PATH", "PYTHONIOENCODING", "LANG", "LC_ALL", "TMPDIR")
        env = {k: os.environ[k] for k in allowed if k in os.environ}
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    @staticmethod
    def _effect_values(cap: SyntheticCapability) -> set[str]:
        return {getattr(effect, "value", str(effect)) for effect in cap.effects}

    @staticmethod
    def _authority_values(cap: SyntheticCapability) -> set[str]:
        return set(cap.effective_effects)

    def _run_child(
        self,
        child: str,
        payload: str,
        *,
        timeout: float,
        workspace_root: str | None = None,
        effects: set[str] | None = None,
    ) -> tuple[str, str, int]:
        """Run generated code inside the same namespace boundary as runtimes.

        A subprocess with a sanitized environment is not a sandbox: Python
        can still open arbitrary host paths.  Bubblewrap is therefore required
        here as well.  Read-only synthetic capabilities receive a read-only
        workspace; write/delete effects explicitly receive writable scope.
        """
        owned_root = workspace_root is None
        root = workspace_root or tempfile.mkdtemp(prefix="athena-synth-")
        values = effects or set()
        writable = bool({"WRITE_LOCAL", "DELETE"} & values)
        network = "allow" if {"NETWORK_READ", "NETWORK_WRITE"} & values else "deny"
        proc = None
        try:
            proc = spawn_owned(
                [sys.executable, "-c", child],
                env=self._child_env(),
                sandbox_root=root,
                network_policy=network,
                sandbox_writable=writable,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(input=payload, timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                stdout, stderr = proc.communicate()
                return stdout, stderr or "synthetic execution timed out", 124
            return stdout, stderr, proc.returncode
        finally:
            if owned_root:
                shutil.rmtree(root, ignore_errors=True)

    async def _run_child_async(
        self,
        child: str,
        payload: str,
        *,
        timeout: float,
        workspace_root: str | None = None,
        effects: set[str] | None = None,
    ) -> tuple[str, str, int]:
        """Async equivalent of :meth:`_run_child` for live validation/invocation.

        Validation and generated capability calls are part of the async agent
        path.  Using ``asyncio.create_subprocess_exec`` keeps the event loop
        responsive and avoids relying on thread-pool subprocess semantics.
        The command line is built by the same fail-closed Bubblewrap policy as
        the synchronous compatibility path.
        """
        owned_root = workspace_root is None
        root = workspace_root or tempfile.mkdtemp(prefix="athena-synth-")
        values = effects or set()
        writable = bool({"WRITE_LOCAL", "DELETE"} & values)
        network = "allow" if {"NETWORK_READ", "NETWORK_WRITE"} & values else "deny"
        proc = None
        try:
            argv = sandbox_argv(
                [sys.executable, "-c", child],
                root=root,
                network_policy=network,
                writable=writable,
            )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=self._child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(payload.encode()), timeout=timeout,
                )
            except TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                return (
                    stdout.decode("utf-8", errors="replace"),
                    stderr.decode("utf-8", errors="replace")
                    or "synthetic execution timed out",
                    124,
                )
            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                proc.returncode if proc.returncode is not None else 1,
            )
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            raise
        finally:
            if owned_root:
                shutil.rmtree(root, ignore_errors=True)

    def synthesize(
        self,
        *,
        capability_id: str | None = None,
        name: str,
        description: str,
        code: str,
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        effects: set | frozenset | None = None,
        task_id: str | None = None,
        provenance: dict | None = None,
        validation_cases: list[dict] | None = None,
        required_dependencies: tuple[DependencyRequirement, ...] = (),
    ) -> SyntheticCapability:
        """Create (but do not yet trust) a synthetic capability.

        ``validation_cases`` are [{"args": {...}, "expect_output_contains": ...}]
        executed in a subprocess sandbox before the capability becomes callable.
        """
        return SyntheticCapability(
            id=capability_id or f"synth_{name}",
            name=name,
            description=description,
            code=code,
            input_schema=(
                input_schema
                if input_schema is not None
                else {"type": "object", "properties": {}}
            ),
            output_schema=output_schema,
            effects=frozenset(effects or {EffectClass.READ_LOCAL}),
            task_id=task_id,
            provenance=provenance or {},
            validation={},
            validation_cases=[dict(case) for case in validation_cases or []],
            required_dependencies=required_dependencies,
            effective_effects=_GENERATED_EFFECTIVE_AUTHORITY,
        )

    async def validate(
        self, cap: SyntheticCapability, cases: list[dict],
        *, timeout: float = 15.0,
        tier: ValidationTier | str = ValidationTier.TASK,
        workspace_root: str | None = None,
    ) -> SyntheticCapability:
        """Run each case in an isolated interpreter; record evidence.

        workspace_root is mounted read-only for validation when supplied.
        This lets a task-local analyzer inspect the task workspace without
        turning validation into host execution. Writes/network remain denied
        by the fixed scratch/read-only authority profile.
        """
        passed = 0
        details = []
        observed_values: list[object] = []

        # Static source checks happen before any trial execution.  The source
        # validator is intentionally separate from tool-input repair: it
        # validates generated implementation code, while repair validates one
        # model-produced argument candidate against the capability schema.
        source_validation = self._source_validator.validate(cap.code, tier=tier)
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
                "cases_passed": 0, "all_passed": False,
                "source": source_record,
                "details": [{"case": "static", "passed": False,
                              "error": f"static validation: {exc}"}],
            }
            return cap

        child = _child_code(repr(cap.code))
        from athena.capabilities.registry import validate_schema

        async def _run_case(case: dict):
            return await self._run_child_async(
                child,
                json.dumps(case.get("args") or {}),
                timeout=timeout,
                workspace_root=workspace_root,
                effects=self._authority_values(cap),
            )

        for i, case in enumerate(cases or []):
            try:
                case_args = case.get("args") or {}
                input_errors = validate_schema(cap.input_schema, case_args)
                if input_errors:
                    details.append({
                        "case": i, "passed": False,
                        "error": "input contract: " + "; ".join(input_errors),
                    })
                    continue
                out, err, rc = await _run_case(case)
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
                    output_errors = validate_schema(cap.output_schema, value)
                    ok = not output_errors
                expect = case.get("expect_output_contains")
                if expect is not None:
                    if value is None:
                        ok = False
                    else:
                        ok = ok and str(expect).lower() in json.dumps(value).lower()
                passed += 1 if ok else 0
                details.append({"case": i, "passed": ok,
                                "value": value, "rc": rc,
                                **({"error": "output contract: "
                                   + "; ".join(output_errors)}
                                   if output_errors else {}),
                                **({"stderr": err[-300:]} if err else {})})
            except (KeyError, OSError, TypeError, ValueError) as exc:
                details.append({"case": i, "passed": False, "error": str(exc)})

        output_schema_inferred = False
        if cap.output_schema is None and observed_values:
            # A generated capability that omitted an output contract still
            # gets a concrete model-facing contract from successful fixtures.
            # Infer it after execution so it describes the actual boundary.
            cap.output_schema = _schema_for_values(observed_values)
            output_schema_inferred = True

        total = len(details)
        cap.validation = {
            "tier": source_validation.tier.value,
            "cases_total": total, "cases_passed": passed,
            "all_passed": total > 0 and passed == total,
            "output_schema_inferred": output_schema_inferred,
            "source": source_record,
            "details": details,
        }
        cap.lifecycle_state = "VALIDATED" if cap.validation["all_passed"] else "REJECTED"
        return cap

    # ------------------------------------------------------------------
    # Registration into the live registry (ephemeral, task-scoped)
    # ------------------------------------------------------------------
    def _build_executor(
        self, cap: SyntheticCapability, *, proof_sink=None, candidate_sink=None,
    ):
        """Build the canonical executor closure for one validated record."""
        child = _child_code(repr(cap.code))

        class _Executor:
            def __init__(self, engine_ref, proof_sink_ref, candidate_sink_ref):
                self.engine = engine_ref
                self.proof_sink = proof_sink_ref
                self.candidate_sink = candidate_sink_ref

            descriptor = CapabilityDescriptor(
                id=cap.id,
                description=f"[synthetic] {cap.description} "
                            f"(validated {cap.validation.get('cases_passed', 0)}/"
                            f"{cap.validation.get('cases_total', 0)})",
                input_schema=cap.input_schema,
                output_schema=cap.output_schema,
                # The executable is sandboxed with this effective authority;
                # the generated declaration remains audit metadata only.
                effects=frozenset(EffectClass(effect) for effect in cap.effective_effects),
                origin=(CapabilityOrigin.PROJECT if cap.task_id is None
                        else CapabilityOrigin.GENERATED),
                version="0",
            )

            async def invoke(self, request, output_accumulator=None, context=None):
                # P1-18: enforce task scoping — a task-scoped synthetic must
                # not be callable by another task.
                if cap.task_id and request.task_id != cap.task_id:
                    return CapabilityResult(
                        request.call_id, request.capability_id,
                        CapabilityResultStatus.FAILED,
                        error=f"synthetic capability {cap.id} is scoped to "
                              f"task {cap.task_id}")
                async def _run():
                    payload = json.dumps(dict(request.arguments or {}))
                    workspace_root = (
                        context.workspace.root if context is not None else None
                    )
                    return await self.engine._run_child_async(
                        child,
                        payload,
                        timeout=30,
                        workspace_root=workspace_root,
                        effects=self.engine._authority_values(cap),
                    )

                stdout, stderr, returncode = await _run()
                ok = returncode == 0 and "__RESULT__" in stdout
                cap.uses += 1
                from athena.protocol.messages import utcnow
                cap.last_used_at = utcnow().isoformat()

                async def _persist_proof() -> str | None:
                    errors: list[str] = []
                    if self.proof_sink is None:
                        pass
                    else:
                        try:
                            await self.proof_sink(
                                cap.id, self.engine._proof_record(cap),
                            )
                        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                            # The execution result remains truthful, but a
                            # durable proof failure is surfaced in result metadata
                            # and logs instead of silently degrading auditability.
                            _logger.error(
                                "generated capability proof persistence failed for %s: %s",
                                cap.id, exc,
                            )
                            errors.append(str(exc))
                    if (
                        self.candidate_sink is not None
                        and cap.task_id
                        and cap.uses >= 2
                        and cap.successes >= 2
                    ):
                        cap.lifecycle_state = "CANDIDATE"
                        try:
                            await self.candidate_sink(
                                self.engine._generated_record(
                                    cap, scope=AffordanceScope.CANDIDATE,
                                )
                            )
                        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                            _logger.error(
                                "generated candidate persistence failed for %s: %s",
                                cap.id, exc,
                            )
                            errors.append(str(exc))
                    return "; ".join(errors) or None

                if ok:
                    try:
                        value = json.loads(
                            stdout.split("__RESULT__", 1)[1].splitlines()[0])
                    except (IndexError, json.JSONDecodeError) as exc:
                        cap.failures += 1
                        proof_error = await _persist_proof()
                        return CapabilityResult(
                            request.call_id, request.capability_id,
                            CapabilityResultStatus.FAILED,
                            error=f"synthetic returned invalid JSON: {exc}",
                            metadata=(
                                {"proof_persistence_error": proof_error}
                                if proof_error else {}
                            ),
                        )
                    if cap.output_schema is not None:
                        from athena.capabilities.registry import validate_schema
                        errors = validate_schema(cap.output_schema, value)
                        if errors:
                            cap.failures += 1
                            proof_error = await _persist_proof()
                            return CapabilityResult(
                                request.call_id, request.capability_id,
                                CapabilityResultStatus.FAILED,
                                error="generated output validation failed: "
                                      + "; ".join(errors),
                                metadata=(
                                    {"proof_persistence_error": proof_error}
                                    if proof_error else {}
                                ),
                            )
                    cap.successes += 1
                    proof_error = await _persist_proof()
                    return CapabilityResult(
                        request.call_id, request.capability_id,
                        CapabilityResultStatus.OK,
                        output=json.dumps(value),
                        metadata=(
                            {"proof_persistence_error": proof_error}
                            if proof_error else {}
                        ))
                cap.failures += 1
                proof_error = await _persist_proof()
                return CapabilityResult(
                    request.call_id, request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=(stderr or "synthetic failed")[-500:],
                    metadata=(
                        {"proof_persistence_error": proof_error}
                        if proof_error else {}
                    ))

        return _Executor(
            engine_ref=self,
            proof_sink_ref=proof_sink,
            candidate_sink_ref=candidate_sink,
        )

    @staticmethod
    def _proof_record(cap: SyntheticCapability) -> dict:
        live_quality = (
            cap.successes / cap.uses if cap.uses else 0.0
        )
        validation_quality = (
            cap.validation.get("cases_passed", 0)
            / cap.validation.get("cases_total", 1)
            if cap.validation.get("cases_total") else 0.0
        )
        return {
            **dict(cap.validation),
            "lifecycle_state": cap.lifecycle_state,
            "quality_score": round(
                (validation_quality + live_quality) / (2 if cap.uses else 1),
                4,
            ),
            "last_used_at": cap.last_used_at,
            "fixture_count": len(cap.validation_cases or []),
            "fixture_hashes": [
                hashlib.sha256(
                    json.dumps(case, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                for case in (cap.validation_cases or [])
            ],
            "usage": {
                "uses": cap.uses,
                "successes": cap.successes,
                "failures": cap.failures,
            },
        }

    @staticmethod
    def _dependency_lock(cap: SyntheticCapability) -> dict:
        """Return a reproducible identity for the generated dependency set.

        A generated record may carry additional environment metadata supplied
        by a caller, but the dependency fingerprint is always derived from
        the declared requirements rather than trusted input.  This makes a
        promoted capability explainable after restart and lets a later
        resolver detect that its dependency environment has changed.
        """
        requirements = [
            {
                "name": dependency.name,
                "manager": dependency.manager,
                "version": dependency.version,
                "reason": dependency.reason,
                "required_for": dependency.required_for,
            }
            for dependency in cap.required_dependencies
        ]
        encoded = json.dumps(
            requirements, sort_keys=True, separators=(",", ":")
        ).encode()
        return {
            **dict(cap.dependency_lock or {}),
            "format": 1,
            "requirements": requirements,
            "fingerprint": hashlib.sha256(encoded).hexdigest(),
        }

    def _generated_record(
        self, cap: SyntheticCapability, *, scope: AffordanceScope,
        project_scope: str | None = None, user_scope: str | None = None,
    ) -> GeneratedCapability:
        proof = self._proof_record(cap)
        return GeneratedCapability(
            id=cap.id,
            name=cap.name,
            description=cap.description,
            implementation=cap.code,
            input_schema=cap.input_schema,
            output_schema=cap.output_schema,
            declared_effects=frozenset(self._effect_values(cap)),
            effective_authority=frozenset(self._authority_values(cap)),
            required_dependencies=cap.required_dependencies,
            scope=scope,
            task_scope=(
                cap.task_id
                if scope in {AffordanceScope.TASK, AffordanceScope.CANDIDATE}
                else None
            ),
            project_scope=project_scope,
            user_scope=user_scope,
            provenance=cap.provenance,
            validation_state="VALIDATED",
            proof_record=proof,
            lifecycle_state=cap.lifecycle_state,
            supersedes=cap.supersedes,
            dependency_lock=self._dependency_lock(cap),
            last_used_at=cap.last_used_at,
            use_count=cap.uses,
            success_count=cap.successes,
            failure_count=cap.failures,
            quality_score=float(proof.get("quality_score") or 0.0),
        )

    def register_ephemeral(self, registry, cap: SyntheticCapability) -> bool:
        """Admit a VALIDATED synthetic capability through the normal path."""
        if not cap.validation.get("all_passed"):
            _logger.warning("refusing unvalidated synthetic %s", cap.name)
            return False

        proof_sink = getattr(registry, "update_generated_proof", None)
        candidate_sink = getattr(registry, "persist_generated_candidate", None)
        executor = self._build_executor(
            cap, proof_sink=proof_sink, candidate_sink=candidate_sink
        )
        generated = self._generated_record(
            cap,
            scope=AffordanceScope.TASK if cap.task_id
            else AffordanceScope.PROJECT,
        )
        if hasattr(registry, "register_task") and cap.task_id:
            registry.register_task(cap.task_id, executor, generated=generated)
        else:
            registry.register(executor)
        self._synthetic[cap.id] = cap
        self._executors[cap.id] = executor
        _logger.info("ephemeral capability registered: %s", cap.id)
        return True

    def restore_executor(self, generated: GeneratedCapability, *, proof_sink=None):
        """Rehydrate an already validated project/user capability.

        The persisted source is syntax/schema checked again before an executor
        is returned. Runtime execution still goes through the dispatcher and
        policy engine.
        """
        if generated.validation_state not in {"VALIDATED", "PROMOTED"}:
            raise ValueError("generated capability is not validated")
        from athena.capabilities.registry import _compile_validator

        tier = (
            ValidationTier.PROJECT
            if generated.scope is AffordanceScope.PROJECT
            else ValidationTier.USER
        )
        source_validation = self._source_validator.validate(
            generated.implementation, tier=tier,
        )
        if not source_validation.passed:
            failed = "; ".join(
                check.detail for check in source_validation.checks
                if check.status == "failed"
            )
            raise ValueError(
                f"persisted generated capability failed {tier.value} source checks: "
                f"{failed or 'unknown validation failure'}"
            )
        # The source hash is part of the persisted identity.  A formatter or
        # validator version change must not silently produce a different
        # executable from the same record during restart recovery.
        if source_validation.code != generated.implementation:
            raise ValueError(
                "persisted generated capability is not in canonical source format"
            )
        persisted_authority = frozenset(generated.effective_authority)
        if not persisted_authority.issubset(_GENERATED_EFFECTIVE_AUTHORITY):
            raise ValueError(
                "persisted generated capability requests authority outside "
                "the generated sandbox profile"
            )
        _compile_validator(generated.input_schema)
        if generated.output_schema is not None:
            _compile_validator(generated.output_schema)
        proof = dict(generated.proof_record)
        usage = dict(proof.pop("usage", {}))
        cap = SyntheticCapability(
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
            validation_cases=[],
            required_dependencies=generated.required_dependencies,
            uses=int(usage.get("uses", 0)),
            successes=int(usage.get("successes", generated.success_count)),
            failures=int(usage.get("failures", generated.failure_count)),
            # Stored metadata is checked above but is never the source of
            # runtime authority. Rehydration derives the envelope from the
            # current Athena profile so old records cannot widen execution.
            effective_effects=_GENERATED_EFFECTIVE_AUTHORITY,
            lifecycle_state=generated.lifecycle_state,
            supersedes=generated.supersedes,
            dependency_lock=dict(generated.dependency_lock),
            last_used_at=generated.last_used_at,
        )
        cap.uses = int(usage.get("uses", generated.use_count))
        executor = self._build_executor(cap, proof_sink=proof_sink)
        self._synthetic[cap.id] = cap
        self._executors[cap.id] = executor
        return executor

    def proof_for(self, cap_id: str) -> dict | None:
        """Proof-carrying summary for a synthetic capability."""
        cap = self._synthetic.get(cap_id)
        if cap is None:
            return None
        return {
            "id": cap.id,
            "validation": cap.validation,
            "uses": cap.uses,
            "successes": cap.successes,
            "failures": cap.failures,
            "provenance": cap.provenance,
            "effects": sorted(getattr(e, "value", str(e)) for e in cap.effects),
            "effective_authority": sorted(cap.effective_effects),
            "code_hash": hashlib.sha256(cap.code.encode()).hexdigest(),
            "lifecycle_state": cap.lifecycle_state,
            "quality_score": self._proof_record(cap).get("quality_score", 0.0),
            "last_used_at": cap.last_used_at,
            "supersedes": list(cap.supersedes),
            "dependency_lock": self._dependency_lock(cap),
        }

    def promote(self, surface, cap_id: str, *, scope: AffordanceScope,
                project_id: str | None = None, user_id: str = "athena") -> bool:
        """Explicitly promote validated task machinery to a wider overlay.

        Promotion is never implicit.  SYSTEM promotion is intentionally
        rejected: native/system changes belong to the normal release process.
        """
        if scope not in {AffordanceScope.PROJECT, AffordanceScope.USER}:
            raise ValueError("promotion target must be project or user")
        cap = self._synthetic.get(cap_id)
        executor = self._executors.get(cap_id)
        if cap is None or executor is None or not cap.validation.get("all_passed"):
            return False
        # Task admission is intentionally lightweight. Widening lifetime and
        # visibility requires the stricter source gate for the target scope;
        # promotion must never turn a task-only proof into a project/user
        # proof merely by changing metadata.
        promotion_tier = (
            ValidationTier.PROJECT if scope is AffordanceScope.PROJECT
            else ValidationTier.USER
        )
        if scope is AffordanceScope.PROJECT and not project_id:
            raise ValueError("project promotion requires project_id")
        if scope is AffordanceScope.USER and not user_id:
            raise ValueError("user promotion requires user_id")
        source_validation = self._source_validator.validate(
            cap.code, tier=promotion_tier
        )
        if not source_validation.passed:
            _logger.warning(
                "refusing promotion of %s after %s source checks",
                cap.id, promotion_tier.value,
            )
            return False
        cap.code = source_validation.code
        cap.lifecycle_state = "PROMOTED"
        cap.validation["tier"] = promotion_tier.value
        cap.validation["source"] = source_validation.to_dict()
        # Formatting is part of the canonical source contract. Rebuild the
        # executor if promotion normalized the source bytes.
        proof_sink = getattr(surface, "update_generated_proof", None)
        executor = self._build_executor(cap, proof_sink=proof_sink)
        source_task_id = cap.task_id
        proof = self._proof_record(cap)
        generated = GeneratedCapability(
            id=cap.id, name=cap.name, description=cap.description,
            implementation=cap.code, input_schema=cap.input_schema,
            output_schema=cap.output_schema,
            required_dependencies=cap.required_dependencies,
            declared_effects=frozenset(self._effect_values(cap)),
            effective_authority=frozenset(self._authority_values(cap)),
            scope=scope, project_scope=project_id,
            user_scope=user_id if scope is AffordanceScope.USER else None,
            provenance={**cap.provenance, "promoted_from": "task"},
            validation_state="PROMOTED", proof_record=proof,
            lifecycle_state="PROMOTED",
            supersedes=cap.supersedes,
            dependency_lock=self._dependency_lock(cap),
            use_count=cap.uses,
            success_count=cap.successes,
            failure_count=cap.failures,
            quality_score=float(proof.get("quality_score") or 0.0),
            last_used_at=cap.last_used_at,
        )
        if scope is AffordanceScope.PROJECT:
            surface.register_project(project_id, executor, generated=generated)
        else:
            surface.register_user(user_id, executor, generated=generated)
        # The executor's closure enforced the task owner until the explicit
        # promotion succeeded. Remove the old overlay before widening it.
        cap.task_id = None
        if source_task_id and hasattr(surface, "unregister_task_capability"):
            surface.unregister_task_capability(source_task_id, cap.id)
        return True

    def to_skill_candidate(self, cap_id: str):
        """Convert a repeatedly-successful synthetic into a SkillCandidate."""
        cap = self._synthetic.get(cap_id)
        if cap is None or cap.uses < 2 or cap.successes < cap.uses:
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


def _schema_for_values(values: list[object]) -> dict:
    """Infer a conservative JSON Schema from observed JSON results."""
    schemas = [_schema_for_value(value) for value in values]
    unique = {json.dumps(schema, sort_keys=True) for schema in schemas}
    if len(unique) == 1:
        return schemas[0]
    return {"anyOf": [schemas[index] for index in _first_schema_indexes(schemas)]}


def _first_schema_indexes(schemas: list[dict]) -> list[int]:
    seen: set[str] = set()
    indexes: list[int] = []
    for index, schema in enumerate(schemas):
        encoded = json.dumps(schema, sort_keys=True)
        if encoded not in seen:
            seen.add(encoded)
            indexes.append(index)
    return indexes


def _schema_for_value(value: object) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {
            "type": "array",
            "items": _schema_for_values(value) if value else {},
        }
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {
                str(key): _schema_for_values([item])
                for key, item in value.items()
            },
            "required": [str(key) for key in value],
            "additionalProperties": False,
        }
    return {}
