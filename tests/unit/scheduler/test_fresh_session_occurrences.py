"""Scheduled work defaults to fresh sessions per occurrence, with lineage."""

from __future__ import annotations

import pytest

from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.ids import new_id
from athena.scheduler.scheduler import TaskTemplate
from athena.protocol.tasks import TaskStatus  # noqa: F401 - status contract reference


def _template(**kw) -> TaskTemplate:
    defaults = dict(
        objective="run the periodic check",
        capability_policy=None,
        model_policy=None,
        resource_budget=None,
    )
    defaults.update(kw)
    return TaskTemplate(**defaults)


def test_template_without_session_mints_fresh_session_per_occurrence():
    template = _template()

    first = template.build_task_spec("job-1", occurrence_key="job-1|t1")
    second = template.build_task_spec("job-1", occurrence_key="job-1|t2")

    assert first.session_id is not None
    assert second.session_id is not None
    assert first.session_id != second.session_id


def test_template_lineage_references_schedule_and_creator():
    template = _template(
        metadata={
            "_schedule_lineage": {
                "job_id": "job-1",
                "creator_task_id": "task-creator",
                "creator_session_id": "session-creator",
            }
        }
    )

    spec = template.build_task_spec("job-1", occurrence_key="job-1|t1")

    lineage = spec.metadata.get("_schedule_lineage")
    assert lineage == {
        "job_id": "job-1",
        "creator_task_id": "task-creator",
        "creator_session_id": "session-creator",
    }


def test_explicit_persistent_session_is_honored():
    template = _template(session_id="session-persistent")

    first = template.build_task_spec("job-1", occurrence_key="job-1|t1")
    second = template.build_task_spec("job-1", occurrence_key="job-1|t2")

    assert first.session_id == "session-persistent"
    assert second.session_id == "session-persistent"


@pytest.mark.asyncio
async def test_schedule_capability_defaults_to_fresh_sessions():
    from athena.capabilities.schedule import ScheduleAPI, ScheduleCapability

    class _Store:
        def __init__(self):
            self.jobs = {}

        async def upsert_job(
            self, job_id, name, *, payload, trigger_spec, enabled, next_run, metadata=None
        ):
            self.jobs[job_id] = {
                "id": job_id,
                "name": name,
                "payload": payload,
                "metadata": metadata or {},
            }

        async def list_jobs(self, enabled_only=True):
            return list(self.jobs.values())

    class _TaskManager:
        pass

    class _Scheduler:
        def __init__(self, store):
            self._store = store

    store = _Store()
    api = ScheduleAPI(_Scheduler(store), _TaskManager())
    capability = ScheduleCapability(api)

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="schedule",
            call_id=new_id("call"),
            task_id="task-creator",
            session_id="session-creator",
            arguments={
                "operation": "create",
                "name": "nightly check",
                "objective": "run the periodic check",
                "trigger": {"type": "interval", "interval_seconds": 3600},
            },
        )
    )

    assert result.status.name == "OK", result.error
    job = store.jobs[result and __import__("json").loads(result.output)["job_id"]]
    template = job["payload"]["template"]
    # Fresh sessions by default: the scheduling conversation's session is NOT
    # the execution session.
    assert template["session_id"] is None
    lineage = template["metadata"]["_schedule_lineage"]
    assert lineage["creator_task_id"] == "task-creator"
    assert lineage["creator_session_id"] == "session-creator"


@pytest.mark.asyncio
async def test_schedule_capability_persistent_session_opt_in():
    import json

    from athena.capabilities.schedule import ScheduleAPI, ScheduleCapability

    class _Store:
        def __init__(self):
            self.jobs = {}

        async def upsert_job(
            self, job_id, name, *, payload, trigger_spec, enabled, next_run, metadata=None
        ):
            self.jobs[job_id] = {"id": job_id, "payload": payload, "metadata": metadata or {}}

        async def list_jobs(self, enabled_only=True):
            return list(self.jobs.values())

    class _TaskManager:
        pass

    class _Scheduler:
        def __init__(self, store):
            self._store = store

    store = _Store()
    api = ScheduleAPI(_Scheduler(store), _TaskManager())
    capability = ScheduleCapability(api)

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="schedule",
            call_id=new_id("call"),
            task_id="task-creator",
            session_id="session-creator",
            arguments={
                "operation": "create",
                "name": "shared-context job",
                "objective": "run the periodic check",
                "trigger": {"type": "interval", "interval_seconds": 3600},
                "persistent_session": True,
            },
        )
    )

    assert result.status.name == "OK", result.error
    job = store.jobs[json.loads(result.output)["job_id"]]
    template = job["payload"]["template"]
    # Explicit opt-in: every occurrence shares the requesting session.
    assert template["session_id"] == "session-creator"


def test_occurrence_metadata_marks_occurrence_key():
    template = _template()

    spec = template.build_task_spec("job-1", occurrence_key="job-1|t1")

    assert spec.metadata["_occurrence"] == "job-1|t1"
