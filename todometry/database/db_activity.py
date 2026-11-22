from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime, timedelta, timezone

from loguru import logger

from todometry.api import RequestSpec, TodoistEndpoints
from todometry.api.client import TodoistConnection
from todometry.core.types import Event, EventEntry
from todometry.core.utils import (
    get_max_concurrent_requests,
    report_tqdm_progress,
    safe_instantiate_entry,
)

ACTIVITY_PAGE_LIMIT = 100


def _activity_query_date(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _format_progress_date_range(date_from: datetime, date_to: datetime) -> str:
    return (
        f"{date_from.astimezone(timezone.utc).date().isoformat()} "
        f"to {date_to.astimezone(timezone.utc).date().isoformat()} UTC"
    )


def _events_not_in(events: list[Event], seen: set[Event] | None) -> list[Event]:
    return [event for event in events if event not in seen] if seen else events


class DatabaseActivity(TodoistConnection):
    """Database class to fetch activity data from the Todoist API."""

    def fetch_activity_adaptively(
        self,
        nweeks_window_size: int = 10,
        early_stop_after_n_windows: int = 5,
        events_already_fetched: set[Event] | None = None,
        progress_desc: str = "Querying activity data",
        date_to: datetime | None = None,
        max_workers: int | None = None,
        on_window: Callable[[datetime, datetime, set[Event]], None] | None = None,
    ) -> list[Event]:
        """
        Fetch activity events from Todoist API in a moving-window pattern.

        Each window scans all cursor pages for a date range. The loop moves
        backward until enough consecutive windows contain no unseen events.
        """
        events_already_fetched = set(events_already_fetched or ())

        n_empty_weeks: int = 0
        iterated_weeks: int = 0
        if nweeks_window_size <= 0:
            raise ValueError("nweeks_window_size must be positive")
        if early_stop_after_n_windows <= 0:
            raise ValueError("early_stop_after_n_windows must be positive")
        if date_to is not None:
            if date_to.tzinfo is None:
                date_to = date_to.replace(tzinfo=timezone.utc)
            now_utc = date_to.astimezone(timezone.utc)
        else:
            now_utc = datetime.now(timezone.utc)
        worker_count = max_workers or get_max_concurrent_requests()
        # Do not speculate farther than the configured empty-window stop
        # horizon. This keeps recovery bounded while still removing the old
        # single-worker bottleneck.
        worker_count = max(1, min(worker_count, early_stop_after_n_windows))
        total_events: list[Event] = []
        logger.debug(
            "Start fetch_activity_adaptively: window_size={}, early_stop={}, "
            "workers={}, date_to={}",
            nweeks_window_size,
            early_stop_after_n_windows,
            worker_count,
            now_utc.isoformat(),
        )
        window_number = 0
        report_tqdm_progress(
            progress_desc,
            0,
            max(early_stop_after_n_windows, 1),
            "window",
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            while n_empty_weeks < early_stop_after_n_windows:
                batch: list[tuple[int, datetime, datetime, Future[list[Event]]]] = []
                for _ in range(worker_count):
                    if n_empty_weeks >= early_stop_after_n_windows:
                        break
                    window_number += 1
                    window_end = now_utc - timedelta(weeks=iterated_weeks)
                    window_start = window_end - timedelta(weeks=nweeks_window_size)
                    window_range = _format_progress_date_range(window_start, window_end)
                    report_tqdm_progress(
                        progress_desc,
                        window_number - 1,
                        max(window_number - 1 + early_stop_after_n_windows, 1),
                        "window",
                        detail=(
                            f"{progress_desc}: window {window_number} scanning {window_range}; "
                            f"workers={worker_count}; "
                            f"empty windows={n_empty_weeks}/{early_stop_after_n_windows}; "
                            f"cached events={len(events_already_fetched)}"
                        ),
                    )
                    future = executor.submit(
                        copy_context().run,
                        self._fetch_activity_range,
                        date_from=window_start,
                        date_to=window_end,
                        events_already_fetched=set(events_already_fetched),
                        progress_desc=progress_desc,
                    )
                    batch.append((window_number, window_start, window_end, future))
                    iterated_weeks += nweeks_window_size

                for current_window, window_start, window_end, future in batch:
                    window_range = _format_progress_date_range(window_start, window_end)
                    if n_empty_weeks >= early_stop_after_n_windows:
                        logger.debug(
                            "Discarding speculative activity window {} after early stop.",
                            current_window,
                        )
                        continue
                    window_events = future.result()
                    new_events = _events_not_in(window_events, events_already_fetched)
                    n_empty_weeks = n_empty_weeks + 1 if not new_events else 0
                    total_events.extend(new_events)
                    events_already_fetched.update(new_events)
                    if on_window is not None:
                        on_window(window_start, window_end, set(new_events))
                    remaining_empty_windows = early_stop_after_n_windows - n_empty_weeks
                    report_tqdm_progress(
                        progress_desc,
                        current_window,
                        max(current_window + remaining_empty_windows, current_window),
                        "window",
                        detail=(
                            f"{progress_desc}: completed window {current_window} "
                            f"({window_range}); workers={worker_count}; "
                            f"new events={len(new_events)}; "
                            f"empty windows={n_empty_weeks}/{early_stop_after_n_windows}; "
                            f"total cached events={len(events_already_fetched)}"
                        ),
                    )
        logger.debug(
            f"Stopping fetch after {iterated_weeks} weeks processed, total_events={len(total_events)}"
        )

        # Extend with already fetched events to avoid losing them.
        total_events.extend(events_already_fetched)
        total_events = list(set(total_events))  # deduplication
        logger.debug(
            f"Total events after merging with already fetched and deduplication: {len(total_events)}"
        )

        # Final sorting from newest to oldest.
        total_events.sort(key=lambda x: x.event_entry.event_date, reverse=True)
        return total_events

    def fetch_activity_incremental(
        self,
        events_already_fetched: set[Event],
        *,
        date_from: datetime | None,
        date_to: datetime,
        progress_desc: str = "Fetching recent activity",
    ) -> list[Event]:
        """Read all cursor pages through the last verified activity boundary."""
        return self._fetch_activity_range(
            date_from=date_from,
            date_to=date_to,
            events_already_fetched=events_already_fetched,
            progress_desc=progress_desc,
        )

    def fetch_activity_history(
        self,
        *,
        nweeks_window_size: int = 10,
        early_stop_after_n_windows: int = 5,
        events_already_fetched: set[Event] | None = None,
        date_to: datetime | None = None,
        progress_desc: str = "Fetching activity history",
        max_workers: int | None = None,
        on_window: Callable[[datetime, datetime, set[Event]], None] | None = None,
    ) -> list[Event]:
        """Backfill older activity windows, optionally ending at the cache boundary."""

        logger.info(
            "Activity sync mode=history; window={}w; stop={}; boundary={}; workers={}",
            nweeks_window_size,
            early_stop_after_n_windows,
            date_to.isoformat() if date_to else "now",
            max_workers or get_max_concurrent_requests(),
        )
        return self.fetch_activity_adaptively(
            nweeks_window_size=nweeks_window_size,
            early_stop_after_n_windows=early_stop_after_n_windows,
            events_already_fetched=events_already_fetched,
            date_to=date_to,
            progress_desc=progress_desc,
            max_workers=max_workers,
            on_window=on_window,
        )

    def _fetch_activity_range(
        self,
        *,
        date_from: datetime | None,
        date_to: datetime,
        events_already_fetched: set[Event] | None = None,
        progress_desc: str = "Querying activity data",
        parent_project_id: str | None = None,
    ) -> list[Event]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        events: list[Event] = []
        start_label = (
            date_from.isoformat() if date_from is not None else "earliest available"
        )
        logger.info(
            "Starting activity fetch over range [{} .. {})",
            start_label,
            date_to.isoformat(),
        )
        fetched_pages = 0

        while True:
            params: dict[str, str | int] = {
                "limit": ACTIVITY_PAGE_LIMIT,
                "date_to": _activity_query_date(date_to),
            }
            if date_from is not None:
                params["date_from"] = _activity_query_date(date_from)
            if parent_project_id:
                params["parent_project_id"] = parent_project_id
            if cursor:
                params["cursor"] = cursor

            next_page_number = fetched_pages + 1
            estimated_total_pages = next_page_number + 1
            report_tqdm_progress(
                progress_desc,
                next_page_number,
                estimated_total_pages,
                "page",
            )
            spec = RequestSpec(
                endpoint=TodoistEndpoints.LIST_ACTIVITY,
                params=params,
                rate_limited=True,
            )
            project_suffix = (
                f" parent_project_id={parent_project_id}" if parent_project_id else ""
            )
            page_entries, next_cursor = self._fetch_activity_entries_page(
                spec,
                operation_name=(
                    f"fetch activity range {start_label} "
                    f"{date_to.isoformat()}{project_suffix}"
                ),
            )
            page_events = self._events_from_entries(page_entries)
            fetched_pages += 1
            report_tqdm_progress(
                progress_desc,
                fetched_pages,
                estimated_total_pages,
                "page",
            )
            page_new_events = _events_not_in(page_events, events_already_fetched)
            if events_already_fetched:
                skipped_events = len(page_events) - len(page_new_events)
                if skipped_events:
                    logger.debug(
                        "Skipped {} already cached event(s) while scanning range page {}.",
                        skipped_events,
                        fetched_pages,
                    )
            # Keep scanning older cursor pages even when the newest page is
            # already cached. Otherwise gaps in deeper pages can never be healed.
            events.extend(page_new_events)

            if next_cursor is None:
                report_tqdm_progress(
                    progress_desc,
                    fetched_pages,
                    max(fetched_pages, 1),
                    "page",
                )
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("Todoist repeated an activity pagination cursor.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        logger.info(
            f"Finished activity range fetch. Total events collected: {len(events)}"
        )
        return events

    def fetch_activity_for_parent_projects(
        self,
        parent_project_ids: Iterable[str],
        *,
        date_from: datetime,
        date_to: datetime,
        window_weeks: int = 52,
        events_already_fetched: set[Event] | None = None,
        early_stop_after_n_windows: int | None = 2,
        progress_desc: str = "Fetching archived project activity",
        progress_lane_labels: Mapping[str, str] | None = None,
    ) -> list[Event]:
        """Fetch project scopes concurrently while scanning each scope sequentially."""

        project_ids = sorted(
            {project_id for project_id in parent_project_ids if project_id}
        )
        if not project_ids:
            return []
        if date_from >= date_to:
            return []
        if window_weeks <= 0:
            raise ValueError("window_weeks must be positive")
        if early_stop_after_n_windows is not None and early_stop_after_n_windows <= 0:
            raise ValueError("early_stop_after_n_windows must be positive")

        initial_seen_events = set(events_already_fetched or set())
        cached_project_ids = {
            project_id
            for event in initial_seen_events
            for project_id in (
                event.event_entry.parent_project_id,
                event.event_entry.v2_parent_project_id,
            )
            if project_id
        }
        window_delta = timedelta(weeks=window_weeks)
        estimated_windows_per_project = max(
            1,
            int(
                (
                    (date_to - date_from).total_seconds()
                    + window_delta.total_seconds()
                    - 1
                )
                // window_delta.total_seconds()
            ),
        )
        max_workers = min(get_max_concurrent_requests(), len(project_ids))
        lane_labels = progress_lane_labels or {}

        def _scan_project(project_index: int, parent_project_id: str) -> list[Event]:
            lane_id = f"archived-project:{parent_project_id}"
            lane_label = lane_labels.get(
                parent_project_id,
                f"Project {project_index}/{len(project_ids)}",
            )
            seen_events = set(initial_seen_events)
            project_events: list[Event] = []
            window_end = date_to
            project_windows_scanned = 0
            consecutive_empty_windows = 0
            can_stop_early = (
                early_stop_after_n_windows is not None
                and parent_project_id in cached_project_ids
            )
            report_tqdm_progress(
                progress_desc,
                0,
                estimated_windows_per_project,
                "window",
                detail=f"{lane_label}: waiting to scan activity windows",
                lane_id=lane_id,
                lane_label=lane_label,
                lane_status="active",
            )
            while window_end > date_from:
                window_start = max(date_from, window_end - window_delta)
                project_windows_scanned += 1
                window_range = _format_progress_date_range(window_start, window_end)
                report_tqdm_progress(
                    progress_desc,
                    project_windows_scanned - 1,
                    estimated_windows_per_project,
                    "window",
                    detail=(
                        f"{lane_label}: scanning {window_range}; "
                        f"cached events={len(seen_events)}"
                    ),
                    lane_id=lane_id,
                    lane_label=lane_label,
                    lane_status="active",
                )
                window_events = self._fetch_activity_range(
                    date_from=window_start,
                    date_to=window_end,
                    events_already_fetched=seen_events,
                    progress_desc=progress_desc,
                    parent_project_id=parent_project_id,
                )
                new_events = _events_not_in(window_events, seen_events)
                project_events.extend(new_events)
                seen_events.update(new_events)
                consecutive_empty_windows = (
                    0 if new_events else consecutive_empty_windows + 1
                )
                report_tqdm_progress(
                    progress_desc,
                    project_windows_scanned,
                    estimated_windows_per_project,
                    "window",
                    detail=(
                        f"{lane_label}: completed {window_range}; "
                        f"new events={len(new_events)}"
                    ),
                    lane_id=lane_id,
                    lane_label=lane_label,
                    lane_status=(
                        "done"
                        if project_windows_scanned >= estimated_windows_per_project
                        else "active"
                    ),
                )
                window_end = window_start
                if (
                    can_stop_early
                    and early_stop_after_n_windows is not None
                    and consecutive_empty_windows >= early_stop_after_n_windows
                ):
                    skipped_windows = max(
                        estimated_windows_per_project - project_windows_scanned, 0
                    )
                    report_tqdm_progress(
                        progress_desc,
                        estimated_windows_per_project,
                        estimated_windows_per_project,
                        "window",
                        detail=(
                            f"{lane_label}: stopped after "
                            f"{consecutive_empty_windows} newest windows "
                            f"with no new events; skipped {skipped_windows} older windows"
                        ),
                        lane_id=lane_id,
                        lane_label=lane_label,
                        lane_status="done",
                    )
                    break
            return project_events

        fetched_events: list[Event] = []
        report_tqdm_progress(
            progress_desc,
            0,
            len(project_ids),
            "project",
            detail=(
                f"{progress_desc}: scanning {len(project_ids)} project(s); "
                f"workers={max_workers}"
            ),
        )
        for project_index, parent_project_id in enumerate(project_ids, start=1):
            lane_label = lane_labels.get(
                parent_project_id,
                f"Project {project_index}/{len(project_ids)}",
            )
            report_tqdm_progress(
                progress_desc,
                0,
                estimated_windows_per_project,
                "window",
                detail=f"{lane_label}: queued",
                lane_id=f"archived-project:{parent_project_id}",
                lane_label=lane_label,
                lane_status="queued",
            )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    copy_context().run, _scan_project, project_index, parent_project_id
                )
                for project_index, parent_project_id in enumerate(project_ids, start=1)
            ]
            for completed_projects, future in enumerate(as_completed(futures), start=1):
                fetched_events.extend(future.result())
                report_tqdm_progress(
                    progress_desc,
                    completed_projects,
                    len(project_ids),
                    "project",
                    detail=(
                        f"{progress_desc}: completed {completed_projects}/"
                        f"{len(project_ids)} project(s); workers={max_workers}"
                    ),
                )

        fetched_events = list(set(fetched_events))
        fetched_events.sort(
            key=lambda event: event.event_entry.event_date, reverse=True
        )
        logger.info(
            "Finished scoped parent-project activity fetch. Projects={}, new_events={}",
            len(project_ids),
            len(fetched_events),
        )
        return fetched_events

    @staticmethod
    def _events_from_entries(entries: list[EventEntry]) -> list[Event]:
        events: list[Event] = []
        for entry in entries:
            try:
                parsed = datetime.fromisoformat(entry.event_date)
                event_date = (
                    (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Todoist returned an invalid activity timestamp."
                ) from exc
            if not isinstance(entry.id, (int, str)) or entry.id == "":
                raise RuntimeError("Todoist returned activity without an event ID.")
            entry.id = str(entry.id)
            events.append(Event(event_entry=entry, id=entry.id, date=event_date))
        return events

    def _fetch_activity_entries_page(
        self,
        spec: RequestSpec,
        *,
        operation_name: str,
    ) -> tuple[list[EventEntry], str | None]:
        decoded_result = self._api_client.request_json(
            spec, operation_name=operation_name
        )
        if not isinstance(decoded_result, dict):
            raise RuntimeError("Unexpected response payload when fetching activity range")

        raw_events = decoded_result.get("results")
        if not isinstance(raw_events, list):
            raise RuntimeError("Unexpected results payload when fetching activity range")
        if not all(isinstance(event, dict) for event in raw_events):
            raise RuntimeError(
                "Unexpected non-object event record in activity range payload"
            )

        events = [safe_instantiate_entry(EventEntry, **event) for event in raw_events]
        next_cursor = decoded_result.get("next_cursor")
        if "next_cursor" not in decoded_result or (
            next_cursor is not None
            and (not isinstance(next_cursor, str) or not next_cursor)
        ):
            raise RuntimeError(
                "Todoist returned an invalid activity pagination cursor."
            )
        return events, next_cursor
