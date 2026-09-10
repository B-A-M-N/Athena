"""Unit tests for trigger next-fire computation (§76)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from athena.scheduler.triggers import (
    TriggerSpec,
    TriggerType,
    _load_tz,
    _local_utc_candidates,
    next_fire,
)
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


async def test_interval_rejects_datetime_as_interval():
    with pytest.raises(ValueError, match="interval_seconds"):
        TriggerSpec(type=TriggerType.INTERVAL, at=_dt(2026, 1, 1))


@pytest.mark.parametrize(
    "expression",
    (
        "*/0 * * * *",
        "61 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 0",
        "* * 5-2 * *",
        "* * * * nope",
    ),
)
async def test_cron_rejects_invalid_fields(expression):
    with pytest.raises(ValueError):
        TriggerSpec(type=TriggerType.CRON, cron=expression)


async def test_cron_accepts_named_ranges_and_steps():
    trigger = TriggerSpec(type=TriggerType.CRON, cron="*/15 9-17 * jan-mar mon-fri")
    assert next_fire(trigger, _dt(2026, 1, 5, 8, 59)) == _dt(2026, 1, 5, 9, 0)


async def test_cron_every_minute_advances_one_minute():
    trigger = TriggerSpec(type=TriggerType.CRON, cron="* * * * *")
    t0 = _dt(2026, 3, 15, 10, 30, 15)
    nxt = next_fire(trigger, t0)
    assert nxt == _dt(2026, 3, 15, 10, 31, 0)


async def test_cron_dst_fall_back_preserves_real_occurrences():
    trigger = TriggerSpec(type=TriggerType.CRON, cron="0 1 * * *", timezone="America/Chicago")
    first = next_fire(trigger, datetime(2026, 11, 1, 0, 59, tzinfo=UTC))
    assert first == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    second = next_fire(trigger, datetime(2026, 11, 1, 6, 1, tzinfo=UTC))
    assert second == datetime(2026, 11, 2, 7, 0, tzinfo=UTC)


async def test_cron_dst_fall_back_returns_second_fold_after_first_fold():
    trigger = TriggerSpec(type=TriggerType.CRON, cron="0 1 * * *", timezone="America/Chicago")
    first = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert next_fire(trigger, first) == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)


async def test_cron_dst_end_at_stops_between_fall_back_occurrences():
    trigger = TriggerSpec(
        type=TriggerType.CRON,
        cron="0 1 * * *",
        timezone="America/Chicago",
        end_at=datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
    )
    first = datetime(2026, 11, 1, 5, 59, tzinfo=UTC)
    assert next_fire(trigger, first) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert next_fire(trigger, datetime(2026, 11, 1, 6, 0, tzinfo=UTC)) is None


async def test_local_dst_resolution_rejects_gap_and_preserves_both_folds():
    chicago = _load_tz("America/Chicago")
    nonexistent = _local_utc_candidates(datetime(2026, 3, 8, 2, 30), chicago)
    repeated = _local_utc_candidates(datetime(2026, 11, 1, 1, 30), chicago)
    assert nonexistent == ()
    assert repeated == (
        datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 7, 30, tzinfo=UTC),
    )


async def test_cron_dst_spring_forward_skips_nonexistent_wall_time():
    trigger = TriggerSpec(type=TriggerType.CRON, cron="30 2 * * *", timezone="America/Chicago")
    next_occurrence = next_fire(trigger, datetime(2026, 3, 8, 7, 59, tzinfo=UTC))
    assert next_occurrence == datetime(2026, 3, 9, 7, 30, tzinfo=UTC)


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
