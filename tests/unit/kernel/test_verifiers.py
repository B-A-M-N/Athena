"""Tests for acceptance verification (P9)."""
from __future__ import annotations
import pytest

import os
import tempfile
from pathlib import Path

from athena.protocol.tasks import (
    Criterion,
    TaskSpec,
    VerificationSpec,
    VerificationType,
    WorkspaceSpec,
)
from athena.kernel.verifiers import (
    CompositeVerifier,
    _FileVerifier,
    _ManualVerifier,
)


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
