import pytest

from athena.execution.conformance import run_backend_conformance, run_backend_passport
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
    assert all(receipt.passed for receipt in receipts), receipts
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


@pytest.mark.asyncio
async def test_local_runtime_matrix_is_behaviorally_proven():
    manager = ExecutionManager()
    manager.register_runtime(PythonRuntime())
    manager.register_runtime(ShellRuntime())
    if NodeRuntime is not None and NodeRuntime.available():
        manager.register_runtime(NodeRuntime())
    try:
        receipts = await run_backend_conformance(manager, backend="local")
        assert receipts
        assert all(receipt.passed for receipt in receipts), receipts
        assert all("execution" in receipt.checks for receipt in receipts)
    finally:
        await manager.close_all()
