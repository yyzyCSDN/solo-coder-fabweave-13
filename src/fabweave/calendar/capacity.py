from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from itertools import chain

from fabweave.core.events import EventLog
from fabweave.core.timebox import TimeBox, merge_timeboxes


class RulePriority(IntEnum):
    """Winner for rules covering the same half-open minute."""

    BASE_SHIFT = 0
    SHORT_SHIFT = 20
    HOLIDAY = 40
    OVERTIME = 60
    LINE_STOP = 70


@dataclass(frozen=True, order=True, slots=True)
class _Patch:
    rank: tuple[int, int, int, str]
    start: int
    end: int
    closed: bool
    rule_id: str
    kind: str

    @property
    def interval(self) -> TimeBox:
        return TimeBox(self.start, self.end)


@dataclass(frozen=True, slots=True)
class ResolvedSegment:
    """One open or closed piece of the queried day."""

    interval: TimeBox
    available: bool
    applied_rule_id: str
    applied_rule_kind: str
    overridden_rule_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapacityDay:
    """Capacity visible for a date at one transaction-time version."""

    date: int
    as_of: int
    rules_version: int
    available_minutes: int
    segments: tuple[ResolvedSegment, ...]

    @property
    def available_segments(self) -> tuple[ResolvedSegment, ...]:
        return tuple(segment for segment in self.segments if segment.available)

    @property
    def blocked_segments(self) -> tuple[ResolvedSegment, ...]:
        return tuple(segment for segment in self.segments if not segment.available)

    @property
    def applied_rule_ids(self) -> tuple[str, ...]:
        rule_ids = chain.from_iterable(
            [segment.applied_rule_id, *segment.overridden_rule_ids]
            for segment in self.segments
        )
        return tuple(dict.fromkeys(rule_ids))


class CapacityCalendar:
    """Append-only, bitemporal capacity calendar.

    Rule event dates are production time. ``recorded_at`` is transaction time:
    a rule added today can apply to an old production date, but is invisible to
    an ``as_of`` query made before it was recorded. Consequently, later rule
    entry cannot silently alter a historical capacity result that was already
    viewed at an earlier transaction time.
    """

    _STREAM = "capacity-calendar"
    _BASE_ID = "base-shift"
    _DAY_LENGTH = 24 * 60

    def __init__(self, event_log: EventLog | None = None) -> None:
        self._log = event_log if event_log is not None else EventLog()
        if not self._log.read(self._STREAM):
            self.set_base_shift(recorded_at=0)

    @property
    def revision(self) -> int:
        return len(self._log.read(self._STREAM))

    def set_base_shift(
        self,
        *,
        intervals: list[TimeBox] | None = None,
        recorded_at: int = 0,
    ) -> int:
        """Publish default shift offsets, visible to queries from ``recorded_at``.

        Offsets are minutes from local midnight, for example ``TimeBox(480, 960)``
        is an 08:00-16:00 shift. Omit ``intervals`` for an all-day baseline.
        """

        selected = self._base_intervals(intervals)
        return self._append(
            f"{self._BASE_ID}-revision-{self.revision + 1}",
            recorded_at,
            "base_shift_set",
            {"intervals": [(item.start, item.end) for item in selected]},
        )

    def add_holiday(self, rule_id: str, date: int, *, recorded_at: int = 0) -> int:
        return self._add_full_day_rule(rule_id, date, recorded_at, "holiday_declared")

    def add_line_stop(
        self,
        rule_id: str,
        date: int,
        start_offset: int,
        end_offset: int,
        *,
        recorded_at: int = 0,
    ) -> int:
        return self._add_interval_rule(
            rule_id,
            date,
            start_offset,
            end_offset,
            recorded_at,
            "line_stop_declared",
        )

    def add_short_shift(
        self,
        rule_id: str,
        date: int,
        open_offset: int,
        close_offset: int,
        *,
        recorded_at: int = 0,
    ) -> int:
        interval = self._offset_interval(date, open_offset, close_offset)
        return self._append(
            rule_id,
            recorded_at,
            "short_shift_declared",
            {"start": interval.start, "end": interval.end, "date": date},
        )

    def add_overtime(
        self,
        rule_id: str,
        date: int,
        start_offset: int,
        end_offset: int,
        *,
        recorded_at: int = 0,
    ) -> int:
        return self._add_interval_rule(
            rule_id,
            date,
            start_offset,
            end_offset,
            recorded_at,
            "overtime_declared",
        )

    def capacity_for(self, date: int, as_of: int) -> CapacityDay:
        """Return the capacity result visible for ``date`` at transaction time ``as_of``."""

        if as_of < 0:
            raise ValueError("as_of cannot be negative")
        visible = [event for event in self._log.read(self._STREAM) if event.occurred_at <= as_of]
        if not visible:
            raise LookupError("no calendar revision was visible at that time")

        patches: list[_Patch] = []
        base_event = self._latest_base_event(visible)
        base_intervals = self._decode_intervals(base_event.data["intervals"])
        day = TimeBox(date * self._DAY_LENGTH, (date + 1) * self._DAY_LENGTH)

        base_shift_intervals = [
            TimeBox(day.start + interval.start, day.start + interval.end)
            for interval in base_intervals
        ]
        for interval in base_shift_intervals:
            patches.append(
                self._patch(
                    self._BASE_ID,
                    interval,
                    closed=False,
                    kind="base_shift_set",
                    priority=RulePriority.BASE_SHIFT,
                    recorded_at=base_event.occurred_at,
                    sequence=base_event.sequence,
                )
            )

        for event in (item for item in visible if item.kind != "base_shift_set"):
            interval = TimeBox(event.data["start"], event.data["end"]).intersection(day)
            if interval is None:
                continue
            if event.kind == "short_shift_declared":
                # A shortened shift replaces only scheduled time. Its closed mask
                # is the old shift minus the new window; the open interval then
                # keeps that window available wherever this rule is the winner.
                for covered in self._subtract_intervals(base_shift_intervals, [interval]):
                    patches.append(
                        self._patch(
                            event.event_id,
                            covered,
                            closed=True,
                            kind=event.kind,
                            priority=RulePriority.SHORT_SHIFT,
                            recorded_at=event.occurred_at,
                            sequence=event.sequence,
                        )
                    )
                patches.append(
                    self._patch(
                        event.event_id,
                        interval,
                        closed=False,
                        kind=event.kind,
                        priority=RulePriority.SHORT_SHIFT,
                        recorded_at=event.occurred_at,
                        sequence=event.sequence,
                    )
                )
            elif event.kind == "holiday_declared":
                patches.append(
                    self._patch(
                        event.event_id,
                        interval,
                        closed=True,
                        kind=event.kind,
                        priority=RulePriority.HOLIDAY,
                        recorded_at=event.occurred_at,
                        sequence=event.sequence,
                    )
                )
            elif event.kind == "overtime_declared":
                patches.append(
                    self._patch(
                        event.event_id,
                        interval,
                        closed=False,
                        kind=event.kind,
                        priority=RulePriority.OVERTIME,
                        recorded_at=event.occurred_at,
                        sequence=event.sequence,
                    )
                )
            elif event.kind == "line_stop_declared":
                patches.append(
                    self._patch(
                        event.event_id,
                        interval,
                        closed=True,
                        kind=event.kind,
                        priority=RulePriority.LINE_STOP,
                        recorded_at=event.occurred_at,
                        sequence=event.sequence,
                    )
                )

        segments = self._resolve(patches, day)
        return CapacityDay(
            date=date,
            as_of=as_of,
            rules_version=len(visible),
            available_minutes=sum(
                segment.interval.duration for segment in segments if segment.available
            ),
            segments=tuple(segments),
        )

    def _append(
        self,
        event_id: str,
        recorded_at: int,
        kind: str,
        data: dict[str, object],
    ) -> int:
        if recorded_at < 0:
            raise ValueError("recorded_at cannot be negative")
        events = self._log.read(self._STREAM)
        event = self._log.append(
            self._STREAM,
            len(events),
            event_id,
            recorded_at,
            kind,
            data,
        )
        return event.sequence

    def _add_full_day_rule(
        self,
        rule_id: str,
        date: int,
        recorded_at: int,
        kind: str,
    ) -> int:
        day_length = self._DAY_LENGTH
        interval = TimeBox(date * day_length, (date + 1) * day_length)
        return self._append(
            rule_id,
            recorded_at,
            kind,
            {"start": interval.start, "end": interval.end, "date": date},
        )

    def _add_interval_rule(
        self,
        rule_id: str,
        date: int,
        start_offset: int,
        end_offset: int,
        recorded_at: int,
        kind: str,
    ) -> int:
        interval = self._offset_interval(date, start_offset, end_offset)
        return self._append(
            rule_id,
            recorded_at,
            kind,
            {"start": interval.start, "end": interval.end, "date": date},
        )

    @classmethod
    def _offset_interval(cls, date: int, start_offset: int, end_offset: int) -> TimeBox:
        day_length = cls._DAY_LENGTH
        if not (0 <= start_offset < day_length):
            raise ValueError("start_offset must be within the date")
        if not (start_offset < end_offset <= day_length):
            raise ValueError("end_offset must exceed start_offset and stay within the date")
        return TimeBox(date * day_length + start_offset, date * day_length + end_offset)

    @classmethod
    def _base_intervals(cls, intervals: list[TimeBox] | None) -> list[TimeBox]:
        day_length = cls._DAY_LENGTH
        if intervals is None:
            return [TimeBox(0, day_length)]
        day = TimeBox(0, day_length)
        clipped = [part for interval in intervals if (part := interval.intersection(day))]
        ordered = sorted(clipped)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.end > current.start:
                raise ValueError("base shift intervals cannot overlap")
        return merge_timeboxes(ordered)

    @staticmethod
    def _decode_intervals(values: list[tuple[int, int]]) -> list[TimeBox]:
        return [TimeBox(start, end) for start, end in values]

    @staticmethod
    def _subtract_intervals(minuends: list[TimeBox], subtrahends: list[TimeBox]) -> list[TimeBox]:
        differences: list[TimeBox] = []
        for minuend in minuends:
            start = minuend.start
            clipped_subtrahends = [
                part
                for subtrahend in subtrahends
                if (part := subtrahend.intersection(minuend)) is not None
            ]
            for subtrahend in merge_timeboxes(clipped_subtrahends):
                clipped_start = max(start, subtrahend.start)
                if start < clipped_start:
                    differences.append(TimeBox(start, clipped_start))
                start = max(start, subtrahend.end)
            if start < minuend.end:
                differences.append(TimeBox(start, minuend.end))
        return differences

    @staticmethod
    def _latest_base_event(events: list) -> object:
        return [event for event in events if event.kind == "base_shift_set"][-1]

    @staticmethod
    def _patch(
        rule_id: str,
        interval: TimeBox,
        *,
        closed: bool,
        kind: str,
        priority: RulePriority,
        recorded_at: int,
        sequence: int,
    ) -> _Patch:
        # Higher priority wins. For the same kind, later transaction entry wins;
        # event sequence makes same-recorded-at ordering deterministic.
        return _Patch(
            rank=(priority, recorded_at, sequence, rule_id),
            start=interval.start,
            end=interval.end,
            closed=closed,
            rule_id=rule_id,
            kind=kind,
        )

    def _resolve(self, patches: list[_Patch], day: TimeBox) -> list[ResolvedSegment]:
        if not patches:
            return [
                ResolvedSegment(
                    day,
                    available=False,
                    applied_rule_id="unscheduled",
                    applied_rule_kind="unscheduled",
                    overridden_rule_ids=(),
                )
            ]

        boundaries = sorted(
            {
                day.start,
                day.end,
                *(value for patch in patches for value in (patch.start, patch.end)),
            }
        )
        resolved: list[ResolvedSegment] = []
        for start, end in zip(boundaries, boundaries[1:]):
            active = [patch for patch in patches if patch.start <= start and end <= patch.end]
            if not active:
                resolved.append(
                    ResolvedSegment(
                        TimeBox(start, end),
                        available=False,
                        applied_rule_id="unscheduled",
                        applied_rule_kind="unscheduled",
                        overridden_rule_ids=(),
                    )
                )
                continue
            winner = max(active)
            overridden = tuple(
                dict.fromkeys(
                    patch.rule_id
                    for patch in active
                    if patch.rule_id and patch.rule_id != winner.rule_id
                )
            )
            applied = winner.rule_id or "unscheduled"
            resolved.append(
                ResolvedSegment(
                    TimeBox(start, end),
                    available=not winner.closed,
                    applied_rule_id=applied,
                    applied_rule_kind=winner.kind,
                    overridden_rule_ids=overridden,
                )
            )

        merged: list[ResolvedSegment] = []
        for segment in resolved:
            if merged and self._can_merge(merged[-1], segment):
                previous = merged[-1]
                merged[-1] = ResolvedSegment(
                    TimeBox(previous.interval.start, segment.interval.end),
                    available=previous.available,
                    applied_rule_id=previous.applied_rule_id,
                    applied_rule_kind=previous.applied_rule_kind,
                    overridden_rule_ids=previous.overridden_rule_ids,
                )
            else:
                merged.append(segment)
        return merged

    @staticmethod
    def _can_merge(left: ResolvedSegment, right: ResolvedSegment) -> bool:
        return (
            left.interval.end == right.interval.start
            and left.available == right.available
            and left.applied_rule_id == right.applied_rule_id
            and left.applied_rule_kind == right.applied_rule_kind
            and left.overridden_rule_ids == right.overridden_rule_ids
        )
