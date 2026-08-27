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
    dispatcher = CapabilityDispatcher(reg, PolicyEngine(AutonomyLevel.SUPERVISED))
    return dispatcher, list(execs)


def test_preflight_aborts_batch_on_unrepairable_call():
    ok_exec, bad_exec = _read_exec("files.read"), _read_exec("files.write")
    dispatcher, execs = _dispatcher(ok_exec, bad_exec)

    results = asyncio.run(dispatcher.dispatch_many(
        [
            _req("files.read", path="/tmp/ws/a.txt"),
            # required "path" is an int -> no safe repair rule applies
            _req("files.write", path=12345),
        ],
        workspace=_workspace(),
    ))

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

    results = asyncio.run(dispatcher.dispatch_many(
        [_req("files.read", path="/tmp/ws/a.txt"), _req("no.such", x=1)],
        workspace=_workspace(),
    ))

    assert len(results) == 1
    result = results[0]
    assert result.status == CapabilityResultStatus.FAILED
    assert "unknown-capability" in result.error and "no.such" in result.error
    assert exec_.invocations == []


def test_preflight_all_valid_batch_executes_everything():
    a, b = _read_exec("files.read"), _read_exec("files.stat")
    dispatcher, execs = _dispatcher(a, b)

    results = asyncio.run(dispatcher.dispatch_many(
        [_req("files.read", path="/tmp/ws/a.txt"),
         _req("files.stat", path="/tmp/ws/b.txt")],
        workspace=_workspace(),
    ))

    assert len(results) == 2
    assert all(r.status == CapabilityResultStatus.OK for r in results)
    assert [len(e.invocations) for e in execs] == [1, 1]


@pytest.mark.athena_scenario("COMPAT-006")
def test_preflight_repaired_arguments_are_used():
    exec_ = _read_exec(schema={
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "x-athena-aliases": {"path": ["file_path"]},
    })
    dispatcher, _ = _dispatcher(exec_)

    results = asyncio.run(dispatcher.dispatch_many(
        # numeric_string coercion repairs "3" -> 3? No — path stays str; use
        # alias repair instead: fs family maps file_path -> path.
        [_req("files.read", file_path="/tmp/ws/a.txt")],
        workspace=_workspace(),
    ))

    assert len(results) == 1
    assert results[0].status == CapabilityResultStatus.OK
    assert exec_.invocations[0].arguments["path"] == "/tmp/ws/a.txt"


def test_preflight_false_restores_legacy_behavior():
    exec_ = _read_exec()
    dispatcher, _ = _dispatcher(exec_)

    results = asyncio.run(dispatcher.dispatch_many(
        [_req("files.read", path="/tmp/ws/a.txt"), _req("no.such", x=1)],
        workspace=_workspace(),
        preflight=False,
    ))

    # Without preflight the valid call still executes; the unknown one fails.
    statuses = sorted(r.status.value if isinstance(r, CapabilityResult) else "?"
                      for r in results)
    assert statuses == ["failed", "ok"]
    assert len(exec_.invocations) == 1


def test_preflight_empty_batch():
    dispatcher, _ = _dispatcher(_read_exec())
    assert asyncio.run(dispatcher.dispatch_many([], workspace=_workspace())) == []
