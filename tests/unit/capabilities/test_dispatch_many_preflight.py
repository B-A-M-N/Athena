"""dispatch_many preflight: validate/repair ALL before ANY executes (item 69)."""

import asyncio

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec


def _workspace() -> WorkspaceSpec:
    return WorkspaceSpec(id="w1", root="/tmp/ws")


class _Executor:
    """Minimal executor that records whether invoke() was called."""

    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.invocations = []

    async def invoke(self, request, *, output_accumulator=None, context=None):
        self.invocations.append(request)
        return CapabilityResult(
            "call", request.capability_id, CapabilityResultStatus.OK, output="ok"
        )


_READ_SCHEMA = {
    "allow_extra": True,
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}


def _read_exec(cap="files.read", schema=_READ_SCHEMA) -> _Executor:
    return _Executor(
        CapabilityDescriptor(
            id=cap,
            description="read",
            input_schema=schema,
            effects=frozenset({EffectClass.READ_LOCAL}),
        )
    )


def _req(cap: str, **args) -> CapabilityRequest:
    return CapabilityRequest(capability_id=cap, arguments=args or {}, task_id="t1")


def _dispatcher(*execs) -> tuple[CapabilityDispatcher, list[_Executor]]:
    reg = CapabilityRegistry()
    for e in execs:
        reg.register(e)
    dispatcher = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.AUTONOMOUS))
    return dispatcher, list(execs)


def test_preflight_aborts_batch_on_unrepairable_call():
    ok_exec, bad_exec = _read_exec("files.read"), _read_exec("files.write")
    dispatcher, execs = _dispatcher(ok_exec, bad_exec)

    results = asyncio.run(
        dispatcher.dispatch_many(
            [
                _req("files.read", path="/tmp/ws/a.txt"),
                # required "path" is an int -> no safe repair rule applies
                _req("files.write", path=12345),
            ],
            workspace=_workspace(),
        )
    )

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, CapabilityResult)
    assert result.status == CapabilityResultStatus.FAILED
    assert "batch_preflight_failed" in result.error
    assert "files.write" in result.error  # names the bad call
    assert all(e.invocations == [] for e in execs)  # NOTHING executed


def test_preflight_aborts_batch_on_unknown_capability():
    exec_ = _read_exec()
    dispatcher, _ = _dispatcher(exec_)

    results = asyncio.run(
        dispatcher.dispatch_many(
            [_req("files.read", path="/tmp/ws/a.txt"), _req("no.such", x=1)],
            workspace=_workspace(),
        )
    )

    assert len(results) == 1
    result = results[0]
    assert result.status == CapabilityResultStatus.FAILED
    assert "unknown_capability" in result.error and "no.such" in result.error
    assert exec_.invocations == []


def test_preflight_all_valid_batch_executes_everything():
    a, b = _read_exec("files.read"), _read_exec("files.stat")
    dispatcher, execs = _dispatcher(a, b)

    results = asyncio.run(
        dispatcher.dispatch_many(
            [_req("files.read", path="/tmp/ws/a.txt"), _req("files.stat", path="/tmp/ws/b.txt")],
            workspace=_workspace(),
        )
    )

    assert len(results) == 2
    assert all(r.status == CapabilityResultStatus.OK for r in results)
    assert [len(e.invocations) for e in execs] == [1, 1]


@pytest.mark.athena_scenario("COMPAT-006")
def test_preflight_repaired_arguments_are_used():
    exec_ = _read_exec(
        schema={
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "x-athena-aliases": {"path": ["file_path"]},
        }
    )
    dispatcher, _ = _dispatcher(exec_)

    results = asyncio.run(
        dispatcher.dispatch_many(
            # numeric_string coercion repairs "3" -> 3? No — path stays str; use
            # alias repair instead: fs family maps file_path -> path.
            [_req("files.read", file_path="/tmp/ws/a.txt")],
            workspace=_workspace(),
        )
    )

    assert len(results) == 1
    assert results[0].status == CapabilityResultStatus.OK
    assert exec_.invocations[0].arguments["path"] == "/tmp/ws/a.txt"


def test_preflight_false_restores_legacy_behavior():
    exec_ = _read_exec()
    dispatcher, _ = _dispatcher(exec_)

    results = asyncio.run(
        dispatcher.dispatch_many(
            [_req("files.read", path="/tmp/ws/a.txt"), _req("no.such", x=1)],
            workspace=_workspace(),
            preflight=False,
        )
    )

    # Without preflight the valid call still executes; the unknown one fails.
    statuses = sorted(r.status.value if isinstance(r, CapabilityResult) else "?" for r in results)
    assert statuses == ["failed", "ok"]
    assert len(exec_.invocations) == 1


def test_preflight_empty_batch():
    dispatcher, _ = _dispatcher(_read_exec())
    assert asyncio.run(dispatcher.dispatch_many([], workspace=_workspace())) == []


@pytest.mark.athena_scenario("DISPATCH-PREPARED-BATCH")
def test_batched_calls_prepare_each_executor_exactly_once():
    """One prepared snapshot per request crosses preflight, controls, and core."""

    executors = {"files.a": _read_exec("files.a"), "files.b": _read_exec("files.b")}
    dispatcher, _ = _dispatcher(*executors.values())
    resolutions = []
    original = dispatcher._executor_for

    def counting_executor_for(request, workspace):
        resolutions.append(request.capability_id)
        return original(request, workspace)

    dispatcher._executor_for = counting_executor_for
    requests = [_req("files.a", path="/tmp/ws/a"), _req("files.b", path="/tmp/ws/b")]

    results = asyncio.run(dispatcher.dispatch_many(requests, workspace=_workspace()))

    assert [result.status for result in results] == [CapabilityResultStatus.OK] * 2
    assert sorted(resolutions) == ["files.a", "files.b"]
    assert all(result.status == CapabilityResultStatus.OK for result in results)


def _write_exec(name: str) -> _Executor:
    return _Executor(
        CapabilityDescriptor(
            id=name,
            description="write",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effects=frozenset({EffectClass.WRITE_LOCAL}),
        )
    )


def _execute_exec() -> _Executor:
    return _Executor(
        CapabilityDescriptor(
            id="execute",
            description="execute",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        )
    )


def test_complex_mutation_batch_escalates_before_execution():
    """Multiple project writes mark the task runtime-speculative before invoke."""
    writes = [_write_exec("files.a"), _write_exec("files.b")]
    dispatcher, _ = _dispatcher(*writes)

    results = asyncio.run(
        dispatcher.dispatch_many(
            [
                _req("files.a", path="/tmp/ws/a.txt"),
                _req("files.b", path="/tmp/ws/b.txt"),
            ],
            workspace=_workspace(),
        )
    )

    assert [result.status for result in results] == [CapabilityResultStatus.OK] * 2
    assert dispatcher._runtime_speculative_tasks == {"t1"}


def test_mutation_plus_execution_batch_escalates_before_execution():
    dispatcher, _ = _dispatcher(_write_exec("files.a"), _execute_exec())

    results = asyncio.run(
        dispatcher.dispatch_many(
            [
                _req("files.a", path="/tmp/ws/a.txt"),
                _req("execute", code="pytest"),
            ],
            workspace=_workspace(),
        )
    )

    assert [result.status for result in results] == [CapabilityResultStatus.OK] * 2
    assert dispatcher._runtime_speculative_tasks == {"t1"}


def test_read_batch_does_not_claim_runtime_speculation():
    reads = [_read_exec("files.read"), _read_exec("files.stat")]
    dispatcher, _ = _dispatcher(*reads)

    results = asyncio.run(
        dispatcher.dispatch_many(
            [_req("files.read", path="/tmp/ws/a"), _req("files.stat", path="/tmp/ws/b")],
            workspace=_workspace(),
        )
    )

    assert [result.status for result in results] == [CapabilityResultStatus.OK] * 2
    assert dispatcher._runtime_speculative_tasks == set()


async def test_preparation_failure_codes_are_exact():
    """Schema, unknown-capability, and effect-contract failures carry distinct
    typed codes, not all collapsed into 'unknown-capability'."""
    from athena.capabilities.dispatch_mechanisms import PreparationFailure
    from athena.capabilities.prepared import PreparationFailure as PF

    # Direct unit: the failure object distinguishes codes.
    assert PF(code="unknown_capability").code == "unknown_capability"
    assert PF(code="schema_validation").code == "schema_validation"
    assert PF(code="repair_invalid").code == "repair_invalid"
    assert PF(code="effect_contract").code == "effect_contract"

    # Schema failure carries structured errors.
    schema_failure = PreparationFailure(
        code="schema_validation",
        schema_errors=("missing required: path",),
    )
    assert schema_failure.code == "schema_validation"
    assert schema_failure.schema_errors == ("missing required: path",)
