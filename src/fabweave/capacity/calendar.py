"""Capacity calendar.

Four kinds of daily adjustments can be stacked onto the same calendar day:

``line_down``     the line is stopped; this wins over everything else,
                  including approved overtime.
``overtime``      adds temporary production minutes, even on holidays, but
                  never re-opens minutes lost to a line stop.
``holiday``       the plant-wide holiday blocks the whole day.
``short_shift``   replaces the baseline shift with shortened windows for the
                  day (a set, not a subtraction).

Conflict precedence is explicit (higher number wins), per minute:

    line_down > overtime > holiday > short_shift > baseline shift

Minutes are always handled as half-open intervals, reusing ``TimeBox``, so
touching shifts do not overlap and available time is never double counted.

Published calendars are immutable snapshots kept in a ``RevisionHistory``.
Historical queries bind to the revision that was in effect at the query time,
so adding rules later never silently rewrites capacity that was already read.
A day that has been observed is locked: a later revision that would change it
is rejected unless the change is authorized through ``observed_corrections``,
which also appends an audit event.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum

from fabweave.core.events import EventLog
from fabweave.core.ids import deterministic_id
from fabweave.core.timebox import TimeBox, merge_timeboxes
from fabweave.core.versioning import Revision, RevisionHistory

# Minutes from local midnight.
DAY_START = 0
DAY_END = 24 * 60
DEFAULT_BASELINE: tuple[TimeBox, ...] = (TimeBox(9 * 60, 18 * 60),)  # 09:00-18:00


class EntryKind(str, Enum):
    """Capacity adjustment kinds.  Values never change once persisted."""

    LINE_DOWN = "line_down"
    OVERTIME = "overtime"
    HOLIDAY = "holiday"
    SHORT_SHIFT = "short_shift"


# Higher priority wins when layers claim the same minute.
PRIORITY: dict[EntryKind, int] = {
    EntryKind.SHORT_SHIFT: 10,
    EntryKind.HOLIDAY: 20,
    EntryKind.OVERTIME: 30,
    EntryKind.LINE_DOWN: 40,
}


@dataclass(frozen=True, order=True, slots=True)
class CalendarEntry:
    """One adjustment for one day.

    ``day`` is an abstract calendar ordinal (callers map real dates onto
    dense integers).  Times are minutes from midnight, held as half-open
    windows.  Blocking kinds (``line_down``, ``holiday``) default to the
    whole day when no windows are given; empty windows on the other kinds
    would be a meaningless adjustment and are rejected.
    """

    entry_id: str
    day: int
    kind: EntryKind
    windows: tuple[TimeBox, ...] = ()

    def __post_init__(self) -> None:
        if not self.entry_id or not self.entry_id.strip():
            raise ValueError("entry id is required")
        if self.day < 0:
            raise ValueError("day ordinal must be non-negative")
        if not isinstance(self.kind, EntryKind):
            raise ValueError("unknown entry kind")
        windows = merge_timeboxes(list(self.windows))
        for window in windows:
            if window.start < DAY_START or window.end > DAY_END:
                raise ValueError("windows must stay inside the day")
        if self.kind in {EntryKind.OVERTIME, EntryKind.SHORT_SHIFT} and not windows:
            raise ValueError(f"{self.kind.value} requires at least one window")
        if self.kind == EntryKind.HOLIDAY:
            windows = [TimeBox(DAY_START, DAY_END)]
        if self.kind == EntryKind.LINE_DOWN and not windows:
            windows = [TimeBox(DAY_START, DAY_END)]
        object.__setattr__(self, "windows", tuple(windows))


@dataclass(frozen=True, slots=True)
class Override:
    """Evidence that a higher-priority layer changed the outcome for windows."""

    winner: EntryKind
    winner_id: str
    available: bool
    overrode: EntryKind | None  # None means it overrode the base layer
    overrode_id: str | None
    windows: tuple[TimeBox, ...]

    def __str__(self) -> str:
        if self.overrode is None:
            scope = "opened_closed" if self.available else "closed_open"
        elif self.available:
            scope = self.overrode.value  # opened time the lower layer had blocked
        else:
            scope = f"open_{self.overrode.value}"  # blocked time the lower layer had opened
        return f"{self.winner.value}:{scope}:{tuple((w.start, w.end) for w in self.windows)}"


@dataclass(frozen=True, slots=True)
class DayCapacity:
    day: int
    available_windows: tuple[TimeBox, ...]
    blocked_windows: tuple[TimeBox, ...]
    available_minutes: int
    blocked_minutes: int
    overrides: tuple[Override, ...]


@dataclass(frozen=True, slots=True)
class Observation:
    """A historical read, bound forever to the revision seen at the time."""

    observed_at: int
    version: int
    capacity: DayCapacity


@dataclass(frozen=True, slots=True)
class Drift:
    """Difference between what was observed and what the latest revision says."""

    observed_version: int
    latest_version: int
    observed_minutes: int
    latest_minutes: int
    changed: bool

    @property
    def delta_minutes(self) -> int:
        return self.latest_minutes - self.observed_minutes


# --- Layer model used by the per-minute sweep --------------------------------


@dataclass(frozen=True, slots=True)
class _Layer:
    priority: int
    kind: EntryKind
    entry_id: str
    windows: tuple[TimeBox, ...]
    available: bool  # verdict of this layer on its own windows

    def speaks_at(self, minute: int) -> bool:
        return any(window.contains(minute) for window in self.windows)


def _complement(windows: list[TimeBox]) -> list[TimeBox]:
    result: list[TimeBox] = []
    cursor = DAY_START
    for window in merge_timeboxes(windows):
        if cursor < window.start:
            result.append(TimeBox(cursor, window.start))
        cursor = max(cursor, window.end)
    if cursor < DAY_END:
        result.append(TimeBox(cursor, DAY_END))
    return result


def resolve_day(
    day: int,
    entries: list[CalendarEntry] | tuple[CalendarEntry, ...],
    baseline: tuple[TimeBox, ...] = DEFAULT_BASELINE,
) -> DayCapacity:
    """Resolve every adjustment for one day into usable capacity.

    The baseline is a set of open windows; every minute outside it is closed.
    Adjustment layers are consulted in priority order on each minute of the
    day, so any number of entries can be stacked and the winner is always
    unambiguous.
    """

    for entry in entries:
        if entry.day != day:
            raise ValueError("resolve_day only accepts entries for the same day")

    baseline_windows = list(baseline)
    short_windows = [window for entry in entries if entry.kind == EntryKind.SHORT_SHIFT for window in entry.windows]

    # The base layer is the baseline shift, replaced wholesale by a short
    # shift for this day (set semantics, not a subtraction).
    base_windows = merge_timeboxes(short_windows) if short_windows else baseline_windows
    layers: list[_Layer] = [
        _Layer(0, EntryKind.SHORT_SHIFT, "baseline", tuple(base_windows), True),
    ]
    for entry in entries:
        # The short shift already replaced the base layer; it is not stacked
        # again as an adjustment layer.
        if entry.kind == EntryKind.SHORT_SHIFT:
            continue
        available = entry.kind == EntryKind.OVERTIME
        layers.append(_Layer(PRIORITY[entry.kind], entry.kind, entry.entry_id, entry.windows, available))
    layers.sort(key=lambda layer: (layer.priority, layer.entry_id))

    # Per-minute highest-priority speaking layer; ``available_winners`` records
    # the availability verdict, ``top`` keeps the deciding layer for evidence.
    available_winners: list[bool] = []
    top_layers: list[_Layer | None] = []
    for minute in range(DAY_START, DAY_END):
        top = next(
            (layer for layer in reversed(layers) if layer.speaks_at(minute)),
            None,
        )
        top_layers.append(top)
        available_winners.append(top is not None and top.available)

    available_windows = _segments([minute for minute, open_ in enumerate(available_winners) if open_])
    blocked_windows = _complement(available_windows)

    # Collect the minutes where the top layer actually changed the verdict,
    # keyed by winner / loser, then merge contiguous minutes into windows.
    override_minutes: dict[tuple[EntryKind, str, bool, EntryKind | None, str | None], list[int]] = {}
    for minute, top_layer in enumerate(top_layers):
        if top_layer is None or top_layer.priority == 0:
            continue
        # Highest-priority *adjustment* layer below the winner.
        lower = next(
            (
                layer
                for layer in reversed(layers)
                if 0 < layer.priority < top_layer.priority and layer.speaks_at(minute)
            ),
            None,
        )
        base_open = any(window.contains(minute) for window in base_windows)
        if lower is not None and lower.available != top_layer.available:
            # The winner directly overruled another adjustment layer.
            key = (top_layer.kind, top_layer.entry_id, top_layer.available, lower.kind, lower.entry_id)
        elif lower is None and top_layer.available != base_open:
            # The winner changed the base-layer verdict.
            key = (top_layer.kind, top_layer.entry_id, top_layer.available, None, None)
        else:
            continue
        override_minutes.setdefault(key, []).append(minute)

    override_records = tuple(
        Override(winner, winner_id, available, overrode, overrode_id, tuple(_segments(minutes)))
        for (winner, winner_id, available, overrode, overrode_id), minutes in sorted(
            override_minutes.items(),
            key=lambda item: (item[1][0], item[0][0].value),
        )
    )
    return DayCapacity(
        day,
        tuple(available_windows),
        tuple(blocked_windows),
        sum(window.duration for window in available_windows),
        sum(window.duration for window in blocked_windows),
        override_records,
    )


def _segments(minutes: list[int]) -> list[TimeBox]:
    """Turn a set of open minutes into maximal half-open windows."""

    result: list[TimeBox] = []
    if not minutes:
        return result
    start = previous = minutes[0]
    for minute in minutes[1:]:
        if minute == previous + 1:
            previous = minute
            continue
        result.append(TimeBox(start, previous + 1))  # end is exclusive
        start = previous = minute
    result.append(TimeBox(start, previous + 1))
    return result


# --- Versioned calendar ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Snapshot:
    entries: tuple[CalendarEntry, ...]
    days: dict[int, DayCapacity]


class CapacityCalendar:
    """Append-only, bitemporal capacity calendar.

    ``publish`` stores a new immutable revision effective at ``effective_at``
    and containing the full entry set.  All queries are answered through a
    revision: ``capacity_for(day, as_of=...)`` returns exactly what was in
    effect at ``as_of``.

    ``observe`` records that a day's capacity was read at a point in time and
    binds that read to the revision then in effect.  A later publication that
    changes an observed day is rejected unless that day is named in
    ``observed_corrections``; corrections append audit events to the log, so
    history can never be rewritten quietly.
    """

    def __init__(self, log: EventLog | None = None, baseline: tuple[TimeBox, ...] = DEFAULT_BASELINE) -> None:
        self._history: RevisionHistory[_Snapshot] = RevisionHistory()
        self._observations: dict[int, Observation] = {}
        self._log = log if log is not None else EventLog()
        self._baseline = tuple(merge_timeboxes(list(baseline)))
        for window in self._baseline:
            if window.start < DAY_START or window.end > DAY_END:
                raise ValueError("baseline windows must stay inside the day")

    def publish(
        self,
        version: int,
        effective_at: int,
        entries: list[CalendarEntry] | tuple[CalendarEntry, ...],
        *,
        observed_corrections: frozenset[int] | set[int] = frozenset(),
    ) -> Revision[_Snapshot]:
        normalized = tuple(deepcopy(entry) for entry in entries)
        by_day: dict[int, list[CalendarEntry]] = {}
        for entry in normalized:
            by_day.setdefault(entry.day, []).append(entry)

        days = {day: resolve_day(day, day_entries, self._baseline) for day, day_entries in by_day.items()}

        corrections = set(observed_corrections)
        unauthorized: list[int] = []
        changed_corrections: list[int] = []
        # An observed day is checked even if the new revision carries no
        # entries for it, because that would silently revert it to baseline.
        for day in sorted(set(self._observations) | set(days)):
            observation = self._observations.get(day)
            if observation is None:
                continue
            latest = days.get(day, resolve_day(day, (), self._baseline))
            changed = (
                latest.available_windows != observation.capacity.available_windows
                or latest.blocked_windows != observation.capacity.blocked_windows
            )
            if changed:
                if day not in corrections:
                    unauthorized.append(day)
                else:
                    changed_corrections.append(day)
        if unauthorized:
            raise ValueError(
                "publishing would silently change observed capacity for days: "
                + ", ".join(str(day) for day in unauthorized)
            )
        for day in corrections:
            if day not in self._observations:
                raise ValueError("observed correction named a day that was never observed")

        revision = self._history.publish(version, effective_at, _Snapshot(normalized, days))
        for day in changed_corrections:
            latest = days.get(day, resolve_day(day, (), self._baseline))
            observation = self._observations[day]
            stream = f"capacity-amendment:{day}"
            expected_sequence = len(self._log.read(stream))
            self._log.append(
                stream,
                expected_sequence,
                deterministic_id("CAPAMD", day, version, observation.version),
                effective_at,
                "capacity_amended",
                {
                    "day": day,
                    "observed_version": observation.version,
                    "new_version": version,
                    "observed_minutes": observation.capacity.available_minutes,
                    "new_minutes": latest.available_minutes,
                },
            )
        return revision

    def observe(self, day: int, at: int) -> Observation:
        """Record a read of ``day`` as it stood at ``at`` and bind it."""

        revision = self._history.at(at)
        capacity = self._capacity(revision, day)
        existing = self._observations.get(day)
        if existing is not None:
            return existing
        observation = Observation(at, revision.version, capacity)
        self._observations[day] = observation
        return observation

    def observed_capacity(self, day: int) -> DayCapacity:
        observation = self._observations.get(day)
        if observation is None:
            raise LookupError(f"day {day} has never been observed")
        return observation.capacity

    def drift_for(self, day: int) -> Drift:
        observation = self._observations.get(day)
        if observation is None:
            raise LookupError(f"day {day} has never been observed")
        latest_revision = self._history.latest()
        latest = self._capacity(latest_revision, day)
        return Drift(
            observation.version,
            latest_revision.version,
            observation.capacity.available_minutes,
            latest.available_minutes,
            latest.available_windows != observation.capacity.available_windows
            or latest.blocked_windows != observation.capacity.blocked_windows,
        )

    def observed_days(self) -> tuple[int, ...]:
        return tuple(sorted(self._observations))

    def capacity_for(self, day: int, *, as_of: int) -> DayCapacity:
        """Answer strictly from the revision effective at ``as_of``."""

        return self._capacity(self._history.at(as_of), day)

    def latest_capacity(self, day: int) -> DayCapacity:
        return self._capacity(self._history.latest(), day)

    def _capacity(self, revision: Revision[_Snapshot], day: int) -> DayCapacity:
        resolved = revision.value.days.get(day)
        if resolved is None:
            resolved = resolve_day(day, (), self._baseline)
        return DayCapacity(
            day,
            resolved.available_windows,
            resolved.blocked_windows,
            resolved.available_minutes,
            resolved.blocked_minutes,
            resolved.overrides,
        )
