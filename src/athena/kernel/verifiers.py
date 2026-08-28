"""Concrete acceptance-criteria verifiers (BUILDSPEC §10, §21).

Each :class:`Criterion` carries a :class:`VerificationSpec` declaring HOW its
satisfaction is observed. This module provides a :class:`CompositeVerifier`
that dispatches to a per-type interpreter, wired into
:class:`TerminationEvaluator` at service build time.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Protocol

from athena.protocol.messages import TextBlock
from athena.protocol.capabilities import CapabilityRequest, CapabilityRequestOrigin
from athena.protocol.tasks import (
    Criterion,
    TaskSpec,
    VerificationSpec,
    VerificationType,
    MutationMode,
    NetworkPolicy,
    PathRule,
)
from athena.causal.checkpoint import _run_worker as _run_checkpoint_worker

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
    """Verify by inspecting filesystem state (read-only).

    File predicates are read-only observations, but when a dispatcher is
    bound they route through the canonical ``fs`` capability so an active
    speculative branch is observed instead of the real workspace root.
    Otherwise a candidate that created ``foo.py`` would fail a FILE
    criterion checking for it.

    Without a dispatcher, falls back to direct filesystem access for
    backward compatibility.
    """

    def __init__(self, dispatcher: Any = None) -> None:
        self._dispatcher = dispatcher

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        if not spec.path:
            return False
        import os

        target = spec.path
        if task.workspace and not os.path.isabs(target):
            target = os.path.join(task.workspace.root, target)
        real = os.path.realpath(os.path.abspath(target))
        if task.workspace:
            root = os.path.realpath(os.path.abspath(task.workspace.root))
            if real != root and not real.startswith(root + os.sep):
                _logger.warning("file verifier: path escapes workspace: %s", spec.path)
                return False
        # When a dispatcher is bound, route through the canonical fs capability
        # so an active speculative branch is observed instead of the real
        # workspace root.
        if self._dispatcher is not None and task.workspace is not None:
            return await self._verify_via_dispatcher(task, spec)
        # Fallback: direct filesystem access (backward compatibility).
        return self._verify_direct(real, spec)

    def _verify_direct(self, real: str, spec: VerificationSpec) -> bool:
        import os

        if spec.predicate:
            try:
                if spec.predicate == "exists":
                    return os.path.exists(real)
                if spec.predicate == "not_exists":
                    return not os.path.exists(real)
                if spec.predicate.startswith("contains:"):
                    needle = spec.predicate[len("contains:") :]
                    if not os.path.isfile(real):
                        return False
                    with open(real, "r", encoding="utf-8", errors="replace") as f:
                        return needle in f.read()
            except Exception as exc:
                _logger.warning("file verifier predicate failed: %s", exc)
                return False
        return os.path.exists(real)

    async def _verify_via_dispatcher(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        try:
            result = await self._dispatcher.dispatch(
                CapabilityRequest(
                    capability_id="fs",
                    arguments={"operation": "stat", "path": spec.path},
                    task_id=task.id,
                    session_id=task.session_id,
                    call_id=f"file_verify_{task.id}_{id(spec)}",
                    origin=CapabilityRequestOrigin.SYSTEM,
                ),
                workspace=task.workspace,
                profile=(task.metadata or {}).get("autonomy"),
                task_policy=task.capability_policy,
            )
        except Exception as exc:
            _logger.warning("file verifier dispatch failed: %s", exc)
            return False
        if result.status.value != "ok":
            return False
        import json as _json

        try:
            info = _json.loads(result.output)
        except (ValueError, TypeError):
            info = {}
        if spec.predicate == "exists":
            return bool(info.get("is_file") or info.get("is_dir"))
        if spec.predicate == "not_exists":
            return not (info.get("is_file") or info.get("is_dir"))
        if spec.predicate and spec.predicate.startswith("contains:"):
            if not info.get("is_file"):
                return False
            read = await self._dispatcher.dispatch(
                CapabilityRequest(
                    capability_id="fs",
                    arguments={"operation": "read", "path": spec.path},
                    task_id=task.id,
                    session_id=task.session_id,
                    call_id=f"file_verify_read_{task.id}_{id(spec)}",
                    origin=CapabilityRequestOrigin.SYSTEM,
                ),
                workspace=task.workspace,
                profile=(task.metadata or {}).get("autonomy"),
                task_policy=task.capability_policy,
            )
            if read.status.value != "ok":
                return False
            needle = spec.predicate[len("contains:") :]
            return needle in (read.output or "")
        return True


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

    def __init__(self, fabric: Any = None) -> None:
        self._fabric = fabric

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        if not spec.capability:
            return False
        if self._fabric is None:
            return False
        try:
            metadata = dict(task.metadata or {})
            return self._fabric.has(
                spec.capability,
                task_id=task.id,
                project_id=metadata.get("project_id"),
                user_id=metadata.get("user_id"),
            )
        except Exception:
            return False


class _ModelJudgmentVerifier:
    """Ask the model to judge satisfaction (lower-trust, configurable)."""

    def __init__(
        self,
        registry: Any = None,
        *,
        trusted: bool = False,
        evidence_provider: Any = None,
        inference_broker: Any = None,
    ) -> None:
        # ``registry`` remains accepted for source compatibility, but model
        # access is intentionally brokered by the kernel. A verifier must not
        # open an unmetered provider stream beside the task's reasoning loop.
        self._registry = registry
        self._trusted = trusted
        self._evidence_provider = evidence_provider
        self._inference_broker = inference_broker

    async def verify_one(self, task: TaskSpec, spec: VerificationSpec) -> bool:
        if self._inference_broker is None:
            _logger.warning("model judgment: no kernel inference broker bound")
            return False
        metadata = dict(task.metadata or {})
        evidence = metadata.get("verification_evidence", metadata.get("evidence", {}))
        artifacts = metadata.get("artifacts", ())
        unresolved = metadata.get("unresolved_failures", ())
        world_state = metadata.get("world_state")
        if self._evidence_provider is not None:
            try:
                provided = await self._evidence_provider(task)
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
            response = await self._inference_broker(
                task=task,
                system_prompt=(
                    "You are Athena's acceptance-criteria judge. "
                    "Evaluate only the supplied task evidence."
                ),
                user_prompt=prompt,
            )
            text_parts = [
                block.text
                for block in getattr(response, "blocks", ())
                if isinstance(block, TextBlock) and block.text
            ]
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
        inference_broker: Any = None,
        model_judgment_trusted: bool = False,
    ) -> None:
        # ``execution`` is retained in the signature for source compatibility,
        # but command verification never uses it: the dispatcher is the only
        # route that can establish the task's policy/sandbox boundary.
        self._command = _CommandVerifier(dispatcher)
        self._file = _FileVerifier(dispatcher)
        self._artifact = _ArtifactPredicateVerifier(artifact_store)
        self._capability = _CapabilityCheckVerifier(capability_registry)
        self._model = _ModelJudgmentVerifier(
            model_registry,
            trusted=model_judgment_trusted,
            evidence_provider=evidence_provider,
            inference_broker=inference_broker,
        )
        self._manual = _ManualVerifier()

    async def verify(self, task: TaskSpec, criteria: tuple[Criterion, ...]) -> list[bool]:
        """Return, in order, whether each criterion is satisfied."""
        async with _verification_view(
            task, enabled=self._command._dispatcher is not None
        ) as view_task:
            return await self._verify_in_view(view_task, criteria)

    async def _verify_in_view(
        self,
        task: TaskSpec,
        criteria: tuple[Criterion, ...],
    ) -> list[bool]:
        """Evaluate a plan against the disposable workspace view."""
        results: list[bool] = []
        protected = await _verification_manifest(task.workspace)
        allowed = _verification_writable_paths(task)
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
            after = await _verification_manifest(task.workspace)
            changed = _manifest_changes(protected, after)
            unexpected = sorted(path for path in changed if path not in allowed)
            if unexpected:
                _logger.error(
                    "verification probe mutated protected workspace resources: %s",
                    ", ".join(unexpected[:16]),
                )
                # The view is disposable, but a mutating verifier is still
                # not valid evidence.  Do not certify any remaining checks.
                ok = False
                results.append(ok)
                results.extend(False for _ in criteria[len(results) :])
                return results
            results.append(ok)
        return results


async def _verification_manifest(workspace: Any) -> dict[str, str]:
    """Capture the protected view through the same filesystem worker used by
    reality and checkpoint code.

    A missing workspace is represented by an empty manifest so verifiers fail
    through their ordinary criterion rather than turning evidence collection
    into an exception path.
    """
    if workspace is None or not os.path.isdir(workspace.root):
        return {}
    try:
        result = await _run_checkpoint_worker(
            "manifest",
            root=workspace.root,
            workspace_root=workspace.root,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return {}
    manifest = result.get("manifest") if isinstance(result, dict) else None
    return (
        {str(path): str(digest) for path, digest in manifest.items()}
        if isinstance(manifest, dict)
        else {}
    )


def _verification_writable_paths(task: TaskSpec) -> frozenset[str]:
    raw = (task.metadata or {}).get("verification_writable_paths", ())
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(path).replace(os.sep, "/") for path in raw)


def _manifest_changes(
    before: dict[str, str],
    after: dict[str, str],
) -> set[str]:
    return {path for path in set(before) | set(after) if before.get(path) != after.get(path)}


@asynccontextmanager
async def _verification_view(task: TaskSpec, *, enabled: bool):
    """Provide verifiers a disposable workspace for effectful probes.

    Acceptance commands are observations, not mutation authority.  Running
    them against a throwaway copy prevents a test/build command from changing
    the certified source tree while still allowing it to inspect the exact
    candidate view supplied by the caller.
    """
    workspace = task.workspace
    if not enabled or workspace is None or not os.path.isdir(workspace.root):
        yield task
        return
    parent = tempfile.mkdtemp(prefix="athena-verify-")
    view_root = os.path.join(parent, "workspace")
    try:
        await _run_checkpoint_worker(
            "clone",
            root=parent,
            checkpoint_id="workspace",
            workspace_root=workspace.root,
        )
        view = replace(
            workspace,
            id=f"{workspace.id}-verification",
            root=view_root,
            readable=_rebase_rules(workspace.readable, workspace.root, view_root),
            writable=_rebase_rules(workspace.writable, workspace.root, view_root),
            execution_backend="verification",
            network_policy=NetworkPolicy.DENY,
            mutation_mode=MutationMode.DIRECT,
        )
        yield replace(task, workspace=view)
    finally:
        try:
            await _run_checkpoint_worker(
                "delete",
                root=parent,
                checkpoint_id="workspace",
                workspace_root=parent,
            )
        finally:
            try:
                os.rmdir(parent)
            except OSError:
                _logger.debug("verification view parent was not empty: %s", parent)


def _rebase_rules(
    rules: tuple[PathRule, ...],
    base_root: str,
    view_root: str,
) -> tuple[PathRule, ...]:
    base = os.path.realpath(os.path.abspath(base_root))
    view = os.path.realpath(os.path.abspath(view_root))
    rebased: list[PathRule] = []
    for rule in rules:
        path = str(rule.path)
        if os.path.isabs(path):
            normalized = os.path.realpath(os.path.abspath(path))
            if normalized == base or normalized.startswith(base + os.sep):
                path = view + normalized[len(base) :]
        else:
            path = os.path.join(view, path)
        rebased.append(PathRule(path=path, allow=rule.allow))
    return tuple(rebased)
