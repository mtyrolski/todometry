"""Todoist activity synchronization and the date ranges verified by the API."""

from calendar import monthrange
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from todometry.core.types import Event
from todometry.core.utils import Cache
from todometry.database.db_activity import DatabaseActivity
from todometry.features.activity import (
    activity_datetime_utc,
    activity_history_boundary,
    activity_sync_lock,
    load_activity_cache,
    merge_activity_cache,
    needs_activity_history,
)

_DAY = timedelta(days=1)
_WEEK = timedelta(days=7)
_Interval = tuple[datetime, datetime]
_CoverageLabel = Literal["covered", "partial", "unverified"]


class _PeriodStatus(TypedDict):
    coverage: _CoverageLabel
    eventCount: int


class _CoverageTotals(TypedDict):
    days: dict[_CoverageLabel, int]
    weeks: dict[_CoverageLabel, int]


class ActivityHistoryStatus(TypedDict):
    timeZone: str
    today: str
    firstEventDate: str | None
    latestEventDate: str | None
    coverageStartDate: str | None
    lastSyncedAt: str | None
    years: dict[str, _PeriodStatus]
    months: dict[str, _PeriodStatus]
    days: dict[str, _PeriodStatus]
    weeks: dict[str, _CoverageLabel]
    summary: _CoverageTotals


@dataclass(frozen=True)
class SyncResult:
    events: set[Event]
    new_events: set[Event]


@dataclass
class _ActivityCoverage:
    intervals: list[_Interval] = field(default_factory=list)
    recent_cutoff: datetime | None = None
    last_synced_at: datetime | None = None

    def checkpoint(
        self,
        cache: Cache,
        interval: _Interval,
        saved: set[Event],
        fetched: set[Event],
        *,
        recent: bool = False,
    ) -> set[Event]:
        # Commit events before any interval can attest to their availability.
        if fetched or not Path(cache.activity.path).is_file():
            saved = merge_activity_cache(fetched, cache=cache)
        if recent:
            self.recent_cutoff = interval[1]
            self.last_synced_at = _now_utc()
        self.intervals = _join_intervals([*self.intervals, interval])
        cache.activity_coverage.save(
            {
                "event_count": len(saved),
                "intervals": self.intervals,
                "recent_cutoff": self.recent_cutoff,
                "last_synced_at": self.last_synced_at,
            }
        )
        return saved


def _join_intervals(intervals: list[_Interval]) -> list[_Interval]:
    joined: list[_Interval] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if joined and start <= joined[-1][1]:
            earlier_start, earlier_end = joined[-1]
            joined[-1] = (earlier_start, max(earlier_end, end))
        else:
            joined.append((start, end))
    return joined


def _cached_activity(cache: Cache) -> tuple[set[Event], _ActivityCoverage]:
    saved = cache.activity_coverage.load()
    has_activity_file = Path(cache.activity.path).is_file()
    events = load_activity_cache(cache=cache)
    event_count = saved.get("event_count")
    if (
        not has_activity_file
        or not isinstance(event_count, int)
        or len(events) < event_count
    ):
        return events, _ActivityCoverage()

    intervals: list[_Interval] = []
    raw_intervals = saved.get("intervals")
    if isinstance(raw_intervals, list):
        for interval in raw_intervals:
            if (
                isinstance(interval, tuple)
                and len(interval) == 2
                and isinstance(interval[0], datetime)
                and isinstance(interval[1], datetime)
            ):
                intervals.append(
                    (
                        activity_datetime_utc(interval[0]),
                        activity_datetime_utc(interval[1]),
                    )
                )
    cutoff = saved.get("recent_cutoff")
    last_synced_at = saved.get("last_synced_at")
    return events, _ActivityCoverage(
        intervals=_join_intervals(intervals),
        recent_cutoff=activity_datetime_utc(cutoff)
        if isinstance(cutoff, datetime)
        else None,
        last_synced_at=activity_datetime_utc(last_synced_at)
        if isinstance(last_synced_at, datetime)
        else None,
    )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sync_recent_activity(
    db: DatabaseActivity, *, cache: Cache | None = None
) -> SyncResult:
    """Persist a complete scan through the previous boundary plus a day of overlap."""
    cache = cache or Cache()
    with activity_sync_lock(cache=cache):
        previous, coverage = _cached_activity(cache)
        end = _now_utc().replace(microsecond=0)
        anchor = coverage.recent_cutoff
        if anchor is None:
            dates = (activity_datetime_utc(event.date) for event in previous)
            anchor = max((when for when in dates if when < end), default=None)
        start = None
        if anchor is not None:
            start = datetime.combine(
                min(anchor, end).date() - _DAY, time.min, timezone.utc
            )
        fetched = set(
            db.fetch_activity_incremental(previous, date_from=start, date_to=end)
        )
        if start is None:
            # The open-ended scan confirms the available feed. Avoid claiming dates
            # before the oldest returned event and give empty accounts a recent window.
            start = min(
                (activity_datetime_utc(event.date) for event in fetched),
                default=end.replace(hour=0, minute=0, second=0) - _DAY,
            )
        merged = coverage.checkpoint(
            cache, (start, end), previous, fetched, recent=True
        )
    return SyncResult(merged, fetched - previous)


def backfill_activity_history(
    db: DatabaseActivity,
    *,
    cache: Cache | None = None,
    nweeks_window_size: int = 10,
    early_stop_after_n_windows: int = 2,
) -> SyncResult:
    """Checkpoint history windows so an interrupted scan retains fetched events."""
    cache = cache or Cache()
    with activity_sync_lock(cache=cache):
        previous, coverage = _cached_activity(cache)
        if not needs_activity_history(previous):
            return SyncResult(previous, set())
        merged = previous
        new_events: set[Event] = set()

        def checkpoint(start: datetime, end: datetime, events: set[Event]) -> None:
            nonlocal merged
            merged = coverage.checkpoint(cache, (start, end), merged, events)
            new_events.update(events)

        db.fetch_activity_history(
            nweeks_window_size=nweeks_window_size,
            early_stop_after_n_windows=early_stop_after_n_windows,
            events_already_fetched=previous,
            date_to=activity_history_boundary(previous),
            progress_desc="Fetching activity history",
            on_window=checkpoint,
        )
    return SyncResult(merged, new_events)


def _period_status(
    day: date,
    duration: timedelta,
    zone: ZoneInfo | timezone,
    intervals: list[_Interval],
) -> _CoverageLabel:
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + duration, time.min, zone).astimezone(timezone.utc)
    for range_start, range_end in intervals:
        if range_start <= start and end <= range_end:
            return "covered"
    if any(
        range_start < end and start < range_end for range_start, range_end in intervals
    ):
        return "partial"
    return "unverified"


def _periods(start: date, end: date, duration: timedelta) -> Iterator[date]:
    for offset in range(0, (end - start).days + 1, duration.days):
        yield start + timedelta(days=offset)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _calendar_rollups(
    event_counts: Counter[date],
    *,
    first_day: date | None,
    today: date,
    zone: ZoneInfo | timezone,
    intervals: list[_Interval],
) -> tuple[dict[str, _PeriodStatus], dict[str, _PeriodStatus]]:
    year_counts: Counter[int] = Counter()
    month_counts: Counter[tuple[int, int]] = Counter()
    for day, count in event_counts.items():
        if day <= today:
            year_counts[day.year] += count
            month_counts[day.year, day.month] += count

    years: dict[str, _PeriodStatus] = {}
    months: dict[str, _PeriodStatus] = {}
    for year in range(min(first_day or today, today).year, today.year + 1):
        year_start = date(year, 1, 1)
        year_duration = date(year, 12, 31) - year_start + _DAY
        years[f"{year:04d}"] = {
            "coverage": _period_status(year_start, year_duration, zone, intervals),
            "eventCount": year_counts[year],
        }
        last_month = today.month if year == today.year else 12
        for month in range(1, last_month + 1):
            month_start = date(year, month, 1)
            month_duration = _DAY * monthrange(year, month)[1]
            months[month_start.isoformat()[:7]] = {
                "coverage": _period_status(
                    month_start, month_duration, zone, intervals
                ),
                "eventCount": month_counts[year, month],
            }
    return years, months


def activity_history_status(
    month: date | None = None,
    *,
    timezone_name: str = "UTC",
    demo_mode: bool = False,
    cache: Cache | None = None,
) -> ActivityHistoryStatus:
    """Report saved events and verified coverage in the dashboard's calendar timezone."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        zone = timezone.utc
        timezone_name = "UTC"
    today = _now_utc().astimezone(zone).date()
    events, coverage = (
        (set(), _ActivityCoverage())
        if demo_mode
        else _cached_activity(cache or Cache())
    )
    event_counts = Counter(
        activity_datetime_utc(event.date).astimezone(zone).date() for event in events
    )
    first_event = min(event_counts, default=None)
    latest_event = max(event_counts, default=None)
    first_verified = (
        coverage.intervals[0][0].astimezone(zone).date() if coverage.intervals else None
    )
    first_day = min(
        (day for day in (first_event, first_verified) if day is not None), default=None
    )
    years, months = _calendar_rollups(
        event_counts,
        first_day=first_day,
        today=today,
        zone=zone,
        intervals=coverage.intervals,
    )

    month_start = (month or today).replace(day=1)
    month_end = month_start.replace(
        day=monthrange(month_start.year, month_start.month)[1]
    )
    calendar_start = _monday(month_start)
    calendar_end = min(_monday(month_end) + 6 * _DAY, today)
    days: dict[str, _PeriodStatus] = {
        day.isoformat(): {
            "coverage": _period_status(day, _DAY, zone, coverage.intervals),
            "eventCount": event_counts[day],
        }
        for day in _periods(calendar_start, calendar_end, _DAY)
    }
    weeks: dict[str, _CoverageLabel] = {
        start.isoformat(): _period_status(start, _WEEK, zone, coverage.intervals)
        for start in _periods(calendar_start, calendar_end, _WEEK)
    }
    totals: _CoverageTotals = {
        "days": {"covered": 0, "partial": 0, "unverified": 0},
        "weeks": {"covered": 0, "partial": 0, "unverified": 0},
    }
    if first_day is not None and first_day <= today:
        for day in _periods(first_day, today, _DAY):
            totals["days"][_period_status(day, _DAY, zone, coverage.intervals)] += 1
        for start in _periods(_monday(first_day), today, _WEEK):
            totals["weeks"][_period_status(start, _WEEK, zone, coverage.intervals)] += 1

    return {
        "timeZone": timezone_name,
        "today": today.isoformat(),
        "firstEventDate": first_event.isoformat() if first_event else None,
        "latestEventDate": latest_event.isoformat() if latest_event else None,
        "coverageStartDate": first_verified.isoformat() if first_verified else None,
        "lastSyncedAt": coverage.last_synced_at.isoformat()
        if coverage.last_synced_at
        else None,
        "years": years,
        "months": months,
        "days": days,
        "weeks": weeks,
        "summary": totals,
    }
