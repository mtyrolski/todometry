from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from todometry.core.types import Event
from todometry.core.utils import Cache, exclusive_file_lock

EventCollection = set[Event]
MIN_HISTORY_SPAN = timedelta(weeks=12)


def load_activity_cache(*, cache: Cache | None = None) -> EventCollection:
    """Load only valid event records, repairing semantically bad cache entries."""

    storage = (cache or Cache()).activity
    raw = storage.load()
    valid_events = load_valid_events(raw)
    if len(valid_events) != len(raw):
        logger.warning(
            "Discarded {} invalid activity cache record(s) during recovery.",
            len(raw) - len(valid_events),
        )
        valid_events = storage.update(load_valid_events)
    return valid_events


def merge_activity_cache(
    events: EventCollection, *, cache: Cache | None = None
) -> EventCollection:
    """Merge events under the cache file lock so concurrent workers cannot lose data."""

    incoming = load_valid_events(events)

    def _merge(existing: EventCollection) -> EventCollection:
        return {
            *load_valid_events(existing),
            *incoming,
        }

    return (cache or Cache()).activity.update(_merge)


def activity_sync_lock(*, cache: Cache | None = None):
    """Serialize activity API syncs across threads and dashboard processes."""

    activity_path = Path((cache or Cache()).activity.path)
    lock_path = activity_path.with_name(f"{activity_path.stem}.sync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return exclusive_file_lock(lock_path)


def load_valid_events(events: object) -> EventCollection:
    if not isinstance(events, Iterable):
        return set()
    return {
        event
        for event in events
        if isinstance(event, Event)
        and isinstance(event.date, datetime)
        and getattr(event, "event_entry", None) is not None
    }


def activity_history_boundary(events: EventCollection) -> datetime | None:
    return min((activity_datetime_utc(event.date) for event in events), default=None)


def activity_datetime_utc(value: datetime) -> datetime:
    """Interpret naïve cache timestamps as UTC, as used by legacy activity entries."""
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def activity_project_ids(events: Iterable[Event]) -> set[str]:
    """Return project IDs directly or indirectly referenced by activity events."""

    project_ids: set[str] = set()

    def _collect(value: object, *, key: str = "") -> None:
        normalized_key = key.lower()
        if isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                _collect(nested_value, key=str(nested_key))
            return
        if isinstance(value, (list, tuple, set)):
            for nested_value in value:
                _collect(nested_value, key=key)
            return
        if (
            value is not None
            and "project" in normalized_key
            and normalized_key.endswith("id")
        ):
            project_ids.add(str(value))

    for event in events:
        entry = event.event_entry
        for project_id in (entry.parent_project_id, entry.v2_parent_project_id):
            if project_id:
                project_ids.add(str(project_id))
        if entry.object_type == "project" and entry.object_id:
            project_ids.add(str(entry.object_id))
        _collect(entry.extra_data)
    return project_ids


def needs_activity_history(events: EventCollection) -> bool:
    boundary = activity_history_boundary(events)
    return (
        boundary is None or datetime.now(boundary.tzinfo) - boundary < MIN_HISTORY_SPAN
    )
