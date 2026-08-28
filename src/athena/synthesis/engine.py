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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from athena.affordances.models import (
    AffordanceScope,
    DependencyRequirement,
    EvidenceDependency,
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
from athena.protocol.events import EV, Event
from athena.protocol.ids import new_id
from athena.protocol.tasks import MutationMode, WorkspaceSpec
from athena.synthesis.runtime import GeneratedToolHost, PersistentGeneratedSession

__all__ = ["SynthesisEngine", "SyntheticCapability"]

_logger = logging.getLogger("athena.synthesis")

# Generated Python is an untrusted implementation, not an authority
# declaration.  It may compute over the task workspace inside the restricted
# runtime, but it cannot acquire process-spawn, write, delete, network, or
# privileged authority merely by naming those effects in its metadata.  A
# generated implementation that needs those operations must request the
# corresponding native capability through the normal dispatcher.
_GENERATED_EFFECTIVE_AUTHORITY = frozenset(
    {
        "READ_LOCAL",
        "EXECUTE",
    }
)


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
            'print("__RESULT__" + json.dumps(result))\n'
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
        "            raise RuntimeError(str(envelope.get('error') or 'host call failed'))\n"
        "        return envelope.get('value')\n"
        "NS = {}\n"
        "NS['athena'] = _GeneratedHost()\n"
        f"exec({cap_code_repr}, NS)\n" + execution
    )


def _namespace_python_paths(paths: Sequence[str], root: str) -> tuple[str, ...]:
    """Map host workspace paths to the sandbox's ``/workspace`` mount."""
    root_abs = os.path.realpath(os.path.abspath(root))
    mapped: list[str] = []
    for path in paths:
        path_abs = os.path.realpath(os.path.abspath(path))
        if path_abs == root_abs or path_abs.startswith(root_abs + os.sep):
            mapped.append("/workspace" + path_abs[len(root_abs) :])
        else:
            raise ValueError("dependency import path escaped workspace")
    return tuple(mapped)


@dataclass
class SyntheticCapability:
    """A generated, validated, task-scoped executable capability."""

    id: str
    name: str
    description: str
    code: str  # python source defining `def run(args)`
    input_schema: dict
    effects: frozenset  # declared effect envelope
    task_id: str | None
    provenance: dict  # originating task/call ids
    validation: dict  # test results from sandbox run
    runtime: str = "python"
    uses: int = 0
    successes: int = 0
    failures: int = 0
    validation_cases: list[dict] | None = None
    required_dependencies: tuple[DependencyRequirement, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    evidence_dependencies: tuple[EvidenceDependency, ...] = ()
    input_signatures: set[str] = field(default_factory=set)
    task_context_signatures: set[str] = field(default_factory=set)
    environment_fingerprints: set[str] = field(default_factory=set)
    reuse_count: int = 0
    downstream_verifications: int = 0
    latency_saved_ms: float = 0.0
    turns_saved: int = 0
    # This is calculated by Athena's sandbox contract, not trusted from the
    # generated source or its declared effects.
    effective_effects: frozenset[str] = _GENERATED_EFFECTIVE_AUTHORITY
    output_schema: dict | None = None
    lifecycle_state: str = "DRAFT"
    supersedes: tuple[str, ...] = ()
    dependency_lock: dict = field(default_factory=dict)
    last_used_at: str | None = None


def _generated_failure(
    cap: SyntheticCapability,
    failure_class: str,
    *,
    repairable: bool,
    evidence: dict | None = None,
) -> dict[str, object]:
    """Expose a bounded repair signal without granting repair authority."""
    return {
        "capability_id": cap.id,
        "code_hash": hashlib.sha256(cap.code.encode()).hexdigest(),
        "failure_class": failure_class,
        "repairable": repairable,
        "repair_operation": "synthesis.repair" if repairable else None,
        "evidence": evidence or {},
    }


def _candidate_ready(cap: SyntheticCapability) -> bool:
    """Require meaningful live evidence before retaining a candidate."""
    return bool(
        cap.validation.get("all_passed") is True
        and cap.uses >= 3
        and cap.successes >= 3
        and cap.failures == 0
        and len(cap.input_signatures) >= 2
        and len(cap.task_context_signatures) >= 1
    )


def _promotion_proof_error(
    cap: SyntheticCapability,
    tier: ValidationTier,
) -> str | None:
    """Return the missing proof required to widen a capability's lifetime."""
    if not _candidate_ready(cap):
        return "diverse live proof is incomplete"
    if cap.validation.get("tier") != tier.value:
        return f"behavioral validation at {tier.value} tier is required"
    if cap.validation.get("all_passed") is not True:
        return "target-tier behavioral validation did not pass"
    if cap.failures:
        return "unresolved live failures remain"
    if (
        tier in {ValidationTier.PROJECT, ValidationTier.USER}
        and len(cap.task_context_signatures) < 2
    ):
        return f"{tier.value} promotion requires two distinct task contexts"
    if tier is ValidationTier.USER and len(cap.environment_fingerprints) < 2:
        return "user promotion requires portability across two environments"
    return None


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

    async def observe_event(self, event: Event) -> None:
        """Consume canonical events that prove generated-tool usefulness.

        ``downstream_verifications`` is only incremented when a generated
        capability completed successfully and the same task later emits a
        passing ``VerificationCompleted`` event.  A model result, caller
        metadata, or synthetic counter cannot manufacture this proof.
        """
        changed = self._apply_proof_event(event)
        for cap in changed:
            await self._persist_observed_proof(cap)

    def _apply_proof_event(self, event: Event) -> tuple[SyntheticCapability, ...]:
        task_id = event.task_id
        if not task_id:
            return ()
        payload = dict(event.payload or {})
        if event.type == EV["CAPABILITY_COMPLETED"]:
            capability_id = str(payload.get("capability_id") or "")
            call_id = str(payload.get("call_id") or "")
            if capability_id in self._synthetic and call_id:
                self._pending_verification_calls.setdefault(task_id, {})[call_id] = capability_id
            return ()
        if event.type != EV["VERIFICATION_COMPLETED"] or not payload.get("passed"):
            return ()
        pending = self._pending_verification_calls.pop(task_id, {})
        changed: list[SyntheticCapability] = []
        for capability_id in pending.values():
            cap = self._synthetic.get(capability_id)
            if cap is None:
                continue
            cap.downstream_verifications += 1
            if cap not in changed:
                changed.append(cap)
        return tuple(changed)

    async def replay_event_metrics(self, events) -> None:
        """Rebuild verification metrics from the durable event stream.

        Promoted capabilities survive restart, but the in-memory pending-call
        map does not. Replaying the canonical log restores the metric without
        trusting the previously serialized counter as authority.
        """
        if events is None or not self._synthetic:
            return
        for cap in self._synthetic.values():
            cap.downstream_verifications = 0
        self._pending_verification_calls.clear()
        rowid = 0
        changed: dict[str, SyntheticCapability] = {}
        while True:
            batch = await events.list_recent(after_rowid=rowid, limit=500)
            if not batch:
                break
            for event in batch:
                for cap in self._apply_proof_event(event):
                    changed[cap.id] = cap
                rowid = max(rowid, int(getattr(event, "_rowid", rowid)))
            if len(batch) < 500:
                break
        for cap in changed.values():
            await self._persist_observed_proof(cap)

    async def _persist_observed_proof(self, cap: SyntheticCapability) -> None:
        if self._proof_sink is None:
            return
        try:
            await self._proof_sink(cap.id, self._proof_record(cap))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning(
                "could not persist event-derived proof for %s: %s",
                cap.id,
                exc,
            )

    async def evidence_status(
        self,
        capability: SyntheticCapability | GeneratedCapability,
        research_store,
    ) -> dict:
        """Check the research revisions a generated capability relies on.

        A source/evidence reference is a proof dependency, not an execution
        permission.  The check is deliberately conservative: a missing
        source/evidence object or a changed captured source hash makes the
        capability stale and therefore unavailable until it is revalidated.
        """
        dependencies = tuple(getattr(capability, "evidence_dependencies", ()) or ())
        if not dependencies:
            return {"status": "CURRENT", "dependencies": []}
        if research_store is None:
            return {
                "status": "REVALIDATION_REQUIRED",
                "dependencies": [
                    {"requirement": dependency.requirement, "status": "research_store_unavailable"}
                    for dependency in dependencies
                ],
            }
        checks: list[dict] = []
        stale = False
        for dependency in dependencies:
            source_id = dependency.source_id
            evidence = None
            if dependency.evidence_id:
                evidence = await research_store.get_evidence(dependency.evidence_id)
                if evidence is None:
                    checks.append(
                        {
                            **dependency.to_record(),
                            "status": "missing_evidence",
                        }
                    )
                    stale = True
                    continue
                if source_id is None:
                    source_id = evidence.source_id
                elif evidence.source_id != source_id:
                    checks.append(
                        {
                            **dependency.to_record(),
                            "status": "evidence_source_mismatch",
                        }
                    )
                    stale = True
                    continue
            source = await research_store.get_source(source_id) if source_id else None
            if source is None:
                checks.append(
                    {
                        **dependency.to_record(),
                        "status": "missing_source",
                    }
                )
                stale = True
                continue
            actual_hash = source.content_hash
            expected_hash = dependency.content_hash
            if expected_hash and actual_hash != expected_hash:
                checks.append(
                    {
                        **dependency.to_record(),
                        "status": "source_revision_changed",
                        "actual_content_hash": actual_hash,
                    }
                )
                stale = True
                continue
            if expected_hash:
                latest = await research_store.latest_source_for_uri(source.canonical_uri)
                if (
                    latest is not None
                    and latest.id != source.id
                    and latest.content_hash != expected_hash
                ):
                    checks.append(
                        {
                            **dependency.to_record(),
                            "source_id": source.id,
                            "status": "source_revision_changed",
                            "actual_content_hash": latest.content_hash,
                            "latest_source_id": latest.id,
                        }
                    )
                    stale = True
                    continue
            checks.append(
                {
                    **dependency.to_record(),
                    "source_id": source.id,
                    "actual_content_hash": actual_hash,
                    "status": "current",
                }
            )
        return {
            "status": "STALE" if stale else "CURRENT",
            "dependencies": checks,
        }

    def _child_env(self, python_paths: Sequence[str] = ()) -> dict:
        if not self._restricted_env:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        else:
            allowed = ("PATH", "PYTHONIOENCODING", "LANG", "LC_ALL", "TMPDIR")
            env = {k: os.environ[k] for k in allowed if k in os.environ}
            env["PYTHONIOENCODING"] = "utf-8"
        if python_paths:
            env["PYTHONPATH"] = os.pathsep.join(python_paths)
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
        python_paths: Sequence[str] = (),
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
                env=self._child_env(python_paths),
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
        python_paths: Sequence[str] = (),
        host: GeneratedToolHost | None = None,
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
                env=self._child_env(_namespace_python_paths(python_paths, root)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                if host is None:
                    # ``athena.call`` is still injected so source validation
                    # and execution use one stable contract.  Without a
                    # parent host, answer the first mediated request with a
                    # deterministic failure instead of leaving the child
                    # blocked on stdin until the execution timeout.
                    unavailable = json.dumps(
                        {
                            "ok": False,
                            "error": "generated host is unavailable in this context",
                        }
                    )
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate((payload + "\n" + unavailable + "\n").encode()),
                        timeout=timeout,
                    )
                else:
                    stdout, stderr = await asyncio.wait_for(
                        self._communicate_with_host(proc, payload, host),
                        timeout=timeout,
                    )
            except TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                return (
                    stdout.decode("utf-8", errors="replace"),
                    stderr.decode("utf-8", errors="replace") or "synthetic execution timed out",
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

    async def _run_persistent_child_async(
        self,
        child: str,
        payload: str,
        *,
        timeout: float,
        workspace_root: str | None,
        effects: set[str] | None,
        python_paths: Sequence[str],
        host: GeneratedToolHost | None,
        session_key: tuple[str, str, str],
    ) -> tuple[str, str, int]:
        """Run one call on the task/workspace-scoped generated process."""
        handle = self._persistent_sessions.get(session_key)
        if handle is None or handle[0].closed:
            if handle is not None:
                await handle[0].close()
                if handle[2]:
                    shutil.rmtree(handle[1], ignore_errors=True)
            owned_root = workspace_root is None
            root = workspace_root or tempfile.mkdtemp(prefix="athena-synth-persistent-")
            values = effects or set()
            writable = bool({"WRITE_LOCAL", "DELETE"} & values)
            network = "allow" if {"NETWORK_READ", "NETWORK_WRITE"} & values else "deny"
            try:
                session = PersistentGeneratedSession(
                    sandbox_argv(
                        [sys.executable, "-c", child],
                        root=root,
                        network_policy=network,
                        writable=writable,
                    ),
                    env=self._child_env(_namespace_python_paths(python_paths, root)),
                )
                await session.start()
            except BaseException:
                if owned_root:
                    shutil.rmtree(root, ignore_errors=True)
                raise
            handle = (session, root, owned_root)
            self._persistent_sessions[session_key] = handle
        result = await handle[0].invoke(payload, host, timeout=timeout)
        if handle[0].closed:
            self._persistent_sessions.pop(session_key, None)
            if handle[2]:
                shutil.rmtree(handle[1], ignore_errors=True)
        return result

    async def close_persistent_sessions(self) -> None:
        """Stop all generated persistent runtimes during service shutdown."""
        handles = tuple(self._persistent_sessions.items())
        self._persistent_sessions.clear()
        await self._close_persistent_handles(handles)

    async def close_persistent_sessions_for_task(self, task_id: str) -> None:
        """Stop task-owned generated state when the task reaches a terminal state."""
        selected = tuple(
            (key, self._persistent_sessions.pop(key))
            for key in tuple(self._persistent_sessions)
            if key[1] == task_id
        )
        await self._close_persistent_handles(selected)

    async def _close_persistent_handles(self, handles) -> None:
        for _key, (session, root, owned_root) in handles:
            try:
                await session.close()
            except (OSError, RuntimeError) as exc:
                _logger.warning("persistent generated runtime close failed: %s", exc)
            finally:
                if owned_root:
                    shutil.rmtree(root, ignore_errors=True)

    async def _run_generated_child(
        self,
        *,
        cap: SyntheticCapability,
        child: str,
        payload: str,
        timeout: float,
        workspace_root: str | None,
        effects: set[str],
        python_paths: Sequence[str],
        context,
        request,
    ) -> tuple[str, str, int]:
        host = (
            GeneratedToolHost(
                dispatcher=self._dispatcher,
                workspace=context.workspace,
                task_id=request.task_id,
                session_id=getattr(request, "session_id", None),
                profile=getattr(context, "autonomy", None),
                task_policy=getattr(context, "capability_policy", None),
                task_budget=getattr(context, "resource_budget", None),
                call_depth=getattr(context, "generated_call_depth", 0),
                call_chain=tuple(getattr(context, "generated_call_chain", ())) + (cap.id,),
                allowed_capabilities=frozenset(cap.required_capabilities),
                inherited_effects=frozenset(
                    getattr(
                        getattr(context, "directives", None),
                        "inherited_effects",
                        (),
                    )
                ),
                inherited_capability_id=getattr(
                    getattr(context, "directives", None),
                    "inherited_capability_id",
                    None,
                ),
            )
            if self._dispatcher is not None and context is not None
            else None
        )
        if cap.runtime != "python_persistent" or request.task_id is None:
            return await self._run_child_async(
                child,
                payload,
                timeout=timeout,
                workspace_root=workspace_root,
                effects=effects,
                python_paths=python_paths,
                host=host,
            )
        root_key = os.path.realpath(os.path.abspath(workspace_root or f"<task:{request.task_id}>"))
        return await self._run_persistent_child_async(
            child,
            payload,
            timeout=timeout,
            workspace_root=workspace_root,
            effects=effects,
            python_paths=python_paths,
            host=host,
            session_key=(cap.id, str(request.task_id), root_key),
        )

    @staticmethod
    async def _communicate_with_host(proc, payload: str, host: GeneratedToolHost):
        """Serve framed ``__HOST__`` requests until the child returns."""
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc.stdin.write((payload + "\n").encode())
        await proc.stdin.drain()
        stderr_task = asyncio.create_task(proc.stderr.read())
        output: list[bytes] = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            if line.startswith(b"__HOST__"):
                try:
                    request = json.loads(line[len(b"__HOST__") :])
                    value = await host.call(request["capability_id"], request["arguments"])
                    response = {"ok": True, "value": value}
                except Exception as exc:  # noqa: BLE001 - return failure to child
                    response = {"ok": False, "error": str(exc)}
                proc.stdin.write((json.dumps(response) + "\n").encode())
                await proc.stdin.drain()
                continue
            output.append(line)
            if line.startswith(b"__RESULT__"):
                break
        if not proc.stdin.is_closing():
            proc.stdin.close()
        await proc.wait()
        return b"".join(output), await stderr_task

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
        supersedes: tuple[str, ...] = (),
    ) -> SyntheticCapability:
        """Create (but do not yet trust) a synthetic capability.

        ``validation_cases`` are [{"args": {...}, "expect_output_contains": ...}]
        executed in a subprocess sandbox before the capability becomes callable.
        """
        if runtime not in {"python", "python_persistent"}:
            raise ValueError(f"unsupported generated runtime: {runtime}")
        return SyntheticCapability(
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
            supersedes=tuple(str(item) for item in supersedes),
            effective_effects=_GENERATED_EFFECTIVE_AUTHORITY,
        )

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
    ) -> SyntheticCapability:
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
                "cases_passed": 0,
                "all_passed": False,
                "source": source_record,
                "details": [
                    {"case": "static", "passed": False, "error": f"static validation: {exc}"}
                ],
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
                }
                return cap

        try:
            dependency_metadata = self._dependency_metadata(
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
            }
            return cap
        cap.dependency_lock = {
            **dict(cap.dependency_lock or {}),
            **dependency_metadata,
        }

        child = _child_code(repr(cap.code))
        from athena.capabilities.registry import validate_schema

        hosts: list[GeneratedToolHost] = []

        async def _run_case(case: dict):
            execution_root = base_workspace_root
            host: GeneratedToolHost | None = None
            if validation_parent:
                if base_workspace_root is None:
                    raise ValueError("validation workspace root is unavailable")
                execution_root = tempfile.mkdtemp(dir=validation_parent)
                shutil.copytree(
                    base_workspace_root,
                    execution_root,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"),
                )
                _apply_workspace_fixture(case, execution_root)
                if self._dispatcher is not None and workspace is not None and task_id:
                    validation_workspace = replace(
                        workspace,
                        id=f"{workspace.id}:synthesis-validation",
                        root=execution_root,
                        readable=(),
                        writable=(),
                        mutation_mode=MutationMode.DIRECT,
                    )
                    host = GeneratedToolHost(
                        dispatcher=self._dispatcher,
                        workspace=validation_workspace,
                        task_id=task_id,
                        session_id=session_id,
                        profile=profile,
                        task_policy=task_policy,
                        task_budget=task_budget,
                        call_depth=generated_call_depth,
                        call_chain=(*generated_call_chain, cap.id),
                        inherited_effects=frozenset(
                            EffectClass(effect) for effect in self._runtime_effective_effects(cap)
                        ),
                        inherited_capability_id=cap.id,
                        allowed_capabilities=(
                            frozenset(cap.required_capabilities)
                            if cap.required_capabilities
                            else None
                        ),
                    )
                    hosts.append(host)
            before = _workspace_snapshot(execution_root) if execution_root else None
            dependency_paths = self._dependency_paths(
                cap.required_dependencies,
                execution_root,
                expected_fingerprint=None,
            )
            output = await self._run_child_async(
                child,
                json.dumps(case.get("args") or {}),
                timeout=timeout,
                workspace_root=execution_root,
                effects=self._authority_values(cap),
                python_paths=dependency_paths,
                host=host,
            )
            return (*output, execution_root, before, host)

        for i, case in enumerate(cases or []):
            try:
                case_args = case.get("args") or {}
                input_errors = validate_schema(cap.input_schema, case_args)
                if input_errors:
                    details.append(
                        {
                            "case": i,
                            "passed": False,
                            "error": "input contract: " + "; ".join(input_errors),
                        }
                    )
                    continue
                out, err, rc, case_root, before, case_host = await _run_case(case)
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
                expected_output = case.get("expect_output", _MISSING)
                if expected_output is not _MISSING:
                    ok = ok and value == expected_output
                expected_failure = bool(
                    case.get("expect_failure")
                    or case.get("expect_error_contains") is not None
                    or case.get("expected_error") is not None
                )
                if expected_failure:
                    ok = rc != 0
                    expected_error = case.get("expect_error_contains")
                    if expected_error is not None:
                        ok = ok and str(expected_error).casefold() in (f"{out}\n{err}".casefold())
                    exact_error = case.get("expected_error", _MISSING)
                    if exact_error is not _MISSING:
                        ok = ok and str(exact_error) == (err or out).strip()
                host = case_host
                effect_error = _check_effect_expectations(case, host)
                if effect_error:
                    ok = False
                resource_error, changed_resources = _check_resource_expectations(
                    case,
                    case_root,
                    before,
                )
                if resource_error:
                    ok = False
                invariant_errors = await _check_invariants(
                    {
                        **case,
                        "invariants": [
                            *(case.get("invariants") or []),
                            *(case.get("verification_requirements") or []),
                        ],
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
                            {"error": "output contract: " + "; ".join(output_errors)}
                            if output_errors
                            else {}
                        ),
                        **({"error": case_error} if case_error else {}),
                        **({"stderr": err[-300:]} if err else {}),
                    }
                )
            except (KeyError, OSError, TypeError, ValueError) as exc:
                details.append({"case": i, "passed": False, "error": str(exc)})
            finally:
                await self._emit_validation_progress(
                    task_id=task_id or cap.task_id,
                    capability_id=cap.id,
                    completed=i + 1,
                    total=len(cases or []),
                )

        if validation_parent:
            shutil.rmtree(validation_parent, ignore_errors=True)

        if hosts:
            cap.required_capabilities = tuple(
                sorted(
                    {
                        str(call.get("capability_id"))
                        for host in hosts
                        for call in host.calls
                        if call.get("capability_id")
                    }
                )
            )

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
            "cases_total": total,
            "cases_passed": passed,
            "all_passed": total > 0 and passed == total,
            "output_schema_inferred": output_schema_inferred,
            "source": source_record,
            "details": details,
        }
        cap.lifecycle_state = "VALIDATED" if cap.validation["all_passed"] else "REJECTED"
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
        if self._dispatcher is None or total <= 0:
            return
        emit = getattr(self._dispatcher, "emit_progress", None)
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
                effects=frozenset(EffectClass(effect) for effect in runtime_effects),
                # A generated invocation has no operation argument from which
                # the legacy dispatcher heuristic could recover its full
                # runtime envelope. Resolve the descriptor to that complete
                # envelope so mediated host calls inherit READ_LOCAL plus any
                # explicitly declared native effect ceiling.
                effect_resolver=lambda _arguments: frozenset(
                    EffectClass(effect) for effect in runtime_effects
                ),
                origin=(
                    CapabilityOrigin.PROJECT if cap.task_id is None else CapabilityOrigin.GENERATED
                ),
                version="0",
            )

            async def invoke(self, request, output_accumulator=None, context=None):
                # P1-18: enforce task scoping — a task-scoped synthetic must
                # not be callable by another task.
                if cap.task_id and request.task_id != cap.task_id:
                    return CapabilityResult(
                        request.call_id,
                        request.capability_id,
                        CapabilityResultStatus.FAILED,
                        error=f"synthetic capability {cap.id} is scoped to task {cap.task_id}",
                    )
                if cap.evidence_dependencies:
                    evidence = await self.engine.evidence_status(
                        cap,
                        self.engine._research_store,
                    )
                    if evidence["status"] != "CURRENT":
                        cap.lifecycle_state = "STALE"
                        cap.validation["evidence"] = evidence
                        if self.proof_sink is not None:
                            try:
                                await self.proof_sink(
                                    cap.id,
                                    self.engine._proof_record(cap),
                                )
                            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                                _logger.warning(
                                    "could not persist stale evidence status for %s",
                                    cap.id,
                                )
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
                                    repairable=True,
                                    evidence=evidence,
                                ),
                            },
                        )

                async def _run():
                    payload = json.dumps(dict(request.arguments or {}))
                    workspace_root = context.workspace.root if context is not None else None
                    dependency_paths = self.engine._dependency_paths(
                        cap.required_dependencies,
                        workspace_root,
                        expected_fingerprint=(
                            cap.dependency_lock.get("environment_fingerprint")
                            if cap.dependency_lock
                            else None
                        ),
                    )
                    return await self.engine._run_generated_child(
                        cap=cap,
                        child=child,
                        payload=payload,
                        timeout=30,
                        workspace_root=workspace_root,
                        effects=self.engine._authority_values(cap),
                        python_paths=dependency_paths,
                        context=context,
                        request=request,
                    )

                try:
                    stdout, stderr, returncode = await _run()
                except ValueError as exc:
                    return CapabilityResult(
                        request.call_id,
                        request.capability_id,
                        CapabilityResultStatus.FAILED,
                        error=f"dependency environment unavailable: {exc}",
                        metadata={
                            "generated_failure": _generated_failure(
                                cap,
                                "environment_changed",
                                repairable=True,
                            ),
                        },
                    )
                ok = returncode == 0 and "__RESULT__" in stdout
                cap.uses += 1
                input_signature = _input_signature(request.arguments)
                repeated_input = input_signature in cap.input_signatures
                cap.input_signatures.add(input_signature)
                task_signature = _input_signature(
                    {
                        "task_id": request.task_id,
                        "session_id": getattr(request, "session_id", None),
                    }
                )
                cap.task_context_signatures.add(task_signature)
                environment_signature = self.engine._environment_signature(
                    context,
                    cap,
                )
                cap.environment_fingerprints.add(environment_signature)
                from athena.protocol.messages import utcnow

                cap.last_used_at = utcnow().isoformat()

                async def _persist_proof() -> str | None:
                    errors: list[str] = []
                    if self.proof_sink is None:
                        pass
                    else:
                        try:
                            await self.proof_sink(
                                cap.id,
                                self.engine._proof_record(cap),
                            )
                        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                            # The execution result remains truthful, but a
                            # durable proof failure is surfaced in result metadata
                            # and logs instead of silently degrading auditability.
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
                                "generated candidate persistence failed for %s: %s",
                                cap.id,
                                exc,
                            )
                            errors.append(str(exc))
                    return "; ".join(errors) or None

                if ok:
                    try:
                        value = json.loads(stdout.split("__RESULT__", 1)[1].splitlines()[0])
                    except (IndexError, json.JSONDecodeError) as exc:
                        cap.failures += 1
                        proof_error = await _persist_proof()
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
                                    repairable=False,
                                ),
                            },
                        )
                    if cap.output_schema is not None:
                        from athena.capabilities.registry import validate_schema

                        errors = validate_schema(cap.output_schema, value)
                        if errors:
                            cap.failures += 1
                            proof_error = await _persist_proof()
                            return CapabilityResult(
                                request.call_id,
                                request.capability_id,
                                CapabilityResultStatus.FAILED,
                                error="generated output validation failed: " + "; ".join(errors),
                                metadata={
                                    **(
                                        {"proof_persistence_error": proof_error}
                                        if proof_error
                                        else {}
                                    ),
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
                    proof_error = await _persist_proof()
                    return CapabilityResult(
                        request.call_id,
                        request.capability_id,
                        CapabilityResultStatus.OK,
                        output=json.dumps(value),
                        metadata=({"proof_persistence_error": proof_error} if proof_error else {}),
                    )
                cap.failures += 1
                proof_error = await _persist_proof()
                return CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=(stderr or "synthetic failed")[-500:],
                    metadata={
                        **({"proof_persistence_error": proof_error} if proof_error else {}),
                        "generated_failure": _generated_failure(
                            cap,
                            (
                                "governance_failure"
                                if "host call" in (stderr or "").lower()
                                else "implementation_failure"
                            ),
                            repairable="host call" not in (stderr or "").lower(),
                        ),
                    },
                )

        return _Executor(
            engine_ref=self,
            proof_sink_ref=proof_sink,
            candidate_sink_ref=candidate_sink,
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
            for effect in descriptor.effects:
                value = getattr(effect, "value", str(effect))
                if value in declared:
                    effects.add(value)
        return frozenset(effects)

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
        return _input_signature(
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

    @staticmethod
    def _proof_record(cap: SyntheticCapability) -> dict:
        live_quality = cap.successes / cap.uses if cap.uses else 0.0
        validation_quality = (
            cap.validation.get("cases_passed", 0) / cap.validation.get("cases_total", 1)
            if cap.validation.get("cases_total")
            else 0.0
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
            "required_capabilities": list(cap.required_capabilities),
            "supersedes": list(cap.supersedes),
            "evidence_dependencies": [
                dependency.to_record() for dependency in cap.evidence_dependencies
            ],
            "distinct_inputs": len(cap.input_signatures),
            "input_signatures": sorted(cap.input_signatures)[:64],
            "distinct_task_contexts": len(cap.task_context_signatures),
            "task_context_signatures": sorted(cap.task_context_signatures)[:64],
            "distinct_environments": len(cap.environment_fingerprints),
            "environment_fingerprints": sorted(cap.environment_fingerprints)[:64],
            "reuse_count": cap.reuse_count,
            "downstream_verifications": cap.downstream_verifications,
            "latency_saved_ms": round(cap.latency_saved_ms, 2),
            "turns_saved": cap.turns_saved,
            # Zero is not evidence that a savings metric was observed. Keep
            # the provenance explicit so promotion/ranking cannot mistake
            # unmeasured fields for a benchmark supplied by the caller.
            "metric_provenance": {
                "reuse_count": "canonical_generated_invocation",
                "downstream_verifications": "canonical_passing_verification",
                "latency_saved_ms": "not_measured",
                "turns_saved": "not_measured",
            },
            "validation_strength": cap.validation.get("tier", "unknown"),
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
        encoded = json.dumps(requirements, sort_keys=True, separators=(",", ":")).encode()
        return {
            **dict(cap.dependency_lock or {}),
            "format": 1,
            "requirements": requirements,
            "fingerprint": hashlib.sha256(encoded).hexdigest(),
        }

    def _generated_record(
        self,
        cap: SyntheticCapability,
        *,
        scope: AffordanceScope,
        project_scope: str | None = None,
        user_scope: str | None = None,
    ) -> GeneratedCapability:
        proof = self._proof_record(cap)
        return GeneratedCapability(
            id=cap.id,
            name=cap.name,
            description=cap.description,
            implementation=cap.code,
            runtime=cap.runtime,
            input_schema=cap.input_schema,
            output_schema=cap.output_schema,
            declared_effects=frozenset(self._effect_values(cap)),
            effective_authority=frozenset(self._authority_values(cap)),
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
            supersedes=cap.supersedes,
            dependency_lock=self._dependency_lock(cap),
            validation_cases=tuple(dict(case) for case in (cap.validation_cases or [])),
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
        executor = self._build_executor(cap, proof_sink=proof_sink, candidate_sink=candidate_sink)
        generated = self._generated_record(
            cap,
            scope=AffordanceScope.TASK if cap.task_id else AffordanceScope.PROJECT,
        )
        if hasattr(registry, "register_task") and cap.task_id:
            registry.register_task(cap.task_id, executor, generated=generated)
        else:
            registry.register(executor)
        self._synthetic[cap.id] = cap
        self._executors[cap.id] = executor
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
        source_validation = self._source_validator.validate(
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
        if not persisted_authority.issubset(_GENERATED_EFFECTIVE_AUTHORITY):
            raise ValueError(
                "persisted generated capability requests authority outside "
                "the generated sandbox profile"
            )
        _compile_validator(generated.input_schema)
        if generated.output_schema is not None:
            _compile_validator(generated.output_schema)
        if generated.required_dependencies and workspace_root:
            self._dependency_paths(
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
            "runtime": cap.runtime,
            "validation": cap.validation,
            "uses": cap.uses,
            "successes": cap.successes,
            "failures": cap.failures,
            "provenance": cap.provenance,
            "effects": sorted(getattr(e, "value", str(e)) for e in cap.effects),
            "effective_authority": sorted(cap.effective_effects),
            "required_capabilities": list(cap.required_capabilities),
            "evidence_dependencies": [
                dependency.to_record() for dependency in cap.evidence_dependencies
            ],
            "code_hash": hashlib.sha256(cap.code.encode()).hexdigest(),
            "lifecycle_state": cap.lifecycle_state,
            "quality_score": self._proof_record(cap).get("quality_score", 0.0),
            "last_used_at": cap.last_used_at,
            "supersedes": list(cap.supersedes),
            "dependency_lock": self._dependency_lock(cap),
        }

    def synthetic_for(self, cap_id: str) -> SyntheticCapability | None:
        """Return the in-memory capability record for lifecycle operations."""
        return self._synthetic.get(cap_id)

    def promote(
        self,
        surface,
        cap_id: str,
        *,
        scope: AffordanceScope,
        project_id: str | None = None,
        user_id: str = "athena",
    ) -> bool:
        """Explicitly promote validated task machinery to a wider overlay.

        Promotion is never implicit.  SYSTEM promotion is intentionally
        rejected: native/system changes belong to the normal release process.
        """
        if scope not in {AffordanceScope.PROJECT, AffordanceScope.USER}:
            raise ValueError("promotion target must be project or user")
        cap = self._synthetic.get(cap_id)
        executor = self._executors.get(cap_id)
        promotion_tier = (
            ValidationTier.PROJECT if scope is AffordanceScope.PROJECT else ValidationTier.USER
        )
        if cap is None or executor is None:
            _logger.warning(
                "refusing promotion of %s because it is unknown or not executable",
                cap_id,
            )
            return False
        proof_error = _promotion_proof_error(cap, promotion_tier)
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
        source_validation = self._source_validator.validate(cap.code, tier=promotion_tier)
        if not source_validation.passed:
            _logger.warning(
                "refusing promotion of %s after %s source checks",
                cap.id,
                promotion_tier.value,
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
            id=cap.id,
            name=cap.name,
            description=cap.description,
            implementation=cap.code,
            input_schema=cap.input_schema,
            runtime=cap.runtime,
            output_schema=cap.output_schema,
            required_dependencies=cap.required_dependencies,
            required_capabilities=cap.required_capabilities,
            evidence_dependencies=cap.evidence_dependencies,
            declared_effects=frozenset(self._effect_values(cap)),
            effective_authority=frozenset(self._authority_values(cap)),
            scope=scope,
            project_scope=project_id,
            user_scope=user_id if scope is AffordanceScope.USER else None,
            provenance={**cap.provenance, "promoted_from": "task"},
            validation_state="PROMOTED",
            proof_record=proof,
            lifecycle_state="PROMOTED",
            supersedes=cap.supersedes,
            dependency_lock=self._dependency_lock(cap),
            use_count=cap.uses,
            success_count=cap.successes,
            failure_count=cap.failures,
            quality_score=float(proof.get("quality_score") or 0.0),
            last_used_at=cap.last_used_at,
            validation_cases=tuple(dict(case) for case in (cap.validation_cases or [])),
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
        # Repetition with the same arguments is not evidence that a helper is
        # reusable. Require successful behavioral diversity before turning
        # executable proof into a durable knowledge candidate.
        if cap is None or not _candidate_ready(cap):
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
            "properties": {str(key): _schema_for_values([item]) for key, item in value.items()},
            "required": [str(key) for key in value],
            "additionalProperties": False,
        }
    return {}
