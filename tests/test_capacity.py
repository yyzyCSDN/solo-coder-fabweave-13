from __future__ import annotations

import pytest

from fabweave.capacity import (
    DAY_END,
    DEFAULT_BASELINE,
    PRIORITY,
    CalendarEntry,
    CapacityCalendar,
    EntryKind,
    resolve_day,
)
from fabweave.core.events import EventLog
from fabweave.core.timebox import TimeBox

H = 60


def E(entry_id: str, day: int, kind: EntryKind, *windows: TimeBox) -> CalendarEntry:
    return CalendarEntry(entry_id, day, kind, windows=windows)


# --- Stacking and precedence -------------------------------------------------


def test_baseline_day_has_exactly_the_shift_minutes():
    day = resolve_day(1, [])
    assert day.available_windows == (TimeBox(9 * H, 18 * H),)
    assert day.available_minutes == 540
    assert day.blocked_minutes == DAY_END - 540
    assert day.overrides == ()


def test_all_four_kinds_stack_on_one_day_with_explicit_winner_per_minute():
    day = 1
    # Holiday kills the whole day; a short shift and OT exist as well.
    entries = [
        E("HOL", day, EntryKind.HOLIDAY),
        E("SHORT", day, EntryKind.SHORT_SHIFT, TimeBox(6 * H, 12 * H)),
        E("OT", day, EntryKind.OVERTIME, TimeBox(18 * H, 19 * H), TimeBox(7 * H, 8 * H)),
        E("DOWN", day, EntryKind.LINE_DOWN, TimeBox(18 * H + 30, 20 * H)),
    ]
    result = resolve_day(day, entries)

    # 07:00-08:00 OT survives the holiday (outside the line-down window);
    # 18:00-18:30 OT survives too; 18:30-20:00 is killed by the line stop.
    assert result.available_windows == (TimeBox(7 * H, 8 * H), TimeBox(18 * H, 18 * H + 30))
    assert result.available_minutes == 90
    blocked = merge(result.blocked_windows)
    assert any(window.start <= 18 * H + 30 and window.end >= 20 * H for window in blocked)

    winners = {(o.winner, o.overrode): o.windows for o in result.overrides}
    # Overtime opened holiday-blocked time ...
    assert (EntryKind.OVERTIME, EntryKind.HOLIDAY) in winners
    # ... and the line stop closed overtime minutes.
    assert (EntryKind.LINE_DOWN, EntryKind.OVERTIME) in winners


def merge(windows):
    out = []
    for window in sorted(windows):
        if out and out[-1].end >= window.start:
            out[-1] = TimeBox(out[-1].start, max(out[-1].end, window.end))
        else:
            out.append(window)
    return out


def test_line_down_beats_overtime_over_the_whole_day():
    result = resolve_day(
        2,
        [
            E("OT", 2, EntryKind.OVERTIME, TimeBox(6 * H, 20 * H)),
            E("DOWN", 2, EntryKind.LINE_DOWN),
        ],
    )
    assert result.available_minutes == 0
    assert result.available_windows == ()
    assert result.blocked_windows == (TimeBox(0, 24 * H),)


def test_overtime_reopens_holiday_but_holiday_still_blocks_baseline():
    result = resolve_day(
        3,
        [
            E("HOL", 3, EntryKind.HOLIDAY),
            E("OT", 3, EntryKind.OVERTIME, TimeBox(20 * H, 22 * H)),
        ],
    )
    assert result.available_windows == (TimeBox(20 * H, 22 * H),)
    assert result.available_minutes == 120
    # Holiday closing the baseline 09:00-18:00 is itself recorded evidence.
    assert any(
        o.winner == EntryKind.HOLIDAY and o.overrode is None and not o.available
        for o in result.overrides
    )


def test_short_shift_replaces_baseline_as_a_set_not_a_subtraction():
    # 06:00-08:00 and 13:00-15:00; baseline 09:00-18:00 must not survive.
    result = resolve_day(
        4,
        [E("SHORT", 4, EntryKind.SHORT_SHIFT, TimeBox(6 * H, 8 * H), TimeBox(13 * H, 15 * H))],
    )
    assert result.available_windows == (TimeBox(6 * H, 8 * H), TimeBox(13 * H, 15 * H))
    assert result.available_minutes == 240
    assert result.blocked_windows[0] == TimeBox(0, 6 * H)
    assert TimeBox(15 * H, 24 * H) in result.blocked_windows


def test_adjacent_adjustments_touch_without_double_counting():
    # OT ending exactly when line-down starts: minute at the boundary is open.
    result = resolve_day(
        5,
        [
            E("OT", 5, EntryKind.OVERTIME, TimeBox(6 * H, 7 * H)),
            E("DOWN", 5, EntryKind.LINE_DOWN, TimeBox(7 * H, 8 * H)),
        ],
    )
    # OT touches the line-down at 07:00; baseline 09:00-18:00 is open as
    # neither entry covers it.
    assert result.available_windows == (TimeBox(6 * H, 7 * H), TimeBox(9 * H, 18 * H))
    assert result.available_minutes == 60 + 540
    assert result.blocked_minutes == DAY_END - (60 + 540)


def test_multiple_line_down_windows_merge_against_one_overtime():
    result = resolve_day(
        6,
        [
            E("OT", 6, EntryKind.OVERTIME, TimeBox(6 * H, 12 * H)),
            E("D1", 6, EntryKind.LINE_DOWN, TimeBox(7 * H, 8 * H)),
            E("D2", 6, EntryKind.LINE_DOWN, TimeBox(10 * H, 11 * H)),
        ],
    )
    # OT minutes 11:00-12:00 run into the baseline at 12:00, so the last open
    # segment merges into the untouched 12:00-18:00 shift.
    assert result.available_windows == (
        TimeBox(6 * H, 7 * H),
        TimeBox(8 * H, 10 * H),
        TimeBox(11 * H, 18 * H),
    )
    assert result.available_minutes == 60 + 120 + 420
    # The line stops are visible as overrides over the overtime layer.
    assert sum(
        sum(w.duration for w in o.windows)
        for o in result.overrides
        if o.winner == EntryKind.LINE_DOWN and o.overrode == EntryKind.OVERTIME
    ) == 120


def test_priority_order_is_documented_and_strict():
    assert list(PRIORITY.values()) == sorted(PRIORITY.values())
    assert PRIORITY[EntryKind.LINE_DOWN] > PRIORITY[EntryKind.OVERTIME]
    assert PRIORITY[EntryKind.OVERTIME] > PRIORITY[EntryKind.HOLIDAY]
    assert PRIORITY[EntryKind.HOLIDAY] > PRIORITY[EntryKind.SHORT_SHIFT]


def test_overlapping_entry_windows_are_merged_and_validated():
    entry = E("OT", 7, EntryKind.OVERTIME, TimeBox(8 * H, 10 * H), TimeBox(9 * H, 11 * H))
    assert entry.windows == (TimeBox(8 * H, 11 * H),)
    with pytest.raises(ValueError):
        E("BAD", 7, EntryKind.OVERTIME)
    with pytest.raises(ValueError):
        E("BAD", 7, EntryKind.SHORT_SHIFT, TimeBox(-1, 10))
    with pytest.raises(ValueError):
        CalendarEntry("BAD", -1, EntryKind.HOLIDAY)


def test_holiday_and_line_down_default_to_the_whole_day():
    assert E("HOL", 8, EntryKind.HOLIDAY).windows == (TimeBox(0, DAY_END),)
    assert E("DOWN", 8, EntryKind.LINE_DOWN).windows == (TimeBox(0, DAY_END),)


def test_custom_baseline_is_respected():
    result = resolve_day(9, [], baseline=(TimeBox(8 * H, 12 * H), TimeBox(13 * H, 17 * H)))
    assert result.available_minutes == 480
    assert result.available_windows == (TimeBox(8 * H, 12 * H), TimeBox(13 * H, 17 * H))


# --- Versioning, as-of reads and the no-silent-rewrite guarantee -------------


def test_historical_reads_use_the_revision_effective_then():
    calendar = CapacityCalendar()
    calendar.publish(1, 100, [E("HOL", 1, EntryKind.HOLIDAY)])

    # Seen at t=150: version 1, zero capacity.
    seen = calendar.capacity_for(1, as_of=150)
    assert seen.available_minutes == 0

    # A later rule (effective 200) must not change the answer for t=150.
    calendar.publish(
        2,
        200,
        [
            E("HOL", 1, EntryKind.HOLIDAY),
            E("OT", 1, EntryKind.OVERTIME, TimeBox(20 * H, 22 * H)),
        ],
    )
    assert calendar.capacity_for(1, as_of=150).available_minutes == 0
    assert calendar.capacity_for(1, as_of=250).available_minutes == 120


def test_capacity_before_any_revision_fails_closed():
    calendar = CapacityCalendar()
    with pytest.raises(LookupError):
        calendar.capacity_for(1, as_of=50)


def test_observe_binds_first_view_and_later_observation_keeps_it():
    calendar = CapacityCalendar()
    calendar.publish(1, 100, [E("HOL", 1, EntryKind.HOLIDAY)])
    first = calendar.observe(1, 150)
    assert first.version == 1 and first.capacity.available_minutes == 0

    calendar.publish(
        2,
        200,
        [
            E("HOL", 1, EntryKind.HOLIDAY),
            E("OT", 1, EntryKind.OVERTIME, TimeBox(20 * H, 22 * H)),
        ],
        observed_corrections={1},
    )
    # Re-observing never silently rebinds the historical view.
    again = calendar.observe(1, 250)
    assert again == first
    assert calendar.observed_capacity(1).available_minutes == 0
    assert calendar.observed_days() == (1,)


def test_publishing_change_to_an_observed_day_is_rejected_without_authorization():
    calendar = CapacityCalendar()
    calendar.publish(1, 100, [E("HOL", 1, EntryKind.HOLIDAY)])
    calendar.observe(1, 150)

    with pytest.raises(ValueError, match="observed capacity"):
        calendar.publish(2, 200, [E("OT", 1, EntryKind.OVERTIME, TimeBox(20 * H, 22 * H))])

    # The rejected revision never landed.
    assert calendar.capacity_for(1, as_of=250).available_minutes == 0


def test_removing_rules_for_an_observed_day_is_also_a_rewrite():
    calendar = CapacityCalendar()
    calendar.publish(1, 100, [E("HOL", 1, EntryKind.HOLIDAY)])
    calendar.observe(1, 150)
    with pytest.raises(ValueError):
        calendar.publish(2, 200, [])  # would silently revert day 1 to baseline


def test_authorized_correction_is_audited_and_drift_is_visible():
    log = EventLog()
    calendar = CapacityCalendar(log)
    calendar.publish(1, 100, [E("HOL", 1, EntryKind.HOLIDAY)])
    calendar.observe(1, 150)

    calendar.publish(
        2,
        200,
        [E("OT", 1, EntryKind.OVERTIME, TimeBox(20 * H, 22 * H))],
        observed_corrections={1},
    )

    events = log.read("capacity-amendment:1")
    assert len(events) == 1
    assert events[0].kind == "capacity_amended"
    assert events[0].data["observed_version"] == 1
    assert events[0].data["new_version"] == 2
    # Holiday blocked the whole day; the revision adds baseline + 2h OT.
    assert events[0].data["new_minutes"] == 660

    drift = calendar.drift_for(1)
    assert drift.changed and drift.delta_minutes == 660
    assert drift.observed_version == 1 and drift.latest_version == 2
    # The bound historical view is still the old one.
    assert calendar.observed_capacity(1).available_minutes == 0


def test_second_correction_appends_a_second_audit_event():
    log = EventLog()
    calendar = CapacityCalendar(log)
    calendar.publish(1, 100, [E("HOL", 1, EntryKind.HOLIDAY)])
    calendar.observe(1, 150)
    calendar.publish(2, 200, [E("OT", 1, EntryKind.OVERTIME, TimeBox(20 * H, 22 * H))], observed_corrections={1})
    calendar.publish(
        3,
        300,
        [E("OT", 1, EntryKind.OVERTIME, TimeBox(20 * H, 23 * H))],
        observed_corrections={1},
    )
    events = log.read("capacity-amendment:1")
    assert [event.data["new_version"] for event in events] == [2, 3]


def test_unchanged_day_with_correction_is_accepted_without_event():
    log = EventLog()
    calendar = CapacityCalendar(log)
    calendar.publish(1, 100, [])
    calendar.observe(1, 150)
    # Naming a day that did not actually change is harmless and writes nothing.
    calendar.publish(2, 200, [], observed_corrections={1})
    assert log.read("capacity-amendment:1") == ()


def test_correction_for_never_observed_day_is_rejected():
    calendar = CapacityCalendar()
    calendar.publish(1, 100, [])
    with pytest.raises(ValueError):
        calendar.publish(2, 200, [E("HOL", 9, EntryKind.HOLIDAY)], observed_corrections={9})


def test_unrelated_new_days_do_not_trip_the_observed_lock():
    calendar = CapacityCalendar()
    calendar.publish(1, 100, [E("HOL", 1, EntryKind.HOLIDAY)])
    calendar.observe(1, 150)
    calendar.publish(2, 200, [E("HOL", 1, EntryKind.HOLIDAY), E("HOL2", 2, EntryKind.HOLIDAY)])
    assert calendar.capacity_for(2, as_of=250).available_minutes == 0


def test_published_entries_are_immutable_snapshots():
    calendar = CapacityCalendar()
    entries = [E("HOL", 1, EntryKind.HOLIDAY)]
    calendar.publish(1, 100, entries)
    # The snapshot keeps its own copy; later caller-side edits cannot reach it.
    entries.append(E("HACK", 1, EntryKind.LINE_DOWN))
    assert calendar.capacity_for(1, as_of=150).available_minutes == 0
    # And mutating a published entry's inputs cannot retrograde a revision.
    fresh = [E("OT", 2, EntryKind.OVERTIME, TimeBox(20 * H, 22 * H))]
    calendar.publish(2, 200, fresh)
    fresh.clear()
    assert calendar.capacity_for(2, as_of=250).available_minutes == 660


def test_revision_history_still_moves_forward():
    calendar = CapacityCalendar()
    calendar.publish(1, 100, [])
    with pytest.raises(ValueError):
        calendar.publish(1, 200, [])  # version must advance
    with pytest.raises(ValueError):
        calendar.publish(2, 90, [])  # cannot backdate


def test_drift_for_unobserved_day_fails():
    calendar = CapacityCalendar()
    calendar.publish(1, 100, [])
    with pytest.raises(LookupError):
        calendar.drift_for(3)
