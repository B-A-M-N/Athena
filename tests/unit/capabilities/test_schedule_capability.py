"""Behavioral coverage for scheduler capability management operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from athena.capabilities.schedule import (
    ScheduleAPI,
    ScheduleControl,
    _grant_allows,
    _workspace_rules_cover,
)
from athena.scheduler.scheduler import TriggerType
from athena.state.database import Database
from athena.state.schedules import ScheduleStore
from athena.protocol.tasks import CapabilityPolicy, ResourceBudget, WorkspaceSpec


class _Scheduler:
    def __init__(self, store):
        self._store = store


async def _api():
    db = Database(":memory:")
    await db._ensure_ready()
    return db, ScheduleAPI(_Scheduler(ScheduleStore(db)), task_manager=None)


async def test_schedule_api_round_trips_jobs_and_enforces_owner_visibility():
    db, api = await _api()
    owner = {"task_id": "task-a", "session_id": "session-a", "project_id": "repo-a"}
    other = {"task_id": "task-b", "session_id": "session-b", "project_id": "repo-b"}

    created = await api.create(
        name="nightly verification",
        objective="verify the next release",
        trigger={"type": "interval", "interval_seconds": 60},
        session_id="session-a",
        owner=owner,
    )
    job_id = created["job_id"]

    job = await api.inspect(job_id, owner=owner)
    assert job is not None
    assert job["trigger"]["type"] == TriggerType.INTERVAL.value
    assert job["template"]["objective"] == "verify the next release"
    assert job["metadata"]["_owner"] == owner
    assert job["next_run"]

    assert len(await api.list_jobs(owner=owner)) == 1
    assert await api.list_jobs(owner=other) == []
    assert await api.inspect(job_id, owner=other) is None
    assert await api.disable(job_id, owner=other) is False
    assert await api.delete(job_id, owner=other) is False

    assert await api.disable(job_id, owner=owner) is True
    assert len(await api.list_jobs(owner=owner)) == 1
    assert await api.enable(job_id, owner=owner) is True
    assert await api.delete(job_id, owner=owner) is True
    assert await api.delete(job_id, owner=owner) is False
    await db.close()


@pytest.mark.parametrize(
    ("trigger", "message"),
    [
        ({"type": "once"}, "requires at"),
        ({"type": "interval", "interval_seconds": 0}, "must be positive"),
        ({"type": "cron", "cron": "* * *"}, "five fields"),
        ({"type": "event"}, "requires event_name"),
        ({"type": "once", "at": "not-a-date"}, "ISO-8601"),
    ],
)
async def test_schedule_api_rejects_incomplete_trigger_contract(trigger, message):
    db, api = await _api()
    with pytest.raises(ValueError, match=message):
        await api.create(
            name="invalid",
            objective="must fail",
            trigger=trigger,
            owner={"task_id": "task-a"},
        )
    await db.close()


async def test_model_schedule_control_requires_creator_authority_or_explicit_narrowing():
    db, api = await _api()
    owner = {
        "task_id": "task-a",
        "session_id": "session-a",
        "project_id": "repo-a",
        "principal_id": "principal-a",
    }
    workspace = WorkspaceSpec(id="repo-a", root="/repo")
    broad = CapabilityPolicy(effects=frozenset({"READ_LOCAL", "WRITE_LOCAL"}))
    created = await api.create(
        name="controlled",
        objective="run",
        trigger={"type": "interval", "interval_seconds": 60},
        owner=owner,
        workspace=workspace,
        capability_policy=broad,
        resource_budget=ResourceBudget(),
    )
    control = ScheduleControl(
        origin="model",
        task_id="task-a",
        session_id="session-a",
        principal_id="principal-a",
        project_id="repo-a",
        capability_policy=CapabilityPolicy(effects=frozenset({"READ_LOCAL"})),
        resource_budget=ResourceBudget(),
        workspace=workspace,
    )
    with pytest.raises(PermissionError):
        await api.disable(created["job_id"], owner=owner, control=control)

    narrowed = await api.update(
        created["job_id"],
        owner=owner,
        objective="narrowed",
        control=control,
        authority_mode="narrow_to_caller",
    )
    assert narrowed is not None
    assert narrowed["metadata"]["_authority_snapshot"]["capability_policy"]["effects"] == [
        "READ_LOCAL"
    ]
    await db.close()


async def test_model_cannot_see_ownerless_legacy_schedule():
    db, api = await _api()
    store = api._scheduler._store  # noqa: SLF001 - fixture boundary
    await store.upsert_job(
        "legacy-job",
        "legacy",
        payload={"template": {"objective": "legacy"}},
        trigger_spec={"type": "event", "event_name": "legacy"},
        enabled=True,
        next_run=None,
        metadata={},
    )
    control = ScheduleControl(origin="model", task_id="task-a", session_id="session-a")
    assert await api.inspect("legacy-job", owner={"task_id": "task-a"}, control=control) is None
    await db.close()


async def test_operator_grant_binds_schedule_control_to_task_without_bearer_token():
    db, api = await _api()
    owner = {
        "task_id": "task-a",
        "session_id": "session-a",
        "project_id": "repo-a",
        "principal_id": "principal-a",
    }
    workspace = WorkspaceSpec(id="repo-a", root="/repo")
    authority = CapabilityPolicy(effects=frozenset({"READ_LOCAL"}))
    created = await api.create(
        name="delegated",
        objective="run",
        trigger={"type": "interval", "interval_seconds": 60},
        owner=owner,
        workspace=workspace,
        capability_policy=authority,
        resource_budget=ResourceBudget(),
    )
    grant = await api.grant_control(
        created["job_id"],
        control=ScheduleControl(origin="user_direct"),
        task_id="task-b",
        principal_id="principal-a",
        project_id="repo-a",
    )
    assert grant is not None
    assert "control_token" not in grant
    stored = await api._scheduler._store.get_job_id(created["job_id"])
    assert all("token" not in item for item in stored["metadata"]["_delegated_control_grants"])

    delegated = ScheduleControl(
        origin="model",
        task_id="task-b",
        session_id="session-b",
        principal_id="principal-a",
        project_id="repo-a",
        capability_policy=authority,
        resource_budget=ResourceBudget(),
        workspace=workspace,
    )
    assert await api.disable(created["job_id"], owner={"task_id": "task-b"}, control=delegated)
    assert await api.revoke_control(
        created["job_id"], control=ScheduleControl(origin="user_direct")
    )
    await db.close()


async def test_task_bound_grant_enforces_operation_scope_and_revoke_preserves_creator(monkeypatch):
    db, api = await _api()
    owner = {
        "task_id": "task-a",
        "session_id": "session-a",
        "project_id": "repo-a",
        "principal_id": "principal-a",
    }
    workspace = WorkspaceSpec(id="repo-a", root="/repo")
    authority = CapabilityPolicy(effects=frozenset({"READ_LOCAL", "WRITE_LOCAL"}))
    created = await api.create(
        name="delegated",
        objective="run",
        trigger={"type": "interval", "interval_seconds": 60},
        owner=owner,
        workspace=workspace,
        capability_policy=authority,
        resource_budget=ResourceBudget(),
    )
    job_id = created["job_id"]
    await api.grant_control(
        job_id,
        control=ScheduleControl(origin="user_direct"),
        task_id="task-b",
        principal_id="principal-a",
        project_id="repo-a",
        operations=("inspect",),
    )
    delegated = ScheduleControl(
        origin="model",
        task_id="task-b",
        session_id="session-b",
        principal_id="principal-a",
        project_id="repo-a",
        capability_policy=authority,
        resource_budget=ResourceBudget(),
        workspace=workspace,
    )

    assert await api.inspect(job_id, owner={"task_id": "task-b"}, control=delegated)
    assert await api.inspect(job_id, control=delegated)
    unauthorized = replace(delegated, task_id="task-c")
    assert await api.inspect(job_id, control=unauthorized) is None
    for operation in ("enable", "disable", "delete", "run"):
        with pytest.raises(PermissionError):
            await getattr(api, operation)(job_id, owner={"task_id": "task-b"}, control=delegated)
    with pytest.raises(PermissionError):
        await api.update(job_id, owner={"task_id": "task-b"}, control=delegated, objective="nope")

    assert await api.revoke_control(job_id, control=ScheduleControl(origin="user_direct"))
    assert await api.inspect(job_id, owner={"task_id": "task-b"}, control=delegated) is None
    assert await api.run(job_id, owner={"task_id": "task-b"}, control=delegated) is None
    assert await api.enable(job_id, owner={"task_id": "task-b"}, control=delegated) is False
    assert await api.disable(job_id, owner={"task_id": "task-b"}, control=delegated) is False

    creator = ScheduleControl(
        origin="model",
        task_id="task-a",
        session_id="session-a",
        principal_id="principal-a",
        project_id="repo-a",
        capability_policy=authority,
        resource_budget=ResourceBudget(),
        workspace=workspace,
    )
    assert await api.disable(job_id, owner=owner, control=creator)

    run_job = (
        await api.create(
            name="run-only",
            objective="run",
            trigger={"type": "interval", "interval_seconds": 60},
            owner=owner,
            workspace=workspace,
            capability_policy=authority,
            resource_budget=ResourceBudget(),
        )
    )["job_id"]
    await api.grant_control(
        run_job,
        control=ScheduleControl(origin="user_direct"),
        task_id="task-c",
        operations=("run",),
    )
    run_control = replace(delegated, task_id="task-c")

    async def fake_run_now(_job_id):
        return "occurrence-run-only"

    monkeypatch.setattr(api._scheduler, "run_now", fake_run_now, raising=False)
    assert (
        await api.run(run_job, owner={"task_id": "task-c"}, control=run_control)
        == "occurrence-run-only"
    )
    with pytest.raises(PermissionError):
        await api.update(
            run_job,
            owner={"task_id": "task-c"},
            control=run_control,
            objective="must remain denied",
        )
    await db.close()


async def test_grant_expiry_is_timezone_aware_and_malformed_expiry_is_atomic():
    db, api = await _api()
    owner = {"task_id": "task-a", "session_id": "session-a"}
    created = await api.create(
        name="expiry",
        objective="run",
        trigger={"type": "interval", "interval_seconds": 60},
        owner=owner,
    )
    job_id = created["job_id"]
    before = await api.inspect(job_id, owner=owner)
    assert before is not None
    with pytest.raises(ValueError, match="timezone-aware"):
        await api.grant_control(
            job_id,
            control=ScheduleControl(origin="user_direct"),
            task_id="task-b",
            expires_at="2026-09-09T11:00:00",
        )
    after = await api.inspect(job_id, owner=owner)
    assert after is not None
    assert after["metadata"].get("_delegated_control_grants") is None

    await api.grant_control(
        job_id,
        control=ScheduleControl(origin="user_direct"),
        task_id="task-offset",
        operations=("inspect",),
        expires_at="2026-09-09T11:00:00+02:00",
    )
    raw_job = await api._scheduler._store.get_job_id(job_id)
    assert raw_job["metadata"]["_delegated_control_grants"][-1]["expires_at"] == (
        "2026-09-09T09:00:00+00:00"
    )

    grant = {
        "operations": ["inspect"],
        "expires_at": "2026-09-09T11:00:00+02:00",
        "revoked": False,
    }
    before_offset = datetime(2026, 9, 9, 8, 30, tzinfo=timezone.utc)
    after_offset = datetime(2026, 9, 9, 9, 30, tzinfo=timezone.utc)
    assert _grant_allows(grant, operation="inspect", now=before_offset)
    assert not _grant_allows(grant, operation="inspect", now=after_offset)

    expired_job = (
        await api.create(
            name="expired",
            objective="run",
            trigger={"type": "interval", "interval_seconds": 60},
            owner=owner,
        )
    )["job_id"]
    await api.grant_control(
        expired_job,
        control=ScheduleControl(origin="user_direct"),
        task_id="task-expired",
        operations=("run",),
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )
    expired_control = ScheduleControl(
        origin="model",
        task_id="task-expired",
        session_id="session-expired",
    )
    assert await api.inspect(expired_job, control=expired_control) is None
    assert await api.run(expired_job, control=expired_control) is None
    await db.close()


def test_schedule_workspace_coverage_preserves_nested_denies():
    upper = [
        {"path": "/repo", "allow": True},
        {"path": "/repo/secret", "allow": False},
    ]
    lower = [{"path": "/repo", "allow": True}]
    assert not _workspace_rules_cover(upper, lower, upper_root="/repo", lower_root="/repo")

    narrowed = [
        {"path": "/repo", "allow": True},
        {"path": "/repo/secret", "allow": False},
    ]
    assert _workspace_rules_cover(upper, narrowed, upper_root="/repo", lower_root="/repo")
