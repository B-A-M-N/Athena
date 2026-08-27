"""Tests for acceptance verification (P9)."""
from __future__ import annotations
import pytest

import os
import tempfile
from types import SimpleNamespace

from athena.protocol.tasks import (
    Criterion,
    TaskSpec,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)
from athena.kernel.verifiers import (
    CompositeVerifier,
    _CapabilityCheckVerifier,
    _FileVerifier,
    _ManualVerifier,
    _ModelJudgmentVerifier,
)
from athena.protocol.messages import TextBlock
from athena.protocol.models import ModelResponse


def _task(ws_root: str = "/tmp") -> TaskSpec:
    return TaskSpec(
        id="test-task",
        objective="test",
        workspace=WorkspaceSpec(id="ws", root=ws_root),
    )


def _run(coro):
    """Run a coroutine synchronously."""
    import asyncio
    return asyncio.run(coro)


@pytest.mark.athena_claim("BHV-129")
@pytest.mark.athena_evidence("test", "invariant")
def test_manual_verifier_always_false():
    v = _ManualVerifier()
    spec = VerificationSpec(type=VerificationType.MANUAL)
    assert _run(v.verify_one(_task(), spec)) is False


@pytest.mark.athena_claim("BHV-129")
@pytest.mark.athena_evidence("test", "invariant")
def test_file_verifier_exists():
    v = _FileVerifier()
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"hello")
        path = f.name
    try:
        spec = VerificationSpec(type=VerificationType.FILE, path=path, predicate="exists")
        assert _run(v.verify_one(_task(), spec)) is True
    finally:
        os.unlink(path)


def test_file_verifier_not_exists():
    v = _FileVerifier()
    spec = VerificationSpec(type=VerificationType.FILE, path="/nonexistent/path", predicate="exists")
    assert _run(v.verify_one(_task(), spec)) is False


def test_file_verifier_contains():
    v = _FileVerifier()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(b"hello world")
        path = f.name
    try:
        spec = VerificationSpec(type=VerificationType.FILE, path=path, predicate="contains:world")
        assert _run(v.verify_one(_task(), spec)) is True
        spec2 = VerificationSpec(type=VerificationType.FILE, path=path, predicate="contains:missing")
        assert _run(v.verify_one(_task(), spec2)) is False
    finally:
        os.unlink(path)


def test_file_verifier_no_spec():
    v = _FileVerifier()
    spec = VerificationSpec(type=VerificationType.FILE)
    assert _run(v.verify_one(_task(), spec)) is False


def test_composite_dispatch():
    """CompositeVerifier dispatches to the right sub-verifier."""
    v = CompositeVerifier()
    criteria = (
        Criterion(id="c1", description="manual", verification=VerificationSpec(type=VerificationType.MANUAL)),
    )
    results = _run(v.verify(_task(), criteria))
    assert results == [False]


def test_composite_no_verification_spec():
    """Criterion with no verification spec is unresolved."""
    v = CompositeVerifier()
    criteria = (
        Criterion(id="c1", description="no spec", verification=None),
    )
    results = _run(v.verify(_task(), criteria))
    assert results == [False]


def test_capability_check_uses_task_scoped_fabric():
    calls = []

    class Fabric:
        def has(self, capability_id, **scope):
            calls.append((capability_id, scope))
            return True

    task = TaskSpec(
        id="task-7",
        objective="test",
        metadata={"project_id": "project-2", "user_id": "user-3"},
    )
    spec = VerificationSpec(
        type=VerificationType.CAPABILITY_CHECK,
        capability="generated.check",
    )
    assert _run(_CapabilityCheckVerifier(Fabric()).verify_one(task, spec)) is True
    assert calls == [(
        "generated.check",
        {"task_id": "task-7", "project_id": "project-2", "user_id": "user-3"},
    )]


def test_model_judgment_uses_kernel_broker():
    calls = []

    async def broker(**kwargs):
        calls.append(kwargs)
        return ModelResponse(
            request_id="judge-1",
            model="judge-model",
            provider="judge-provider",
            blocks=(TextBlock(text="YES"),),
        )

    task = _task()
    spec = VerificationSpec(
        type=VerificationType.MODEL_JUDGMENT,
        predicate="the result is correct",
    )
    verifier = _ModelJudgmentVerifier(
        SimpleNamespace(), inference_broker=broker,
    )
    assert _run(verifier.verify_one(task, spec)) is True
    assert calls and calls[0]["task"] == task
    assert "the result is correct" in calls[0]["user_prompt"]
