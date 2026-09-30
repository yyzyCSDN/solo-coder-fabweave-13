from __future__ import annotations

from fabweave.calendar.capacity import CapacityCalendar
from fabweave.core.timebox import TimeBox


def test_rules_stack_on_same_day_with_explicit_precedence():
    calendar = CapacityCalendar()
    date = 1
    day_start = date * 24 * 60

    calendar.add_short_shift("short", date, 540, 960, recorded_at=10)
    calendar.add_holiday("holiday", date, recorded_at=20)
    calendar.add_overtime("overtime", date, 1080, 1140, recorded_at=30)
    calendar.add_line_stop("stop", date, 1110, 1170, recorded_at=40)

    result = calendar.capacity_for(date, as_of=40)

    assert result.available_minutes == 30
    assert result.available_segments[0].interval == TimeBox(day_start + 1080, day_start + 1110)
    assert result.available_segments[0].applied_rule_id == "overtime"
    assert result.available_segments[0].applied_rule_kind == "overtime_declared"
    assert "holiday" in result.available_segments[0].overridden_rule_ids

    stop_segments = [segment for segment in result.segments if segment.applied_rule_id == "stop"]
    assert tuple(segment.interval for segment in stop_segments) == (
        TimeBox(day_start + 1110, day_start + 1140),
        TimeBox(day_start + 1140, day_start + 1170),
    )
    assert stop_segments[0].applied_rule_kind == "line_stop_declared"
    assert "overtime" in stop_segments[0].overridden_rule_ids
    assert "overtime" not in stop_segments[1].overridden_rule_ids


def test_adjacent_half_open_intervals_do_not_double_count_or_gap():
    calendar = CapacityCalendar()
    date = 2
    day_start = date * 24 * 60

    calendar.add_line_stop("stop-a", date, 600, 660, recorded_at=1)
    calendar.add_line_stop("stop-b", date, 660, 720, recorded_at=2)
    calendar.add_overtime("overtime", date, 720, 780, recorded_at=3)

    result = calendar.capacity_for(date, as_of=3)

    assert tuple(segment.interval for segment in result.blocked_segments) == (
        TimeBox(day_start + 600, day_start + 660),
        TimeBox(day_start + 660, day_start + 720),
    )
    overtime_segment = next(
        segment for segment in result.available_segments if segment.applied_rule_id == "overtime"
    )
    assert overtime_segment.interval == TimeBox(day_start + 720, day_start + 780)
    assert result.available_minutes == 24 * 60 - 120


def test_historical_capacity_uses_rules_visible_at_the_as_of_instant():
    calendar = CapacityCalendar()
    date = 3

    historical = calendar.capacity_for(date, as_of=10)
    assert historical.rules_version == 1
    assert historical.available_minutes == 24 * 60

    # A later correction is available to new queries, but cannot change the
    # immutable result that was visible when the historical date was reviewed.
    calendar.add_holiday("late-holiday", date, recorded_at=20)

    reviewed_again = calendar.capacity_for(date, as_of=10)
    latest = calendar.capacity_for(date, as_of=20)

    assert reviewed_again.available_minutes == historical.available_minutes
    assert reviewed_again.rules_version == 1
    assert latest.available_minutes == 0
    assert latest.rules_version == 2
    assert latest.blocked_segments[0].applied_rule_id == "late-holiday"


def test_short_shift_uses_base_shift_without_opening_unscheduled_time():
    calendar = CapacityCalendar()
    calendar.set_base_shift(
        intervals=[TimeBox(480, 960), TimeBox(1080, 1200)],
        recorded_at=1,
    )
    date = 4
    day_start = date * 24 * 60

    calendar.add_short_shift("short", date, 540, 900, recorded_at=2)
    result = calendar.capacity_for(date, as_of=2)

    assert result.available_minutes == 360
    assert tuple(segment.interval for segment in result.available_segments) == (
        TimeBox(day_start + 540, day_start + 900),
    )
    short_blocked = next(
        segment for segment in result.blocked_segments if segment.applied_rule_id == "short"
    )
    assert short_blocked.interval == TimeBox(day_start + 480, day_start + 540)
