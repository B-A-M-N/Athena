"""Unit tests for trigger next-fire computation (§76)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from athena.scheduler.triggers import TriggerSpec, TriggerType, next_fire
from athena.scheduler.scheduler import _template_from_job

UTC = timezone.utc


def _dt(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


async def test_once_fires_once_then_exhausted():
    t0 = _dt(2026, 1, 1, 12, 0, 0)
    trigger = TriggerSpec(type=TriggerType.ONCE, at=t0)
    assert next_fire(trigger, _dt(2026, 1, 1, 11, 0, 0)) == t0
    # After the fire time, an ONCE trigger is exhausted.
    assert next_fire(trigger, t0) is None


async def test_interval_times():
    base = _dt(2026, 1, 1, 0, 0, 0)
    one = TriggerSpec(type=TriggerType.INTERVAL, at=base, interval_seconds=60, times=1)
    assert next_fire(one, base - timedelta(minutes=1)) == base
    assert next_fire(one, base) is None

    three = TriggerSpec(type=TriggerType.INTERVAL, at=base, interval_seconds=60, times=3)
    fires = []
    cursor = base - timedelta(minutes=1)
    for _ in range(4):
        nxt = next_fire(three, cursor)
        if nxt is None:
            break
        fires.append(nxt)
        cursor = nxt
    assert fires == [base, base + timedelta(minutes=1), base + timedelta(minutes=2)]


async def test_interval_without_times_fires_indefinitely():
    base = _dt(2026, 1, 1, 0, 0, 0)
    trigger = TriggerSpec(type=TriggerType.INTERVAL, at=base, interval_seconds=60)
    prev = base - timedelta(minutes=1)
    for i in range(5):
        nxt = next_fire(trigger, prev)
        assert nxt is not None
        assert nxt == base + timedelta(minutes=i)
        prev = nxt


async def test_cron_every_minute_advances_one_minute():
    trigger = TriggerSpec(type=TriggerType.CRON, cron="* * * * *")
    t0 = _dt(2026, 3, 15, 10, 30, 15)
    nxt = next_fire(trigger, t0)
    assert nxt == _dt(2026, 3, 15, 10, 31, 0)


async def test_event_trigger_is_advanced_by_event_delivery():
    trigger = TriggerSpec(
        type=TriggerType.EVENT,
        event_name="ArtifactCreated",
        event_filters={"kind": "report"},
    )
    assert next_fire(trigger, _dt(2026, 3, 15, 10, 30, 15)) is None


async def test_scheduled_template_rehydrates_acceptance_criteria():
    template = _template_from_job(
        {
            "id": "job-1",
            "name": "maintenance",
            "payload": {
                "template": {
                    "objective": "check",
                    "acceptance_criteria": [
                        {
                            "id": "check",
                            "description": "tests pass",
                            "verification": {
                                "type": "command",
                                "command": "pytest -q",
                            },
                        }
                    ],
                }
            },
        }
    )
    spec = template.build_task_spec("job-1")
    assert spec.acceptance_criteria[0].verification is not None
    assert spec.acceptance_criteria[0].verification.command == "pytest -q"
