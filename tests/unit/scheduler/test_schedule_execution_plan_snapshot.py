"""Scheduled occurrences consume a persisted service-derived execution plan."""

from __future__ import annotations


from athena.scheduler.control import _authority_snapshot


def test_authority_snapshot_carries_derived_execution_plan():
    snapshot = _authority_snapshot(
        workspace=None,
        capability_policy=None,
        model_policy=None,
        resource_budget=None,
        autonomy=None,
        delivery=None,
        owner={},
    )
    assert "execution_plan" not in snapshot  # raw helper remains neutral


async def test_schedule_create_persists_execution_plan():
    from athena.protocol.tasks import AutonomyLevel
    from athena.scheduler.control import ScheduleAPI, ScheduleControl
    from athena.scheduler.scheduler import Scheduler
    from athena.state.database import Database
    from athena.state.schedules import ScheduleStore

    db = Database(":memory:")
    await db._ensure_ready()
    store = ScheduleStore(db)
    scheduler = Scheduler(store=store, task_manager=None)
    api = ScheduleAPI(scheduler, None)
    await api.create(
        name="complex recurring",
        objective="refactor authentication and update all callers",
        trigger={"type": "interval", "interval_seconds": 3600},
        control=ScheduleControl(
            origin="user_direct",
            task_id=None,
            session_id=None,
            principal_id="operator",
            project_id=None,
            capability_policy=None,
            model_policy=None,
            resource_budget=None,
            workspace=None,
            autonomy=AutonomyLevel.SUPERVISED,
            narrow_to_caller=False,
        ),
    )
    jobs = await store.list_jobs()
    assert jobs
    authority = jobs[0]["metadata"]["_authority_snapshot"]
    assert authority["execution_plan"]["work_class"] == "complex_coding"
    assert authority["execution_plan"]["speculation_depth"] == "single_candidate"
    assert authority["execution_plan"]["verification_floor"] == "strong"
    await db.close()


async def test_occurrence_sets_typed_execution_plan_from_authority_snapshot():

    from athena.protocol.tasks import AutonomyLevel
    from athena.scheduler.scheduler import TaskTemplate

    authority = {
        "execution_plan": {
            "work_class": "complex_coding",
            "speculation_depth": "multi_candidate",
            "isolation_floor": "speculative",
            "verification_floor": "strong",
        },
        "autonomy": AutonomyLevel.SUPERVISED.value,
    }
    template = TaskTemplate(
        objective="tell me a joke",
        authority_snapshot=authority,
    )
    spec = template.build_task_spec("job-occurrence", occurrence_key="job|now")
    assert spec.execution_plan is not None
    assert spec.execution_plan.work_class.value == "complex_coding"
    assert spec.execution_plan.speculation_depth.value == "multi_candidate"
    assert spec.execution_plan.verification_floor.value == "strong"
