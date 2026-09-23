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

import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence

from athena.affordances.models import (
    DependencyRequirement,
    EvidenceDependency,
)
from athena.affordances.validation import GeneratedSourceValidator, ValidationTier
from athena.execution.process_tree import (  # noqa: F401 (patch seams)
    kill_tree,
    kill_tree_async,
    sandbox_argv,
    spawn_owned,
)
from athena.protocol.capabilities import (
    EffectClass,
)
from athena.protocol.failure import RecoveryDiagnostic
from athena.schema import validate_schema
from athena.protocol.resources import TaskResourceCloseResult
from athena.protocol.tasks import WorkspaceSpec
from athena.synthesis.models import (  # noqa: F401 — compatibility re-exports
    _GENERATED_EFFECTIVE_AUTHORITY,
    RegressionCase,
    SyntheticCapability,
)
from athena.synthesis.child_runtime import ChildRuntime, _namespace_python_paths  # noqa: F401 (patch-seam re-export)
from athena.synthesis.helpers import (  # noqa: F401 — compatibility re-exports
    _candidate_ready,
    _promotion_proof_error,
    _risk_tier,
    _schema_for_values,
    _service_negative_cases,
    canonical_generated_identity,
)
from athena.synthesis.promotion import Promotion
from athena.synthesis.validation import Validator
from athena.synthesis.proof_ledger import ProofLedger
from athena.synthesis.executor import GeneratedExecutor
from athena.synthesis.runtime import GeneratedToolHost, PersistentGeneratedSession

__all__ = [
    "RegressionCase",
    "SynthesisEngine",
    "SyntheticCapability",
    "canonical_generated_identity",
]

_logger = logging.getLogger("athena.synthesis")
_UNUSABLE_LIFECYCLE_STATES = frozenset(
    {
        "STALE",
        "DEGRADED",
        "REVALIDATION_REQUIRED",
        "REJECTED",
        "SUPERSEDED",
        "DEPRECATED",
    }
)

# Generated Python is an untrusted implementation, not an authority
# declaration.  It may compute over the task workspace inside the restricted
# runtime, but it cannot acquire process-spawn, write, delete, network, or


def _child_code(cap_code_repr: str, *, persistent: bool = False) -> str:
    """Build the sandboxed child-process program for one capability.

    ``athena`` is a deliberately tiny global API, backed by framed IPC. The
    generated source still has the strict ``run(args)`` contract; it does not
    receive a dispatcher or any host object directly.
    """
    execution = (
        (
            "while True:\n"
            "    raw = sys.stdin.readline()\n"
            "    if not raw:\n"
            "        break\n"
            "    try:\n"
            "        ARGS = json.loads(raw or '{}')\n"
            "        result = NS['run'](ARGS)\n"
            "        sys.stdout.write('__RESULT__' + json.dumps(result) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    except Exception as exc:\n"
            "        sys.stdout.write('__ERROR__' + json.dumps({'error': str(exc)}) + '\\n')\n"
            "        sys.stdout.flush()\n"
        )
        if persistent
        else (
            'ARGS = json.loads(sys.stdin.readline() or "{}")\n'
            'result = NS["run"](ARGS)\n'
            'sys.stdout.write("__RESULT__" + json.dumps(result) + "\\n")\n'
            "sys.stdout.flush()\n"
        )
    )
    return (
        "import json, sys\n"
        "class _GeneratedHost:\n"
        "    def call(self, capability_id, arguments):\n"
        "        request = {'capability_id': capability_id, 'arguments': arguments}\n"
        "        sys.stdout.write('__HOST__' + json.dumps(request) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "        response = sys.stdin.readline()\n"
        "        if not response:\n"
        "            raise RuntimeError('generated host closed without a response')\n"
        "        envelope = json.loads(response)\n"
        "        if not envelope.get('ok'):\n"
        "            failure = envelope.get('failure')\n"
        "            detail = json.dumps(failure, sort_keys=True) if failure else str(envelope.get('error') or 'host call failed')\n"
        "            raise RuntimeError('__ATHENA_FAILURE__' + detail)\n"
        "        return envelope.get('value')\n"
        "NS = {}\n"
        "NS['athena'] = _GeneratedHost()\n"
        f"exec({cap_code_repr}, NS)\n" + execution
    )


def _generated_failure(
    cap: SyntheticCapability,
    failure_class: str,
    *,
    repairable: bool,
    evidence: dict | None = None,
) -> dict[str, object]:
    """Expose a bounded repair signal without granting repair authority."""
    recovery_action = (
        "source_repair"
        if repairable
        else {
            "environment_changed": "dependency_refresh_and_revalidation",
            "provenance_stale": "evidence_reacquisition_and_revalidation",
            "governance_failure": "request_authority_or_fail",
            "missing_authority": "request_authority_or_fail",
        }.get(failure_class, "inspect_and_revalidate")
    )
    return {
        "capability_id": cap.id,
        "code_hash": hashlib.sha256(cap.code.encode()).hexdigest(),
        "failure_class": failure_class,
        "repairable": repairable,
        "repair_operation": "synthesis.repair" if repairable else None,
        "recovery_action": recovery_action,
        "diagnostic": RecoveryDiagnostic(
            operation=cap.id,
            failure_class=failure_class,
            permitted_recovery=(recovery_action,),
            evidence=evidence or {},
        ).as_dict(),
        "evidence": evidence or {},
    }


def _failure_lifecycle_state(failure_class: str) -> str:
    """Return the service-owned state for a live generated-tool failure."""
    if failure_class in {"environment_changed", "dependency_changed", "provenance_stale"}:
        return "REVALIDATION_REQUIRED"
    return "DEGRADED"


def _remember_live_failure(
    cap: SyntheticCapability,
    arguments: Mapping | None,
    *,
    failure_class: str,
    observed_failure: str,
    environment_fingerprint: str | None = None,
    behavioral_oracle: Mapping[str, object] | None = None,
) -> None:
    """Retain repairable live failures as deterministic regression inputs."""
    raw_input = dict(arguments or {})
    expected_contract = {
        "input_schema": dict(cap.input_schema),
        "output_schema": dict(cap.output_schema or {}),
        "schema_hash": hashlib.sha256(
            json.dumps(
                {"input": cap.input_schema, "output": cap.output_schema or {}},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    identity = {
        "capability_family": cap.family_id,
        "revision_first_seen": cap.revision,
        "input": raw_input,
        "environment_fingerprint": environment_fingerprint,
        "expected_contract": expected_contract,
        "failure_class": failure_class,
        "observed_failure": observed_failure[-1000:],
        "behavioral_oracle": dict(behavioral_oracle or {}),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    case = RegressionCase(
        id=f"regression:{fingerprint[:24]}",
        capability_family=cap.family_id,
        revision_first_seen=cap.revision,
        source="live_failure",
        input=raw_input,
        environment_fingerprint=environment_fingerprint,
        expected_contract=expected_contract,
        observed_failure=observed_failure[-1000:],
        failure_class=failure_class,
        behavioral_oracle=behavioral_oracle,
        original_failure_reproduced=True,
        last_replay={
            "revision": cap.revision,
            "reproduced": True,
            "status": "original_failure_observed",
        },
    ).to_record()
    for key in ("live_failure_cases", "regression_cases"):
        cases = cap.validation.setdefault(key, [])
        if not any(str(existing.get("id")) == case["id"] for existing in cases):
            cases.append(dict(case))
            del cases[:-128]


def _admit_generated_input(cap: SyntheticCapability, arguments: object) -> list[str]:
    """Apply the same input admission boundary used by live executors."""
    return validate_schema(cap.input_schema, arguments)


class SynthesisEngine:
    """Registers temporary capabilities born from execution traces."""

    def __init__(
        self,
        *,
        restricted_env: bool = True,
        source_validator: GeneratedSourceValidator | None = None,
        dispatcher=None,
        research_store=None,
    ) -> None:
        self._restricted_env = restricted_env
        self._source_validator = source_validator or GeneratedSourceValidator()
        self._synthetic: dict[str, SyntheticCapability] = {}
        self._executors: dict[str, object] = {}
        self._dispatcher = dispatcher
        self._research_store = research_store
        self._proof_sink = None
        # A successful generated call remains pending until a canonical
        # verification event proves the task outcome.  This is deliberately
        # event-derived: callers cannot submit a verification count as
        # metadata.
        self._pending_verification_calls: dict[str, dict[str, str]] = {}
        self._persistent_sessions: dict[
            tuple[str, str, str], tuple[PersistentGeneratedSession, str, bool]
        ] = {}

    def bind_dispatcher(self, dispatcher) -> None:
        """Bind the canonical dispatcher used by generated host calls."""
        self._dispatcher = dispatcher

    def bind_research_store(self, research_store) -> None:
        """Bind the evidence revision source used by live executors."""
        self._research_store = research_store

    def bind_proof_sink(self, proof_sink) -> None:
        """Bind durable proof persistence for event-derived metrics."""
        self._proof_sink = proof_sink

    async def observe_event(self, event) -> None:
        await ProofLedger(self).observe_event(event)

    def _apply_proof_event(self, event):
        return ProofLedger(self)._apply_proof_event(event)

    async def replay_event_metrics(self, events):
        return await ProofLedger(self).replay_event_metrics(events)

    async def _persist_observed_proof(self, cap) -> None:
        await ProofLedger(self)._persist_observed_proof(cap)

    async def evidence_status(self, cap, research):
        return await ProofLedger(self).evidence_status(cap, research)

    def _child_env(self, python_paths: Sequence[str] = ()) -> dict:
        return ChildRuntime(self)._child_env(python_paths)

    @staticmethod
    def _effect_values(cap: SyntheticCapability) -> set[str]:
        return {getattr(effect, "value", str(effect)) for effect in cap.effects}

    @staticmethod
    def _authority_values(cap: SyntheticCapability) -> set[str]:
        return set(cap.effective_effects)

    def _run_child(
        self, child, payload, *, timeout, workspace_root=None, effects=None, python_paths=()
    ):
        return ChildRuntime(self)._run_child(
            child,
            payload,
            timeout=timeout,
            workspace_root=workspace_root,
            effects=effects,
            python_paths=python_paths,
        )

    async def _run_child_async(
        self,
        child,
        payload,
        *,
        timeout,
        workspace_root=None,
        effects=None,
        python_paths=(),
        host=None,
    ):
        return await ChildRuntime(self)._run_child_async(
            child,
            payload,
            timeout=timeout,
            workspace_root=workspace_root,
            effects=effects,
            python_paths=python_paths,
            host=host,
        )

    async def _run_persistent_child_async(
        self, child, payload, *, timeout, workspace_root, effects, python_paths, host, session_key
    ):
        return await ChildRuntime(self)._run_persistent_child_async(
            child,
            payload,
            timeout=timeout,
            workspace_root=workspace_root,
            effects=effects,
            python_paths=python_paths,
            host=host,
            session_key=session_key,
        )

    async def close_persistent_sessions(self) -> None:
        await ChildRuntime(self).close_persistent_sessions()

    async def close_persistent_sessions_for_task(self, task_id: str):
        return await ChildRuntime(self).close_persistent_sessions_for_task(task_id)

    async def _close_persistent_handles(self, handles) -> TaskResourceCloseResult:
        return await ChildRuntime(self)._close_persistent_handles(handles)

    async def _run_generated_child(
        self,
        *,
        cap,
        child,
        payload,
        timeout,
        workspace_root,
        effects,
        python_paths,
        context,
        request,
    ):
        return await ChildRuntime(self)._run_generated_child(
            cap=cap,
            child=child,
            payload=payload,
            timeout=timeout,
            workspace_root=workspace_root,
            effects=effects,
            python_paths=python_paths,
            context=context,
            request=request,
        )

    @staticmethod
    async def _communicate_with_host(proc, payload: str, host: GeneratedToolHost):
        return await ChildRuntime._communicate_with_host(proc, payload, host)

    def synthesize(
        self,
        *,
        capability_id: str | None = None,
        name: str,
        description: str,
        code: str,
        runtime: str = "python",
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        effects: set | frozenset | None = None,
        task_id: str | None = None,
        provenance: dict | None = None,
        validation_cases: list[dict] | None = None,
        required_dependencies: tuple[DependencyRequirement, ...] = (),
        required_capabilities: tuple[str, ...] = (),
        evidence_dependencies: tuple[EvidenceDependency, ...] = (),
        family_id: str | None = None,
        revision: int = 1,
        parent_revision: int | None = None,
        active_revision: int | None = None,
        supersedes: tuple[str, ...] = (),
        superseded_by: str | None = None,
    ) -> SyntheticCapability:
        """Create (but do not yet trust) a synthetic capability.

        ``validation_cases`` are [{"args": {...}, "expect_output_contains": ...}]
        executed in a subprocess sandbox before the capability becomes callable.
        """
        if runtime not in {"python", "python_persistent"}:
            raise ValueError(f"unsupported generated runtime: {runtime}")
        cap = SyntheticCapability(
            id=capability_id or f"synth_{name}",
            name=name,
            description=description,
            code=code,
            input_schema=(
                input_schema if input_schema is not None else {"type": "object", "properties": {}}
            ),
            output_schema=output_schema,
            effects=frozenset(effects or {EffectClass.READ_LOCAL}),
            task_id=task_id,
            provenance=provenance or {},
            validation={},
            runtime=runtime,
            validation_cases=[dict(case) for case in validation_cases or []],
            required_dependencies=required_dependencies,
            required_capabilities=tuple(sorted(set(required_capabilities))),
            evidence_dependencies=tuple(evidence_dependencies),
            family_id=family_id or str((provenance or {}).get("family_id") or f"generated:{name}"),
            revision=revision,
            parent_revision=parent_revision,
            active_revision=active_revision,
            supersedes=tuple(str(item) for item in supersedes),
            superseded_by=superseded_by,
            effective_effects=_GENERATED_EFFECTIVE_AUTHORITY,
            id_generated=capability_id is None,
        )
        if capability_id is None:
            cap.id = (
                "synth_"
                + hashlib.sha256(canonical_generated_identity(cap).encode()).hexdigest()[:20]
            )
        return cap

    async def validate(
        self,
        cap: SyntheticCapability,
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
    ):
        return await Validator(self).validate(
            cap,
            cases,
            timeout=timeout,
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
        )

    async def _emit_validation_progress(
        self,
        *,
        task_id: str | None,
        capability_id: str,
        completed: int,
        total: int,
    ) -> None:
        await Validator(self)._emit_validation_progress(
            task_id=task_id,
            capability_id=capability_id,
            completed=completed,
            total=total,
        )

    @staticmethod
    def _dependency_paths(
        requirements: Sequence[DependencyRequirement],
        workspace_root: str | None,
        *,
        expected_fingerprint: str | None,
    ) -> tuple[str, ...]:
        if not requirements:
            return ()
        if not workspace_root:
            raise ValueError("generated capability dependencies require a workspace context")
        from athena.execution.dependencies import resolve_dependency_environment

        environment = resolve_dependency_environment(
            workspace_root,
            requirements,
            expected_fingerprint=expected_fingerprint,
        )
        return environment.python_path

    @staticmethod
    def _dependency_metadata(
        requirements: Sequence[DependencyRequirement],
        workspace_root: str | None,
    ) -> dict:
        if not requirements:
            return {}
        if not workspace_root:
            raise ValueError("generated capability dependencies require a workspace context")
        from athena.execution.dependencies import resolve_dependency_environment

        environment = resolve_dependency_environment(workspace_root, requirements)
        return environment.to_metadata()

    # ------------------------------------------------------------------
    # Registration into the live registry (ephemeral, task-scoped)
    # ------------------------------------------------------------------
    def _build_executor(
        self,
        cap: SyntheticCapability,
        *,
        proof_sink=None,
        candidate_sink=None,
    ):
        """Build the canonical executor closure for one validated record."""
        child = _child_code(
            repr(cap.code),
            persistent=cap.runtime == "python_persistent",
        )
        runtime_effects = self._runtime_effective_effects(cap)
        return GeneratedExecutor(
            self,
            cap,
            child=child,
            runtime_effects=runtime_effects,
            proof_sink=proof_sink,
            candidate_sink=candidate_sink,
        )

    def _runtime_effective_effects(
        self,
        cap: SyntheticCapability,
    ) -> frozenset[str]:
        """Build a mediated host-call ceiling from declared effects.

        Generated Python itself retains the fixed sandbox authority. The
        additional effects below authorize only calls made through native
        capabilities, and only when synthesis declared those effects.
        """
        effects = set(_GENERATED_EFFECTIVE_AUTHORITY)
        declared = self._effect_values(cap)
        registry = getattr(self._dispatcher, "registry", None)
        if registry is None:
            return frozenset(effects)
        for capability_id in cap.required_capabilities:
            try:
                descriptor = registry.resolve(capability_id)
            except (KeyError, RuntimeError, TypeError, ValueError):
                continue
            for effect in getattr(descriptor, "effects", ()):
                value = getattr(effect, "value", str(effect))
                if value in declared:
                    effects.add(value)
        return frozenset(effects)

    @staticmethod
    def _environment_signature(context, cap):
        return Promotion._environment_signature(context, cap)

    @staticmethod
    def _proof_record(cap):
        return ProofLedger._proof_record(cap)

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
        encoded = json.dumps(requirements, sort_keys=True, separators=(",", ":")).encode()
        return {
            **dict(cap.dependency_lock or {}),
            "format": 1,
            "requirements": requirements,
            "fingerprint": hashlib.sha256(encoded).hexdigest(),
        }

    def generated_record(self, cap, *, scope, project_scope=None, user_scope=None):
        """Public synthesis-domain operation: build the durable capability record."""
        return self._generated_record(
            cap, scope=scope, project_scope=project_scope, user_scope=user_scope
        )

    def build_executor(self, cap, **kwargs):
        """Public synthesis-domain operation: build an executor for a validated capability."""
        return self._build_executor(cap, **kwargs)

    def _generated_record(self, cap, *, scope, project_scope=None, user_scope=None):
        return Promotion(self)._generated_record(
            cap, scope=scope, project_scope=project_scope, user_scope=user_scope
        )

    def register_ephemeral(self, registry, cap) -> bool:
        return Promotion(self).register_ephemeral(registry, cap)

    def restore_executor(self, generated, *, proof_sink=None, workspace_root=None):
        return Promotion(self).restore_executor(
            generated, proof_sink=proof_sink, workspace_root=workspace_root
        )

    def proof_for(self, capability_id):
        return ProofLedger(self).proof_for(capability_id)

    def synthetic_for(self, cap_id: str) -> SyntheticCapability | None:
        """Return the in-memory capability record for lifecycle operations."""
        return self._synthetic.get(cap_id)

    async def promote(
        self,
        surface,
        cap_id: str,
        *,
        scope,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await Promotion(self).promote(
            surface,
            cap_id,
            scope=scope,
            project_id=project_id,
            user_id=user_id,
        )

    def to_skill_candidate(self, cap_id):
        return Promotion(self).to_skill_candidate(cap_id)


_MISSING = object()


def _input_signature(arguments: object) -> str:
    """Return a stable bounded identity for live-use diversity evidence."""
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _apply_workspace_fixture(case: Mapping[str, object], root: str) -> None:
    """Materialize bounded fixture files inside one disposable workspace."""
    fixture = case.get("workspace_files")
    if fixture is None:
        # ``workspace`` is accepted as a compact alias for callers that use
        # the validation vocabulary directly. It is never the live workspace.
        fixture = case.get("workspace")
    if fixture is None:
        fixture = case.get("workspace_fixture")
    if fixture is None:
        return
    if not isinstance(fixture, Mapping):
        raise TypeError("workspace_files must be an object mapping paths to content")
    root_real = os.path.realpath(os.path.abspath(root))
    for raw_path, content in fixture.items():
        path = str(raw_path)
        if os.path.isabs(path):
            raise ValueError(f"workspace fixture path must be relative: {path}")
        normalized = os.path.normpath(path)
        if normalized in {"", ".", ".."} or normalized.startswith(".." + os.sep):
            raise ValueError(f"workspace fixture path escapes workspace: {path}")
        target = os.path.realpath(os.path.join(root_real, normalized))
        if target != root_real and not target.startswith(root_real + os.sep):
            raise ValueError(f"workspace fixture path escapes workspace: {path}")
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)


def _check_effect_expectations(
    case: Mapping[str, object],
    host: GeneratedToolHost | None,
) -> str | None:
    """Check which governed native calls a validation fixture exercised."""
    raw = case.get("expect_effects", case.get("expect_effect", _MISSING))
    forbidden = case.get("expect_no_effects")
    if raw is not _MISSING and host is None:
        return "effect expectations require a governed host context"
    calls = list(host.calls) if host is not None else []
    if raw is not _MISSING:
        expected = raw if isinstance(raw, (list, tuple)) else [raw]
        for item in expected:
            if isinstance(item, str):
                matched = any(call.get("capability_id") == item for call in calls)
            elif isinstance(item, Mapping):
                capability = item.get("capability_id") or item.get("capability")
                operation = item.get("operation")
                matched = any(
                    (not capability or call.get("capability_id") == capability)
                    and (
                        operation is None or call.get("arguments", {}).get("operation") == operation
                    )
                    for call in calls
                )
            else:
                matched = False
            if not matched:
                return f"expected governed effect was not observed: {item}"
    if forbidden is not None:
        values = forbidden if isinstance(forbidden, (list, tuple)) else [forbidden]
        for item in values:
            if any(
                call.get("capability_id") == item
                or (
                    isinstance(item, Mapping)
                    and call.get("capability_id")
                    == (item.get("capability_id") or item.get("capability"))
                    and (
                        item.get("operation") is None
                        or call.get("arguments", {}).get("operation") == item.get("operation")
                    )
                )
                for call in calls
            ):
                return f"forbidden governed effect was observed: {item}"
    return None


def _workspace_snapshot(root: str | None) -> dict[str, str] | None:
    if root is None:
        return None
    snapshot: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in {".git", "__pycache__"})
        for name in sorted(filenames):
            path = os.path.join(directory, name)
            try:
                data = open(path, "rb").read(2_000_000)
            except OSError:
                continue
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            snapshot[relative] = hashlib.sha256(data).hexdigest()
    return snapshot


def _check_resource_expectations(
    case: Mapping[str, object],
    root: str | None,
    before: dict[str, str] | None,
) -> tuple[str | None, list[str]]:
    expected_changed = case.get("changed_resources", case.get("expected_changed_resources"))
    expected_unchanged = case.get("unchanged_resources", case.get("expected_unchanged_resources"))
    if expected_changed is None and expected_unchanged is None:
        return None, []
    if root is None or before is None:
        return "resource expectations require a workspace fixture", []
    after = _workspace_snapshot(root) or {}
    changed = sorted(
        {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    )
    if expected_changed is not None:
        expected = sorted(
            str(value)
            for value in (
                expected_changed
                if isinstance(expected_changed, (list, tuple))
                else [expected_changed]
            )
        )
        if changed != expected:
            return (
                f"expected changed resources {expected}, observed {changed}",
                changed,
            )
    if expected_unchanged is not None:
        unchanged = [
            str(value)
            for value in (
                expected_unchanged
                if isinstance(expected_unchanged, (list, tuple))
                else [expected_unchanged]
            )
        ]
        violated = sorted(path for path in unchanged if path in changed)
        if violated:
            return f"forbidden resource changes: {violated}", changed
    return None, changed


async def _check_invariants(
    case: Mapping[str, object],
    host: GeneratedToolHost | None,
) -> str | None:
    """Run postconditions through the same host boundary as the tool."""
    raw = case.get("invariants") or ()
    if not raw:
        return None
    if host is None:
        return "invariants require a governed host context"
    if not isinstance(raw, (list, tuple)):
        return "invariants must be a list"
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            return f"invariant {index} must be an object"
        capability_id = str(
            item.get("capability_id")
            or item.get("capability")
            or ("execute" if item.get("command") else "")
        )
        arguments = dict(item.get("arguments") or item.get("args") or {})
        if item.get("command") is not None:
            arguments = {
                "language": str(item.get("language") or "shell"),
                "code": str(item["command"]),
                **arguments,
            }
        if not capability_id:
            return f"invariant {index} has no capability_id or command"
        try:
            value = await host.call(capability_id, arguments)
        except Exception as exc:  # noqa: BLE001 - fixture failure is evidence
            return f"invariant {index} failed: {exc}"
        expected = item.get("expect_output", _MISSING)
        if expected is not _MISSING and value != expected:
            return f"invariant {index} output mismatch"
        contains = item.get("expect_output_contains")
        if (
            contains is not None
            and str(contains).casefold() not in json.dumps(value, sort_keys=True).casefold()
        ):
            return f"invariant {index} output did not contain {contains!r}"
    return None


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
            "properties": {str(key): _schema_for_values([item]) for key, item in value.items()},
            "required": [str(key) for key in value],
            "additionalProperties": False,
        }
    return {}
