import asyncio
import json
from types import SimpleNamespace

from athena.packs.manager import PackManager
from athena.affordances import CapabilityFabric
from athena.capabilities.registry import CapabilityRegistry
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import WorkspaceSpec
from athena.capabilities.packs import PacksCapability
from athena.mcp.adapter import MCPAdapter


class _PackStore:
    def __init__(self):
        self.values = {}
        self.contribution_values = {}

    async def save(self, state):
        self.values[state.id] = state

    async def get(self, pack_id):
        return self.values.get(pack_id)

    async def list(self):
        return list(self.values.values())

    async def set_enabled(self, pack_id, enabled):
        state = self.values.get(pack_id)
        if state is None:
            return None
        from dataclasses import replace

        state = replace(state, enabled=enabled)
        self.values[pack_id] = state
        return state

    async def delete(self, pack_id):
        return self.values.pop(pack_id, None) is not None

    async def save_contribution(self, pack_id, kind, contribution_id):
        self.contribution_values.setdefault(pack_id, []).append(
            {
                "kind": kind,
                "contribution_id": contribution_id,
            }
        )

    async def contributions(self, pack_id):
        return list(self.contribution_values.get(pack_id, ()))

    async def delete_contributions(self, pack_id):
        self.contribution_values.pop(pack_id, None)


def _pack(root):
    pack = root / "example-pack"
    (pack / "skills").mkdir(parents=True)
    (pack / "skills" / "review.md").write_text("Review changed files.", encoding="utf-8")
    (pack / "athena.pack.toml").write_text(
        "id = 'example-pack'\n"
        "version = '1.0.0'\n"
        "publisher = 'test'\n"
        "[provides]\n"
        "skills = ['skills/review.md']\n"
        "[authority]\n"
        "requested_effects = ['READ_LOCAL']\n",
        encoding="utf-8",
    )
    return pack


def test_rehydrate_records_individual_pack_failure(tmp_path):
    store = _PackStore()
    manager = PackManager(store, install_root=str(tmp_path / "installed"))
    good = SimpleNamespace(id="good-pack", enabled=True)
    bad = SimpleNamespace(id="bad-pack", enabled=True)
    store.values = {good.id: good, bad.id: bad}

    manager.bind_integrations(fabric=object())

    async def activate(state):
        if state.id == bad.id:
            raise ValueError("missing payload")

    manager._activate = activate

    assert asyncio.run(manager.rehydrate_enabled()) == 1
    assert manager.rehydration_failures() == [{"pack_id": "bad-pack", "error": "missing payload"}]


def test_declarative_pack_is_validated_installed_and_health_checked(tmp_path):
    store = _PackStore()
    manager = PackManager(store, install_root=str(tmp_path / "installed"))
    source = _pack(tmp_path)

    inspected = manager.inspect_source(str(source), allowed_root=str(tmp_path))
    assert inspected["valid"] is True
    assert inspected["executable_code_loaded"] is False
    state = asyncio.run(manager.install(str(source), allowed_root=str(tmp_path)))
    assert state.enabled is True
    assert manager.health(state)["status"] == "healthy"


def test_pack_capability_exposes_search_and_rejects_workspace_escape(tmp_path):
    store = _PackStore()
    manager = PackManager(store, install_root=str(tmp_path / "installed"))
    capability = PacksCapability(manager)
    source = _pack(tmp_path)
    request = CapabilityRequest(
        capability_id="packs",
        arguments={"operation": "inspect", "source_path": str(source)},
        task_id="task-1",
        call_id="pack-call",
    )
    result = asyncio.run(
        capability.invoke(
            request,
            context=SimpleNamespace(
                workspace=WorkspaceSpec(id="repo", root=str(tmp_path / "other"))
            ),
        )
    )
    assert result.status.value == "failed"
    assert "pack source" in (result.error or "")


def test_pack_upgrade_removes_superseded_payload_after_install(tmp_path):
    store = _PackStore()
    manager = PackManager(store, install_root=str(tmp_path / "installed"))
    source = _pack(tmp_path)
    asyncio.run(manager.install(str(source), allowed_root=str(tmp_path)))

    (source / "skills" / "review.md").write_text(
        "Review changed files and tests.", encoding="utf-8"
    )
    (source / "athena.pack.toml").write_text(
        "id = 'example-pack'\n"
        "version = '2.0.0'\n"
        "publisher = 'test'\n"
        "[provides]\n"
        "skills = ['skills/review.md']\n"
        "[authority]\n"
        "requested_effects = ['READ_LOCAL']\n",
        encoding="utf-8",
    )
    second = asyncio.run(manager.upgrade(str(source), allowed_root=str(tmp_path)))

    assert second.manifest.version == "2.0.0"
    assert not (tmp_path / "installed" / "example-pack" / "1.0.0").exists()
    assert (tmp_path / "installed" / "example-pack" / "2.0.0").is_dir()


def test_pack_contributions_enter_and_leave_live_surfaces(tmp_path):
    source = tmp_path / "integrated-pack"
    (source / "skills").mkdir(parents=True)
    (source / "workflows").mkdir()
    (source / "capabilities").mkdir()
    (source / "skills" / "review.md").write_text(
        "---\nname: pack-review\ndescription: review files\n---\nUse the review workflow.",
        encoding="utf-8",
    )
    (source / "workflows" / "review.json").write_text(
        json.dumps(
            {
                "id": "review",
                "name": "Pack review",
                "description": "Inspect the workspace",
                "steps": [
                    {
                        "id": "profile",
                        "capability": "workspace",
                        "arguments": {"operation": "profile"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (source / "capabilities" / "profile.json").write_text(
        json.dumps(
            {
                "id": "repo.profile",
                "target": "workspace",
                "description": "Profile this repository",
                "defaults": {"operation": "profile"},
            }
        ),
        encoding="utf-8",
    )
    (source / "athena.pack.toml").write_text(
        "id = 'integrated-pack'\nversion = '1.0.0'\npublisher = 'test'\n"
        "[provides]\nskills = ['skills/review.md']\n"
        "workflows = ['workflows/review.json']\n"
        "capabilities = ['capabilities/profile.json']\n"
        "[authority]\nrequested_effects = ['READ_LOCAL']\n",
        encoding="utf-8",
    )

    class _Skills:
        def __init__(self):
            self.values = {}

        async def install(self, skill):
            self.values[skill.id] = skill

        async def disable(self, skill_id):
            self.values[skill_id] = self.values[skill_id].__class__(
                **{**self.values[skill_id].__dict__, "enabled": False}
            )

        async def enable(self, skill_id):
            if skill_id in self.values:
                self.values[skill_id] = self.values[skill_id].__class__(
                    **{**self.values[skill_id].__dict__, "enabled": True}
                )

        async def archive(self, skill_id):
            self.values.pop(skill_id, None)

    class _Workflows:
        def __init__(self):
            self.values = {}

        async def save(self, workflow):
            self.values[workflow.id] = workflow

        async def get(self, workflow_id, **kwargs):
            return self.values.get(workflow_id)

        async def delete(self, workflow_id):
            self.values.pop(workflow_id, None)

    class _Target:
        descriptor = CapabilityDescriptor(
            id="workspace",
            description="workspace",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.READ_LOCAL}),
        )

    class _Dispatcher:
        async def dispatch(self, request, **kwargs):
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output='{"ok": true}',
            )

    store = _PackStore()
    registry = CapabilityRegistry()
    registry.register(_Target())
    fabric = CapabilityFabric(registry)
    skills = _Skills()
    workflows = _Workflows()
    manager = PackManager(store, install_root=str(tmp_path / "installed"))
    manager.bind_integrations(
        skill_lifecycle=skills,
        workflow_store=workflows,
        fabric=fabric,
        dispatcher=_Dispatcher(),
    )

    state = asyncio.run(manager.install(str(source), allowed_root=str(tmp_path)))
    assert state.enabled is True
    assert len(skills.values) == 1
    workflow_id = "pack:integrated-pack:workflow:1"
    assert workflow_id in workflows.values
    assert registry.resolve("repo.profile").origin.value == "plugin"

    asyncio.run(manager.disable(state.id))
    assert workflows.values[workflow_id].enabled is False
    try:
        registry.resolve("repo.profile")
    except Exception:
        pass
    else:
        raise AssertionError("disabled pack alias remains registered")

    asyncio.run(manager.enable(state.id))
    assert workflows.values[workflow_id].enabled is True
    assert registry.resolve("repo.profile").origin.value == "plugin"

    asyncio.run(manager.uninstall(state.id))
    assert workflow_id not in workflows.values


def test_pack_mcp_server_enters_live_surface_and_rehydrates(tmp_path, monkeypatch):
    source = tmp_path / "mcp-pack"
    (source / "mcp").mkdir(parents=True)
    (source / "mcp" / "servers.json").write_text(
        json.dumps(
            {
                "servers": [{"name": "repo-tools", "command": "trusted-helper"}],
            }
        ),
        encoding="utf-8",
    )
    (source / "athena.pack.toml").write_text(
        "id = 'mcp-pack'\nversion = '1.0.0'\npublisher = 'test'\n"
        "[provides]\nmcp_servers = ['mcp/servers.json']\n"
        "[authority]\nrequested_effects = ['NETWORK_READ', 'SPAWN_PROCESS']\n",
        encoding="utf-8",
    )

    class _Client:
        instances = []

        def __init__(self, connection_id, **kwargs):
            self.connection_id = connection_id
            self.connected = False
            self.closed = False
            self.kwargs = kwargs
            self.__class__.instances.append(self)

        async def connect(self):
            self.connected = True
            return self

        async def close(self):
            self.connected = False
            self.closed = True

        async def list_tools(self):
            from athena.mcp.client import MCPToolRef

            return [
                MCPToolRef(
                    name="inspect_remote",
                    description="Inspect the remote project",
                    annotations={"readOnlyHint": True},
                )
            ]

    monkeypatch.setattr("athena.packs.manager.MCPClient", _Client)
    store = _PackStore()
    registry = CapabilityRegistry()
    adapter = MCPAdapter(registry)
    manager = PackManager(store, install_root=str(tmp_path / "installed"))
    manager.bind_integrations(mcp_adapter=adapter)

    state = asyncio.run(manager.install(str(source), allowed_root=str(tmp_path)))
    capability_id = "mcp:pack_mcp-pack_repo-tools:inspect_remote"
    assert capability_id in adapter.capability_ids()
    assert len(_Client.instances) == 1
    assert store.contribution_values[state.id] == [
        {"kind": "mcp", "contribution_id": "pack:mcp-pack:repo-tools"},
    ]

    asyncio.run(manager.disable(state.id))
    assert capability_id not in adapter.capability_ids()
    assert _Client.instances[0].closed is True

    asyncio.run(manager.enable(state.id))
    assert capability_id in adapter.capability_ids()
    assert len(_Client.instances) == 2


def test_pack_instrument_is_a_governed_callable_surface(tmp_path):
    source = tmp_path / "instrument-pack"
    (source / "instruments").mkdir(parents=True)
    (source / "instruments" / "graph.json").write_text(
        json.dumps(
            {
                "id": "repo.graph",
                "target": "workspace",
                "view": {
                    "id": "dependency-graph",
                    "kind": "graph",
                    "title": "Dependencies",
                    "payload": {"nodes": ["a", "b"], "edges": [["a", "b"]]},
                },
            }
        ),
        encoding="utf-8",
    )
    (source / "athena.pack.toml").write_text(
        "id = 'instrument-pack'\nversion = '1.0.0'\npublisher = 'test'\n"
        "[provides]\ninstruments = ['instruments/graph.json']\n"
        "[authority]\nrequested_effects = ['READ_LOCAL']\n",
        encoding="utf-8",
    )

    class _Target:
        descriptor = CapabilityDescriptor(
            id="workspace",
            description="workspace",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.READ_LOCAL}),
        )

    class _Dispatcher:
        async def dispatch(self, request, **kwargs):
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output="workspace-data",
            )

    store = _PackStore()
    registry = CapabilityRegistry()
    registry.register(_Target())
    manager = PackManager(store, install_root=str(tmp_path / "installed"))
    manager.bind_integrations(
        fabric=CapabilityFabric(registry),
        dispatcher=_Dispatcher(),
    )
    state = asyncio.run(manager.install(str(source), allowed_root=str(tmp_path)))

    instrument = registry.executor_for("repo.graph")
    result = asyncio.run(
        instrument.invoke(
            CapabilityRequest(
                capability_id="repo.graph",
                arguments={},
                task_id="task-1",
                call_id="call-1",
            ),
            context=SimpleNamespace(
                workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
            ),
        )
    )
    assert result.status is CapabilityResultStatus.OK
    assert result.metadata["instrument"]["kind"] == "graph"
    assert result.metadata["instrument"]["title"] == "Dependencies"

    asyncio.run(manager.disable(state.id))
    try:
        registry.resolve("repo.graph")
    except Exception:
        pass
    else:
        raise AssertionError("disabled pack instrument remains registered")
