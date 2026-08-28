import asyncio
from types import SimpleNamespace

from athena.capabilities.external_delegate import ExternalDelegateCapability
from athena.delegates.models import DelegateSpec
from athena.delegates.registry import DelegateRegistry
from athena.delegates.sessions import ExternalDelegateManager
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import WorkspaceSpec


class _Store:
    def __init__(self):
        self.values = {}

    async def save(self, session):
        self.values[session.id] = session

    async def get(self, session_id, *, task_id=None):
        session = self.values.get(session_id)
        return (
            session
            if session is not None and (task_id is None or session.task_id == task_id)
            else None
        )

    async def list(self, *, task_id=None):
        return [item for item in self.values.values() if task_id is None or item.task_id == task_id]

    async def update_state(self, session_id, state, *, task_id):
        from dataclasses import replace

        session = await self.get(session_id, task_id=task_id)
        if session is None:
            return None
        updated = replace(session, state=state)
        self.values[session_id] = updated
        return updated


class _Transport:
    def __init__(self):
        self.closed = False

    async def request(self, payload, *, timeout):
        if payload["type"] == "session.start":
            return {"session_id": "remote-1"}
        return {"answer": payload["objective"]}

    async def close(self):
        self.closed = True


def test_external_delegate_session_persists_and_can_be_reused():
    registry = DelegateRegistry()
    transport = _Transport()
    registry.register(
        DelegateSpec(id="reviewer", protocol="acp", endpoint="https://delegate.invalid"),
        connector=lambda **kwargs: transport,
    )
    store = _Store()
    manager = ExternalDelegateManager(registry, store)
    workspace = WorkspaceSpec(id="repo", root="/tmp/repo")

    async def run():
        session = await manager.start(
            "reviewer", task_id="task-1", session_id="s1", workspace=workspace
        )
        first = await manager.send(
            session.id, task_id="task-1", objective="inspect auth", workspace=workspace
        )
        second = await manager.send(
            session.id, task_id="task-1", objective="inspect tests", workspace=workspace
        )
        return session, first, second

    session, first, second = asyncio.run(run())
    assert session.remote_session_id == "remote-1"
    assert first["answer"] == "inspect auth"
    assert second["answer"] == "inspect tests"
    assert store.values[session.id].state == "active"


def test_remote_host_call_is_routed_through_dispatcher_and_capability_ceiling():
    class HostTransport(_Transport):
        async def request_host_call(self, payload, *, host_call, timeout):
            result = await host_call(
                {
                    "capability_id": "fs",
                    "arguments": {"operation": "read", "path": "README.md"},
                }
            )
            return {"host_result": result}

    class Dispatcher:
        async def dispatch(self, request, **kwargs):
            assert request.origin.value == "remote"
            assert kwargs["workspace"].id == "repo"
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output="contents",
            )

    registry = DelegateRegistry()
    transport = HostTransport()
    registry.register(
        DelegateSpec(
            id="builder",
            protocol="a2a",
            endpoint="https://delegate.invalid",
            capability_ceiling=("fs",),
        ),
        connector=lambda **kwargs: transport,
    )
    store = _Store()
    manager = ExternalDelegateManager(registry, store, dispatcher=Dispatcher())
    capability = ExternalDelegateCapability(manager)
    context = SimpleNamespace(workspace=WorkspaceSpec(id="repo", root="/tmp/repo"))
    request = CapabilityRequest(
        capability_id="delegate.external",
        arguments={"operation": "start", "specialist": "builder", "objective": "inspect"},
        task_id="task-1",
        call_id="call-1",
    )
    result = asyncio.run(capability.invoke(request, context=context))
    assert result.status is CapabilityResultStatus.OK
    assert "contents" in result.output


def test_remote_host_call_enforces_delegate_effect_ceiling_before_dispatch():
    class HostTransport(_Transport):
        async def request_host_call(self, payload, *, host_call, timeout):
            result = await host_call(
                {
                    "capability_id": "network",
                    "arguments": {"operation": "post", "url": "https://example.invalid"},
                }
            )
            return {"host_result": result}

    class Dispatcher:
        dispatched = False

        def resolve_effects(self, request, workspace):
            assert request.capability_id == "network"
            assert workspace.id == "repo"
            return (EffectClass.NETWORK_WRITE,)

        async def dispatch(self, request, **kwargs):
            self.dispatched = True
            raise AssertionError("effect-denied host call must not dispatch")

    registry = DelegateRegistry()
    transport = HostTransport()
    registry.register(
        DelegateSpec(
            id="read-only",
            protocol="a2a",
            endpoint="https://delegate.invalid",
            capability_ceiling=("network",),
            effect_ceiling=(EffectClass.NETWORK_READ.value,),
        ),
        connector=lambda **kwargs: transport,
    )
    store = _Store()
    dispatcher = Dispatcher()
    manager = ExternalDelegateManager(registry, store, dispatcher=dispatcher)
    capability = ExternalDelegateCapability(manager)
    context = SimpleNamespace(workspace=WorkspaceSpec(id="repo", root="/tmp/repo"))
    request = CapabilityRequest(
        capability_id="delegate.external",
        arguments={"operation": "start", "specialist": "read-only", "objective": "post"},
        task_id="task-1",
        call_id="call-1",
    )
    result = asyncio.run(capability.invoke(request, context=context))
    assert result.status is CapabilityResultStatus.OK
    assert "effect ceiling denied" in result.output
    assert not dispatcher.dispatched


def test_delegate_task_cleanup_closes_transport_and_durable_session():
    registry = DelegateRegistry()
    transport = _Transport()
    registry.register(
        DelegateSpec(id="reviewer", protocol="acp", endpoint="https://delegate.invalid"),
        connector=lambda **kwargs: transport,
    )
    store = _Store()
    manager = ExternalDelegateManager(registry, store)
    workspace = WorkspaceSpec(id="repo", root="/tmp/repo")

    async def run():
        session = await manager.start(
            "reviewer",
            task_id="task-terminal",
            session_id="s1",
            workspace=workspace,
        )
        closed = await manager.close_task("task-terminal")
        return session, closed

    session, closed = asyncio.run(run())
    assert closed == 1
    assert transport.closed is True
    assert store.values[session.id].state == "closed"


def test_delegate_effect_ceiling_rejects_unknown_effect():
    try:
        DelegateSpec(
            id="bad",
            protocol="a2a",
            endpoint="https://delegate.invalid",
            effect_ceiling=("NOT_AN_EFFECT",),
        )
    except ValueError as exc:
        assert "unknown effect" in str(exc)
    else:
        raise AssertionError("unknown delegate effect should be rejected")


def test_host_configured_command_delegate_can_resume_protocol_session(tmp_path):
    script = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    msg=json.loads(line)\n"
        "    if msg.get('type') == 'session.start':\n"
        "        out={'session_id':'command-remote'}\n"
        "    elif msg.get('type') == 'session.resume':\n"
        "        out={'ok':True}\n"
        "    else:\n"
        "        out={'answer':msg.get('objective')}\n"
        "    print(json.dumps(out), flush=True)\n"
    )
    registry = DelegateRegistry()
    registry.register(
        DelegateSpec(
            id="local-specialist",
            protocol="json_lines",
            command=("python", "-u", "-c", script),
        )
    )
    store = _Store()
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    async def run():
        manager = ExternalDelegateManager(registry, store)
        session = await manager.start(
            "local-specialist",
            task_id="task-1",
            session_id="s1",
            workspace=workspace,
        )
        first = await manager.send(
            session.id,
            task_id="task-1",
            objective="first",
            workspace=workspace,
        )
        # A fresh manager models a service restart. The persisted remote id
        # causes the transport to receive a resume handshake before the call.
        restarted = ExternalDelegateManager(registry, store)
        second = await restarted.send(
            session.id,
            task_id="task-1",
            objective="after restart",
            workspace=workspace,
        )
        await manager.close(session.id, task_id="task-1")
        await restarted.close(session.id, task_id="task-1")
        return first, second

    first, second = asyncio.run(run())
    assert first["answer"] == "first"
    assert second["answer"] == "after restart"
