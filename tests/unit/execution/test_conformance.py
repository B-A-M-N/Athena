import pytest

from athena.execution.conformance import (
    BackendPassport,
    ConformanceReceipt,
    proof_status,
    run_backend_conformance,
    run_backend_passport,
)
from athena.execution.conformance_probes import containment_source
from athena.execution.manager import ExecutionManager
from athena.execution.runtimes import NodeRuntime, PythonRuntime, ShellRuntime
from athena.protocol.execution import ExecutionExitStatus, ExecutionResult


class _ConformanceManager:
    def __init__(self):
        self.state = {}

    def backend_capabilities(self, backend):
        assert backend == "fixture"
        return {
            "supported_runtimes": ("python", "shell", "node"),
            "runtime_capabilities": {
                runtime: {
                    "persistent_runtime_state": True,
                    "reattach": True,
                    "process_signals": True,
                }
                for runtime in ("python", "shell", "node")
            },
        }

    async def create_session(self, **kwargs):
        return f"{kwargs['runtime']}-session"

    async def execute(self, request):
        if "41" in request.source or "=41" in request.source:
            self.state[request.runtime] = 41
            output = ""
        else:
            output = "42" if request.runtime != "shell" else "41"
        return ExecutionResult(
            execution_id="fixture",
            exit_code=0,
            status=ExecutionExitStatus.EXITED,
            stdout=output,
        )

    async def destroy_session(self, _session_id):
        return None


async def test_conformance_executes_every_advertised_persistent_cell():
    receipts = await run_backend_conformance(_ConformanceManager(), backend="fixture")
    assert {receipt.runtime for receipt in receipts} == {"python", "shell", "node"}
    assert all(not receipt.passed for receipt in receipts), receipts
    assert all("persistent_runtime_state" in receipt.checks for receipt in receipts)
    assert all("reattach" in receipt.unverified_claims for receipt in receipts)


async def test_backend_passport_keeps_unverified_claims_non_passing():
    passport = await run_backend_passport(
        _ConformanceManager(),
        backend="fixture",
        release_sha="release-sha",
        environment={"fixture": True},
    )
    assert passport.status == "FAIL"
    assert set(passport.unverified_claims) == {
        f"{runtime}:{claim}"
        for runtime in ("node", "python", "shell")
        for claim in ("reattach", "process_signals")
    }
    record = passport.to_record()
    assert record["kind"] == "athena_backend_passport"
    assert record["release_sha"] == "release-sha"
    assert record["status"] == "FAIL"


def test_empty_or_incomplete_backend_passports_fail_closed():
    assert (
        BackendPassport.from_receipts([], release_sha="release-sha", expected_runtimes=()).status
        == "FAIL"
    )
    receipt = ConformanceReceipt(backend="fixture", runtime="python", checks=("execution",))
    assert (
        BackendPassport.from_receipts(
            [receipt], release_sha="release-sha", expected_runtimes=("python", "shell")
        ).status
        == "FAIL"
    )
    duplicate = ConformanceReceipt(backend="fixture", runtime="python", checks=("execution",))
    assert (
        BackendPassport.from_receipts(
            [receipt, duplicate],
            release_sha="release-sha",
            expected_runtimes=("python", "python"),
        ).status
        == "FAIL"
    )


def test_backend_passport_rejects_cells_from_another_backend():
    passport = BackendPassport(
        backend="local",
        release_sha="sha",
        release_run_id="run",
        environment={"platform": "fixture"},
        expected_runtimes=("python",),
        receipts=(
            ConformanceReceipt(
                backend="container",
                runtime="python",
                checks=("execution",),
            ),
        ),
    )
    assert passport.status == "FAIL"


def test_proof_status_is_shared_by_receipts_and_operator_projection():
    receipt = ConformanceReceipt(
        backend="fixture",
        runtime="python",
        checks=("execution",),
        metadata={"unverified_claims": ("network_containment",)},
    )
    assert (
        proof_status(
            checks=receipt.checks,
            failures=receipt.failures,
            unverified_claims=receipt.unverified_claims,
        )
        == "unverified"
    )
    assert receipt.passed is False
    assert receipt.to_record()["proof_status"] == "unverified"

    from athena.service.operational_matrix import behavioral_proof

    projection = behavioral_proof(
        {
            "status": "FAIL",
            "release_sha": "sha",
            "cells": [receipt.to_record() | {"runtime": "python"}],
        },
        "python",
    )
    assert projection["status"] == "unverified"
    assert projection["certified"] is False


def test_containment_probe_uses_only_the_controlled_endpoint():
    source = containment_source(
        "python", "/tmp/outside", network_host="127.0.0.1", network_port=43123
    )
    assert "198.51.100.1" not in source
    assert "43123" in source


@pytest.mark.asyncio
async def test_local_runtime_matrix_is_behaviorally_proven(tmp_path):
    manager = ExecutionManager()
    manager.register_runtime(PythonRuntime())
    manager.register_runtime(ShellRuntime())
    if NodeRuntime is not None and NodeRuntime.available():
        manager.register_runtime(NodeRuntime())
    try:
        receipts = await run_backend_conformance(
            manager, backend="local", workspace_root=str(tmp_path)
        )
        assert receipts
        assert all(receipt.passed for receipt in receipts), receipts
        assert all("execution" in receipt.checks for receipt in receipts)
    finally:
        await manager.close_all()
