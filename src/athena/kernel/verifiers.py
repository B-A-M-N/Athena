"""Concrete acceptance-criteria verifiers (BUILDSPEC §10, §21).

Each :class:`Criterion` carries a :class:`VerificationSpec` declaring HOW its
satisfaction is observed. This module provides a :class:`CompositeVerifier`
that dispatches to a per-type interpreter, wired into
:class:`TerminationEvaluator` at service build time.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from athena.protocol.messages import (
    Message,
    Provenance,
    Role,
    SourceType,
    TextBlock,
    TrustClass,
    utcnow,
)
from athena.protocol.capabilities import CapabilityRequest, CapabilityRequestOrigin
from athena.protocol.models import ModelRequest
from athena.protocol.tasks import (
    Criterion,
    TaskSpec,
    VerificationSpec,
    VerificationType,
)

_logger = logging.getLogger("athena.verifier")


class _TypeVerifier(Protocol):
    """Verifier for a single verification type."""

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        """Return True if the criterion is satisfied."""
        ...


class _CommandVerifier:
    """Verify a command through the canonical restricted execution route.

    Verification commands are untrusted input.  Sending them directly to the
    execution manager would make an acceptance criterion an execution-policy
    bypass, so this verifier deliberately depends on the capability
    dispatcher.  The ``execute`` capability applies the task workspace and
    sandbox policy before starting the probe.
    """

    def __init__(self, dispatcher: Any = None) -> None:
        self._dispatcher = dispatcher

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        if not spec.command:
            return False
        if self._dispatcher is None:
            _logger.warning("command verifier: no CapabilityDispatcher bound")
            return False
        try:
            if task.workspace is None:
                _logger.warning("command verifier: task has no workspace")
                return False
            request = CapabilityRequest(
                capability_id="execute",
                arguments={"language": "shell", "code": spec.command},
                task_id=task.id,
                session_id=task.session_id,
                call_id=f"verify_{task.id}_{id(spec)}",
                origin=CapabilityRequestOrigin.SYSTEM,
            )
            result = await self._dispatcher.dispatch(
                request,
                workspace=task.workspace,
                profile=(task.metadata or {}).get("autonomy"),
                task_policy=task.capability_policy,
            )
            # An approval request cannot be resolved by a verifier.  Treat it
            # as unresolved instead of turning it into an implicit grant.
            if not hasattr(result, "status"):
                return False
            return result.status.value == "ok"
        except Exception as exc:
            _logger.warning("command verifier failed: %s", exc)
            return False


class _FileVerifier:
    """Verify by inspecting filesystem state (read-only)."""

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        if not spec.path:
            return False
        import os
        target = spec.path
        if task.workspace and not os.path.isabs(target):
            target = os.path.join(task.workspace.root, target)
        target = os.path.realpath(os.path.abspath(target))
        if task.workspace:
            root = os.path.realpath(os.path.abspath(task.workspace.root))
            if target != root and not target.startswith(root + os.sep):
                _logger.warning("file verifier: path escapes workspace: %s", spec.path)
                return False
        if spec.predicate:
            try:
                if spec.predicate == "exists":
                    return os.path.exists(target)
                if spec.predicate == "not_exists":
                    return not os.path.exists(target)
                if spec.predicate.startswith("contains:"):
                    needle = spec.predicate[len("contains:"):]
                    if not os.path.isfile(target):
                        return False
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        return needle in f.read()
            except Exception as exc:
                _logger.warning("file verifier predicate failed: %s", exc)
                return False
        return os.path.exists(target)


class _ArtifactPredicateVerifier:
    """Verify by checking an immutable artifact's properties."""

    def __init__(self, artifact_store: Any = None) -> None:
        self._store = artifact_store

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        if not spec.path and not spec.predicate:
            return False
        if self._store is None:
            return False
        try:
            ref_str = spec.path or spec.predicate
            if not ref_str:
                return False
            refs = await self._store.list(task_id=task.id)
            for ref in refs:
                if ref.hash == ref_str or ref.uri == ref_str:
                    return True
            return False
        except Exception as exc:
            _logger.warning("artifact predicate verifier failed: %s", exc)
            return False


class _CapabilityCheckVerifier:
    """Verify a capability is registered and available."""

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        if not spec.capability:
            return False
        if self._registry is None:
            return False
        try:
            return self._registry.has(spec.capability)
        except Exception:
            return False


class _ModelJudgmentVerifier:
    """Ask the model to judge satisfaction (lower-trust, configurable)."""

    def __init__(
        self, registry: Any = None, *, trusted: bool = False,
        evidence_provider: Any = None,
    ) -> None:
        self._registry = registry
        self._trusted = trusted
        self._evidence_provider = evidence_provider

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        if self._registry is None:
            return False
        try:
            from athena.protocol.tasks import ModelPolicy

            selection = await self._registry.select(
                policy=ModelPolicy(role="judge", require_tools=False)
            )
            provider = self._registry.provider_for(selection.provider)
        except Exception as exc:
            _logger.warning("model judgment: selection failed: %s", exc)
            return False
        metadata = dict(task.metadata or {})
        evidence = metadata.get("verification_evidence", metadata.get("evidence", {}))
        artifacts = metadata.get("artifacts", ())
        unresolved = metadata.get("unresolved_failures", ())
        world_state = metadata.get("world_state")
        if self._evidence_provider is not None:
            try:
                provided = self._evidence_provider(task)
                if hasattr(provided, "__await__"):
                    provided = await provided
                if isinstance(provided, dict):
                    evidence = provided.get("evidence", evidence)
                    artifacts = provided.get("artifacts", artifacts)
                    unresolved = provided.get("unresolved_failures", unresolved)
                    world_state = provided.get("world_state", world_state)
            except Exception as exc:
                _logger.warning("model judgment: evidence collection failed: %s", exc)
        prompt = (
            f"Task objective: {task.objective}\n\n"
            f"Criterion: {spec.predicate or spec.path or spec.command or 'check'}\n\n"
            f"Evidence: {evidence!r}\n"
            f"Artifacts: {artifacts!r}\n"
            f"Unresolved failures: {unresolved!r}\n"
            f"Current world state: {world_state!r}\n\n"
            f"Based only on this task evidence, is this criterion satisfied?\n"
            f"Reply with ONLY: YES or NO."
        )
        try:
            from athena.protocol.ids import new_id
            messages = (Message(
                id=new_id("msg"),
                role=Role.USER,
                blocks=(TextBlock(type="text", text=prompt),),
                created_at=utcnow(),
                provenance=Provenance(source_type=SourceType.RUNTIME, trust=TrustClass.AGENT_CURATED, scope="verify"),
            ),)
            request = ModelRequest(
                messages=messages,
                model=selection.model,
                provider=selection.provider,
                request_id=f"verify_{id(spec)}",
            )
            text_parts: list[str] = []
            async for event in provider.complete(request):
                if event.type.value == "delta" and event.delta:
                    if event.delta.text:
                        text_parts.append(event.delta.text)
                elif event.type.value == "done" and event.response:
                    text_parts = [b.text for b in event.response.blocks if isinstance(b, TextBlock)]
            answer = " ".join(text_parts).strip().upper()
            return answer.startswith("YES")
        except Exception as exc:
            _logger.warning("model judgment: inference failed: %s", exc)
            return self._trusted


class _ManualVerifier:
    """Manual criteria are never auto-satisfied; user must verify."""

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        return False


class CompositeVerifier:
    """Dispatches acceptance-criteria verification by type."""

    def __init__(
        self,
        *,
        execution: Any = None,
        dispatcher: Any = None,
        artifact_store: Any = None,
        capability_registry: Any = None,
        model_registry: Any = None,
        evidence_provider: Any = None,
        model_judgment_trusted: bool = False,
    ) -> None:
        # ``execution`` is retained in the signature for source compatibility,
        # but command verification never uses it: the dispatcher is the only
        # route that can establish the task's policy/sandbox boundary.
        self._command = _CommandVerifier(dispatcher)
        self._file = _FileVerifier()
        self._artifact = _ArtifactPredicateVerifier(artifact_store)
        self._capability = _CapabilityCheckVerifier(capability_registry)
        # P1-22: the judge verifier needs the ModelRouter (which owns
        # select()); a bare ProviderRegistry has no select(). Both are
        # accepted; the router path is used when available.
        self._model = _ModelJudgmentVerifier(
            model_registry,
            trusted=model_judgment_trusted,
            evidence_provider=evidence_provider,
        )
        self._manual = _ManualVerifier()

    async def verify(self, task: TaskSpec, criteria: tuple[Criterion, ...]) -> list[bool]:
        """Return, in order, whether each criterion is satisfied."""
        results: list[bool] = []
        for criterion in criteria:
            spec = criterion.verification
            if spec is None:
                results.append(False)
                continue
            vtype = spec.type
            if vtype == VerificationType.COMMAND:
                ok = await self._command.verify_one(task, spec)
            elif vtype == VerificationType.FILE:
                ok = await self._file.verify_one(task, spec)
            elif vtype == VerificationType.ARTIFACT_PREDICATE:
                ok = await self._artifact.verify_one(task, spec)
            elif vtype == VerificationType.CAPABILITY_CHECK:
                ok = await self._capability.verify_one(task, spec)
            elif vtype == VerificationType.MODEL_JUDGMENT:
                ok = await self._model.verify_one(task, spec)
            elif vtype == VerificationType.MANUAL:
                ok = await self._manual.verify_one(task, spec)
            else:
                ok = False
            results.append(ok)
        return results
