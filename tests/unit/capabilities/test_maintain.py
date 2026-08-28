from __future__ import annotations

import json
from pathlib import Path

import pytest

from athena.capabilities.maintain import MaintenanceCapability
from athena.capabilities.watch import WatchRegistry
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.protocol.tasks import WorkspaceSpec


class _Schedule:
    def __init__(self):
        self.jobs = []

    async def create(self, **kwargs):
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs.append(
            {
                "id": job_id,
                "enabled": True,
                "metadata": {},
                "template": {
                    "metadata": kwargs["metadata"],
                    "trigger": kwargs["trigger"],
                    "workspace_root": kwargs.get("workspace_root"),
                },
            }
        )
        return {"job_id": job_id}

    async def list_jobs(self, **kwargs):
        return list(self.jobs)

    async def disable(self, job_id, **kwargs):
        for job in self.jobs:
            if job["id"] == job_id:
                job["enabled"] = False
                return True
        return False

    async def enable(self, job_id, **kwargs):
        for job in self.jobs:
            if job["id"] == job_id:
                job["enabled"] = True
                return True
        return False

    async def delete(self, job_id, **kwargs):
        before = len(self.jobs)
        self.jobs[:] = [job for job in self.jobs if job["id"] != job_id]
        return len(self.jobs) != before


def _request(operation, **arguments):
    return CapabilityRequest(
        capability_id="maintain",
        task_id="task-maintain",
        call_id=f"maintain-{operation}",
        arguments={"operation": operation, **arguments},
    )


@pytest.mark.asyncio
async def test_maintenance_contract_persists_primary_and_fallback_triggers():
    schedule = _Schedule()
    capability = MaintenanceCapability(schedule)
    result = await capability.invoke(
        _request(
            "create",
            claim="tests remain green",
            observe={"paths": ["src/"]},
            verify={"capability_id": "execute", "arguments": {"command": "pytest -q"}},
            remediation={"workflow_id": "workflow.repair"},
            trigger={"type": "event", "event_name": "WatchObserved"},
            fallback_interval_seconds=600,
        ),
        context=type(
            "Context",
            (),
            {
                "workspace": WorkspaceSpec(id="repo", root="/tmp"),
            },
        )(),
    )

    assert result.status is CapabilityResultStatus.OK
    contract = json.loads(result.output)
    assert contract["status"] == "ACTIVE"
    assert len(contract["jobs"]) == 2
    assert len(schedule.jobs) == 2
    assert all(
        job["template"]["metadata"]["maintenance_contract"]["contract_id"]
        == contract["contract_id"]
        for job in schedule.jobs
    )


@pytest.mark.asyncio
async def test_maintenance_contract_can_be_disabled_and_listed():
    schedule = _Schedule()
    capability = MaintenanceCapability(schedule)
    created = await capability.invoke(
        _request(
            "create",
            claim="service stays healthy",
            observe={"service": "api"},
            verify={"capability_id": "service", "arguments": {"operation": "status"}},
            trigger={"type": "interval", "interval_seconds": 60},
        )
    )
    contract_id = json.loads(created.output)["contract_id"]

    disabled = await capability.invoke(_request("disable", contract_id=contract_id))
    assert disabled.status is CapabilityResultStatus.OK
    listed = await capability.invoke(_request("list"))
    payload = json.loads(listed.output)
    assert payload[0]["contract_id"] == contract_id
    assert payload[0]["status"] == "DISABLED"


@pytest.mark.asyncio
async def test_file_observer_is_event_scoped_and_has_durable_lifecycle(tmp_path):
    schedule = _Schedule()
    registry = WatchRegistry()
    capability = MaintenanceCapability(
        schedule,
        watch_registry=registry,
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )
    result = await capability.invoke(
        _request(
            "create",
            claim="configuration remains valid",
            observe={"kind": "file", "path": ".", "pattern": "*.toml"},
            verify={"capability_id": "execute", "arguments": {"command": "check"}},
            trigger={"type": "interval", "interval_seconds": 3600},
        ),
        context=type(
            "Context",
            (),
            {
                "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
            },
        )(),
    )

    assert result.status is CapabilityResultStatus.OK, result.error
    contract = json.loads(result.output)
    watch_id = contract["observe"]["watch_id"]
    assert watch_id in registry.file_watches
    assert len(schedule.jobs) == 2  # requested interval + WatchObserved trigger
    observer_job = next(
        job
        for job in schedule.jobs
        if job["template"]["metadata"]["maintenance_role"] == "observer"
    )
    assert observer_job["template"]["trigger"] == {
        "type": "event",
        "event_name": "WatchObserved",
        "event_filters": {"watch": watch_id},
    }
    trigger = observer_job["template"]["metadata"]["maintenance_contract"]
    assert trigger["observe"]["watch_id"] == watch_id

    disabled = await capability.invoke(_request("disable", contract_id=contract["contract_id"]))
    assert disabled.status is CapabilityResultStatus.OK
    assert watch_id not in registry.file_watches

    enabled = await capability.invoke(_request("enable", contract_id=contract["contract_id"]))
    assert enabled.status is CapabilityResultStatus.OK
    assert watch_id in registry.file_watches

    deleted = await capability.invoke(_request("delete", contract_id=contract["contract_id"]))
    assert deleted.status is CapabilityResultStatus.OK
    assert watch_id not in registry.file_watches
    assert schedule.jobs == []


@pytest.mark.asyncio
async def test_enabled_file_observer_rehydrates_from_persisted_contract(tmp_path):
    schedule = _Schedule()
    root = Path(tmp_path)
    capability = MaintenanceCapability(
        schedule,
        watch_registry=WatchRegistry(),
        workspace=WorkspaceSpec(id="repo", root=str(root)),
    )
    created = await capability.invoke(
        _request(
            "create",
            claim="artifact exists",
            observe={"kind": "file", "path": ".", "pattern": "*.json"},
            verify={"capability_id": "fs", "arguments": {"operation": "stat"}},
            trigger={"type": "event", "event_name": "WatchObserved"},
        )
    )
    contract = json.loads(created.output)
    old_watch_id = contract["observe"]["watch_id"]

    restored_registry = WatchRegistry()
    restored = MaintenanceCapability(
        schedule,
        watch_registry=restored_registry,
        workspace=WorkspaceSpec(id="repo", root=str(root)),
    )
    assert await restored.rehydrate() == 1
    assert set(restored_registry.file_watches) == {old_watch_id}
