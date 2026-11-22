"""Dashboard state, progress, and service-status runtime helpers."""

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from loguru import logger
from pandas import DataFrame

from todometry.core.types import Event, Project
from todometry.core.utils import (
    Cache,
    IncompatibleCacheError,
    LocalStorageError,
)
from todometry.database.base import Database
from todometry.database.dataframe import (
    ProjectHierarchy,
    apply_project_groups,
    load_activity_data,
    load_cached_activity_data,
    load_project_adjustments,
    resolve_personal_dir,
    resolve_project_hierarchy,
)
from todometry.database.db_projects import preserve_archived_parent_links
from todometry.features.activity import (
    activity_datetime_utc,
    activity_history_boundary,
    activity_project_ids,
    load_activity_cache,
    merge_activity_cache,
    needs_activity_history,
)
from todometry.features.data_sync import SyncResult, backfill_activity_history
from todometry.web.api_components import runtime
from todometry.web.dashboard_payload import normalize_activity_df


@dataclass(frozen=True, slots=True)
class _ArchivedActivityTarget:
    project_id: str
    label: str
    updated_at: str


@dataclass(slots=True)
class DashboardSnapshot:
    """One published dashboard projection and its latest Overview response."""

    df_activity: DataFrame
    active_projects: list[Project] = field(default_factory=list)
    archived_projects: list[Project] = field(default_factory=list)
    project_colors: dict[str, str] = field(default_factory=dict)
    db: Database | None = None
    last_refresh_s: float = 0.0
    demo_mode: bool = False
    source_key: runtime.SyncSourceKey | None = None
    activity_cache_signature: dict[str, int] | None = None
    adjustments_cache_signature: list[dict[str, int | str]] = field(
        default_factory=list
    )
    home_payload: tuple[tuple[object, ...], dict[str, Any]] | None = None
    project_hierarchy: ProjectHierarchy | None = None

    def hierarchy(self) -> ProjectHierarchy:
        return self.project_hierarchy or _dashboard_project_hierarchy(
            self.active_projects, self.archived_projects
        )


@dataclass(slots=True)
class DashboardState:
    """The dashboard projection and shared synchronization job for one app."""

    snapshot: DashboardSnapshot | None = None
    sync: runtime.ActivitySyncJob = field(default_factory=runtime.ActivitySyncJob)
    backfill_source: runtime.SyncSourceKey | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def snapshot_for(self, source: runtime.SyncSource) -> DashboardSnapshot | None:
        snapshot = self.snapshot
        if (
            snapshot is not None
            and snapshot.demo_mode == source.demo_mode
            and (snapshot.source_key is None or snapshot.source_key == source.identity)
        ):
            return snapshot
        return None


_STATE_TTL_S = 60.0
_DASHBOARD_STATE_SCHEMA_VERSION = 5
_DEMO_DASHBOARD_STATE_SCHEMA_VERSION = 2
_ARCHIVED_ACTIVITY_SCAN_STATE_VERSION = 1
_ARCHIVED_ACTIVITY_SENTINEL_LIMIT = 16


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _activity_cache_signature(cache: Cache) -> dict[str, int] | None:
    try:
        stat = Path(cache.activity.path).stat()
    except OSError:
        return None
    return {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)}


def _adjustments_cache_signature() -> list[dict[str, int | str]]:
    personal_dir = resolve_personal_dir()
    if not personal_dir.exists():
        return []

    signatures: list[dict[str, int | str]] = []
    for path in sorted(personal_dir.iterdir()):
        if not path.is_file() or path.suffix != ".py":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        signatures.append(
            {
                "name": path.name,
                "mtime_ns": int(stat.st_mtime_ns),
                "size": int(stat.st_size),
            }
        )
    return signatures


def _dashboard_project_hierarchy(
    active: list[Project],
    archived: list[Project],
    *,
    original_roots: dict[str, Project] | None = None,
) -> ProjectHierarchy:
    return resolve_project_hierarchy(
        {str(project.id): project for project in (*archived, *active)},
        load_project_adjustments(),
        original_roots=original_roots,
    )


def _project_parent_id(project: Project) -> str | None:
    return project.project_entry.parent_id or project.project_entry.v2_parent_id


def _project_has_candidate_ancestor(
    project: Project,
    *,
    projects_by_id: dict[str, Project],
    candidate_names: set[str],
) -> bool:
    visited: set[str] = set()
    parent_id = _project_parent_id(project)
    while parent_id and parent_id not in visited:
        visited.add(parent_id)
        parent = projects_by_id.get(parent_id)
        if parent is None:
            return False
        if parent.project_entry.name in candidate_names:
            return True
        parent_id = _project_parent_id(parent)
    return False


def _configured_archived_parent_projects(
    dbio: Database,
) -> dict[str, _ArchivedActivityTarget]:
    link_mapping, archived_parents, _ = load_project_adjustments()
    archived_parent_names = set(archived_parents)
    if not archived_parent_names:
        return {}

    candidate_names = set(archived_parent_names)
    candidate_names.update(
        source_name
        for source_name, target_name in link_mapping.items()
        if source_name in archived_parent_names or target_name in archived_parent_names
    )

    projects = dbio.fetch_projects(include_tasks=False) + dbio.fetch_archived_projects()
    projects_by_id = {project.id: project for project in projects}
    target_projects: dict[str, _ArchivedActivityTarget] = {}
    for project in projects:
        if not project.is_archived:
            continue
        if (
            project.project_entry.name in candidate_names
            or _project_has_candidate_ancestor(
                project,
                projects_by_id=projects_by_id,
                candidate_names=candidate_names,
            )
        ):
            project_id = str(project.id)
            target_projects[project_id] = _ArchivedActivityTarget(
                project_id=project_id,
                label=str(project.project_entry.name),
                updated_at=str(project.project_entry.updated_at or ""),
            )
    return target_projects


def _load_archived_activity_scan_fingerprints(
    cached_events: set[Event],
    *,
    cache: Cache,
) -> dict[str, str]:
    try:
        payload = cache.archived_activity_scans.load()
    except IncompatibleCacheError:
        raise
    except LocalStorageError:
        return {}
    if (
        not isinstance(payload, Mapping)
        or payload.get("version") != _ARCHIVED_ACTIVITY_SCAN_STATE_VERSION
    ):
        return {}

    sentinels = payload.get("activitySentinels")
    if not isinstance(sentinels, list) or not all(
        isinstance(event_id, str) for event_id in sentinels
    ):
        return {}
    cached_event_ids = {str(event.id) for event in cached_events}
    if not set(sentinels).issubset(cached_event_ids):
        logger.info(
            "Archived activity scan state invalidated because cached activity "
            "sentinels are missing."
        )
        return {}

    projects = payload.get("projects")
    if not isinstance(projects, Mapping):
        return {}
    return {
        str(project_id): str(entry.get("projectUpdatedAt") or "")
        for project_id, entry in projects.items()
        if isinstance(entry, Mapping)
    }


def _save_archived_activity_scan_fingerprints(
    fingerprints: Mapping[str, str],
    scanned_targets: Mapping[str, _ArchivedActivityTarget],
    cached_events: set[Event],
    *,
    cache: Cache,
) -> None:
    completed_at = _now_iso()
    merged_fingerprints = dict(fingerprints)
    merged_fingerprints.update(
        {
            project_id: target.updated_at
            for project_id, target in scanned_targets.items()
        }
    )
    event_ids = sorted({str(event.id) for event in cached_events})
    cache.archived_activity_scans.update(
        lambda _existing: {
            "version": _ARCHIVED_ACTIVITY_SCAN_STATE_VERSION,
            "activitySentinels": event_ids[:_ARCHIVED_ACTIVITY_SENTINEL_LIMIT],
            "projects": {
                project_id: {
                    "projectUpdatedAt": project_updated_at,
                    "completedAt": completed_at,
                }
                for project_id, project_updated_at in merged_fingerprints.items()
            },
        }
    )


def _sync_configured_archived_parent_activity(
    state: DashboardState,
    dbio: Database,
    cached_events: set[Event],
    *,
    cache: Cache,
) -> None:
    targets = _configured_archived_parent_projects(dbio)
    if not targets:
        return

    fingerprints = _load_archived_activity_scan_fingerprints(cached_events, cache=cache)
    pending_targets = {
        project_id: target
        for project_id, target in targets.items()
        if not target.updated_at or fingerprints.get(project_id) != target.updated_at
    }
    skipped_projects = len(targets) - len(pending_targets)
    if not pending_targets:
        logger.info(
            "Skipping archived activity fetch for {} unchanged project(s).",
            skipped_projects,
        )
        state.sync.set_progress(
            "Fetching archived project activity",
            step=1,
            detail=(
                f"Archived activity cache is current; skipped "
                f"{skipped_projects} unchanged project(s)"
            ),
            sub_current=skipped_projects,
            sub_total=skipped_projects,
        )
        return

    now_utc = datetime.now(timezone.utc)
    date_from = now_utc - timedelta(weeks=520)
    date_to = now_utc
    logger.info(
        "Fetching scoped activity for {} changed archived project(s); skipped {} unchanged.",
        len(pending_targets),
        skipped_projects,
    )
    fetched_events = dbio.fetch_activity_for_parent_projects(
        pending_targets,
        date_from=date_from,
        date_to=date_to,
        window_weeks=52,
        events_already_fetched=set(cached_events),
        progress_desc="Fetching archived project activity",
        progress_lane_labels={
            project_id: target.label for project_id, target in pending_targets.items()
        },
    )
    recent_events = {
        event
        for event in fetched_events
        if activity_datetime_utc(event.date) >= date_from
    }
    if recent_events:
        cached_events = merge_activity_cache(recent_events, cache=cache)
        logger.info(
            "Merged {} scoped archived-parent event(s); total={}",
            len(recent_events),
            len(cached_events),
        )
    try:
        _save_archived_activity_scan_fingerprints(
            fingerprints,
            pending_targets,
            cached_events,
            cache=cache,
        )
    except LocalStorageError as exc:
        logger.warning(f"Failed to save archived activity scan state: {exc}")


def _persist_state_to_disk_cache(snapshot: DashboardSnapshot, *, cache: Cache) -> None:
    payload: dict[str, Any] = {
        "version": _DASHBOARD_STATE_SCHEMA_VERSION,
        "created_at": _now_iso(),
        "last_refresh_s": float(snapshot.last_refresh_s),
        "demo_mode": snapshot.demo_mode,
        "demo_state_version": (
            _DEMO_DASHBOARD_STATE_SCHEMA_VERSION if snapshot.demo_mode else None
        ),
        "activity_cache_signature": snapshot.activity_cache_signature,
        "adjustments_cache_signature": snapshot.adjustments_cache_signature,
        "df_activity": snapshot.df_activity,
        "active_projects": snapshot.active_projects,
        "archived_projects": snapshot.archived_projects,
        "project_colors": snapshot.project_colors,
    }
    try:
        cache.dashboard_state.update(lambda _existing: payload)
    except (LocalStorageError, OSError, TypeError) as exc:
        logger.warning(f"Failed to persist dashboard state cache: {exc}")


def _read_dashboard_cache(
    cache: Cache, *, demo_mode: bool, allow_stale_activity: bool = False
) -> dict[str, Any] | None:
    try:
        payload = cache.dashboard_state.load()
    except IncompatibleCacheError:
        raise
    except LocalStorageError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _DASHBOARD_STATE_SCHEMA_VERSION
        or bool(payload.get("demo_mode", False)) != demo_mode
        or (
            demo_mode
            and payload.get("demo_state_version")
            != _DEMO_DASHBOARD_STATE_SCHEMA_VERSION
        )
    ):
        return None
    if not allow_stale_activity and (
        payload.get("activity_cache_signature") != _activity_cache_signature(cache)
        or payload.get("adjustments_cache_signature") != _adjustments_cache_signature()
    ):
        return None
    return payload


def _load_state_from_disk_cache(
    state: DashboardState,
    *,
    source: runtime.SyncSource,
    cache: Cache,
    allow_stale_activity: bool = False,
) -> bool:
    payload = _read_dashboard_cache(
        cache, demo_mode=source.demo_mode, allow_stale_activity=allow_stale_activity
    )
    if payload is None:
        return False

    df_activity = payload.get("df_activity")
    active_projects = payload.get("active_projects")
    archived_projects = payload.get("archived_projects")
    project_colors = payload.get("project_colors")
    if not (
        isinstance(df_activity, DataFrame)
        and isinstance(active_projects, list)
        and isinstance(archived_projects, list)
        and isinstance(project_colors, dict)
    ):
        return False

    state.snapshot = DashboardSnapshot(
        df_activity=normalize_activity_df(df_activity),
        active_projects=active_projects,
        archived_projects=archived_projects,
        project_colors={str(k): str(v) for k, v in project_colors.items()},
        last_refresh_s=float(payload.get("last_refresh_s") or time.time()),
        demo_mode=source.demo_mode,
        source_key=source.identity,
        activity_cache_signature=payload.get("activity_cache_signature"),
        adjustments_cache_signature=payload.get("adjustments_cache_signature", []),
    )
    logger.info(
        "Loaded dashboard state cache from disk "
        f"(events={len(df_activity)}, active_projects={len(active_projects)}, "
        f"archived_projects={len(archived_projects)})"
    )
    return True


def _load_saved_activity_state(
    state: DashboardState, *, source: runtime.SyncSource
) -> bool:
    """Use a saved dashboard or rebuild it from the activity cache without Todoist."""
    cache = Cache(source.cache_dir)
    if state.snapshot_for(source) is None:
        try:
            _load_state_from_disk_cache(
                state,
                source=source,
                cache=cache,
                allow_stale_activity=not source.demo_mode,
            )
        except IncompatibleCacheError:
            if source.demo_mode:
                raise

    snapshot = state.snapshot_for(source)
    if source.demo_mode:
        return snapshot is not None
    signature = _activity_cache_signature(cache)
    adjustments_signature = _adjustments_cache_signature()
    if snapshot is None and signature is None:
        return False
    if (
        snapshot is not None
        and snapshot.activity_cache_signature == signature
        and snapshot.adjustments_cache_signature == adjustments_signature
        and snapshot.project_hierarchy is not None
    ):
        return True

    active = snapshot.active_projects if snapshot is not None else []
    archived = snapshot.archived_projects if snapshot is not None else []
    try:
        hierarchy = (
            snapshot.hierarchy()
            if snapshot is not None
            and snapshot.adjustments_cache_signature == adjustments_signature
            else _dashboard_project_hierarchy(active, archived)
        )
        if signature is not None and (
            snapshot is None
            or snapshot.activity_cache_signature != signature
            or snapshot.adjustments_cache_signature != adjustments_signature
        ):
            activity = normalize_activity_df(
                load_cached_activity_data(
                    active, archived, cache=cache, hierarchy=hierarchy
                )
            )
        else:
            assert snapshot is not None
            activity = apply_project_groups(snapshot.df_activity, hierarchy)
    except Exception as exc:
        if snapshot is None:
            raise
        logger.warning(
            "Could not reload saved activity; retaining the saved dashboard: {}", exc
        )
        return True

    if snapshot is not None and activity.empty and not snapshot.df_activity.empty:
        activity = apply_project_groups(snapshot.df_activity, hierarchy)
        signature = snapshot.activity_cache_signature
    if snapshot is None:
        state.snapshot = DashboardSnapshot(
            df_activity=activity,
            source_key=source.identity,
            activity_cache_signature=signature,
            adjustments_cache_signature=adjustments_signature,
            project_hierarchy=hierarchy,
        )
    else:
        state.snapshot = replace(
            snapshot,
            df_activity=activity,
            activity_cache_signature=signature,
            adjustments_cache_signature=adjustments_signature,
            home_payload=None,
            project_hierarchy=hierarchy,
        )
    return True


async def _reload_saved_activity(
    state: DashboardState, *, source: runtime.SyncSource
) -> bool:
    """Keep the caller's state lock held until the worker finishes publishing."""
    worker = asyncio.create_task(
        asyncio.to_thread(_load_saved_activity_state, state, source=source)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await asyncio.gather(worker, return_exceptions=True)
        raise


def _project_refresh_baseline(
    state: DashboardState, *, source: runtime.SyncSource, cache: Cache
) -> tuple[list[Project] | None, list[Project], float]:
    """Reuse project tasks even when a new activity event invalidates plot data."""
    snapshot = state.snapshot_for(source)
    if snapshot is not None and snapshot.last_refresh_s > 0:
        return (
            snapshot.active_projects,
            snapshot.archived_projects,
            snapshot.last_refresh_s,
        )

    payload = _read_dashboard_cache(
        cache, demo_mode=source.demo_mode, allow_stale_activity=True
    )
    if payload is None:
        return None, [], 0.0
    projects = payload.get("active_projects")
    refreshed_at = payload.get("last_refresh_s")
    if not isinstance(projects, list) or not all(
        isinstance(project, Project) for project in projects
    ):
        return None, [], 0.0
    if not isinstance(refreshed_at, (int, float)) or refreshed_at <= 0:
        return None, [], 0.0
    archived = payload.get("archived_projects")
    previous_archived = (
        archived
        if isinstance(archived, list)
        and all(isinstance(project, Project) for project in archived)
        else []
    )
    return projects, previous_archived, float(refreshed_at)


def _project_ids_changed_since(events: set[Event], refreshed_at: float) -> set[str]:
    """Include cached events synced by another process since the project snapshot."""
    if refreshed_at <= 0:
        return set()
    cutoff = datetime.fromtimestamp(refreshed_at, timezone.utc) - timedelta(days=1)

    def _after_cutoff(event: Event) -> bool:
        event_date = event.date
        if event_date.tzinfo is None:
            event_date = event_date.replace(tzinfo=timezone.utc)
        return event_date.astimezone(timezone.utc) >= cutoff

    return activity_project_ids(event for event in events if _after_cutoff(event))


def _reopened_project_ids(events: set[Event], refreshed_at: float) -> set[str]:
    if refreshed_at <= 0:
        return set()
    ids: set[str] = set()
    for event in events:
        entry = event.event_entry
        when = (
            event.date if event.date.tzinfo else event.date.replace(tzinfo=timezone.utc)
        )
        if (
            entry.object_type == "project"
            and entry.event_type == "unarchived"
            and when.timestamp() > refreshed_at
        ):
            ids.update(
                str(value) for value in (entry.object_id, entry.v2_object_id) if value
            )
    return ids


def _refresh_state_sync(
    state: DashboardState, *, source: runtime.SyncSource, result: SyncResult | None
) -> str | None:
    """Complete the wider dashboard refresh after the shared recent scan."""
    cache = Cache(source.cache_dir)
    demo_mode = source.demo_mode
    cached_events = (
        result.events if result is not None else load_activity_cache(cache=cache)
    )
    recent_events = result.new_events if result is not None else set()
    had_activity_boundary = len(cached_events) > len(recent_events)
    dbio = Database(str(source.env_path), api_key=source.api_key)
    previous_projects, previous_archived, previous_refresh_s = (
        _project_refresh_baseline(state, source=source, cache=cache)
    )
    previous_hierarchy = [*(previous_projects or []), *previous_archived]

    warning = None
    if result is not None and state.backfill_source != source.identity:
        try:
            if needs_activity_history(cached_events):
                boundary = activity_history_boundary(cached_events)
                state.sync.set_progress(
                    "Fetching activity history",
                    step=1,
                    detail=(
                        "Backfilling older activity before "
                        f"{boundary.isoformat() if boundary else 'now'}"
                    ),
                )
                cached_events = backfill_activity_history(dbio, cache=cache).events
        except IncompatibleCacheError:
            raise
        except Exception as exc:
            logger.warning("Could not finish loading older activity: {}", exc)
            warning = "Could not finish loading older activity."
            cached_events = load_activity_cache(cache=cache)
        else:
            state.backfill_source = source.identity

    changed_project_ids = activity_project_ids(recent_events)
    if previous_projects is not None:
        changed_project_ids.update(
            _project_ids_changed_since(cached_events, previous_refresh_s)
        )
    state.sync.set_progress(
        "Reconciling project data",
        step=1,
        detail=(
            f"Refreshing {len(changed_project_ids)} changed project(s) and "
            "checking the account project list"
        ),
    )
    if result is None or not had_activity_boundary or demo_mode:
        previous_projects = None
    active_projects = dbio.fetch_projects_incrementally(
        previous_projects, changed_project_ids
    )
    archived_projects = dbio.fetch_archived_projects()

    preserve_archived_parent_links(
        active_projects,
        archived_projects,
        previous_hierarchy,
        _reopened_project_ids(cached_events, previous_refresh_s),
    )

    if source.api_key:
        try:
            _sync_configured_archived_parent_activity(
                state, dbio, cached_events, cache=cache
            )
        except IncompatibleCacheError:
            raise
        except Exception as exc:
            logger.warning("Could not load archived project activity: {}", exc)
            warning = warning or "Could not load archived project activity."

    state.sync.set_progress(
        "Resolving project hierarchy",
        step=2,
        detail="Resolving roots across active and archived projects",
    )
    activity_signature = _activity_cache_signature(cache)
    adjustments_signature = _adjustments_cache_signature()
    hierarchy = _dashboard_project_hierarchy(
        active_projects,
        archived_projects,
        original_roots=dbio.fetch_mapping_project_id_to_root(),
    )
    df_activity = normalize_activity_df(
        load_activity_data(dbio, cache=cache, hierarchy=hierarchy)
    )

    state.sync.set_progress(
        "Preparing dashboard data",
        step=3,
        detail="Loading metadata and caches",
    )

    if demo_mode:
        from todometry.database.demo import (
            anonymize_activity_dates,
            anonymize_label_names,
            anonymize_project_names,
        )

        if not dbio.is_anonymized:
            dbio.anonymize(
                project_mapping=anonymize_project_names(df_activity, active_projects),
                label_mapping=anonymize_label_names(active_projects),
            )
        df_activity = anonymize_activity_dates(df_activity)
        hierarchy = _dashboard_project_hierarchy(active_projects, archived_projects)

    project_colors = dbio.fetch_mapping_project_name_to_color()

    snapshot = DashboardSnapshot(
        df_activity=df_activity,
        active_projects=active_projects,
        archived_projects=archived_projects,
        project_colors=project_colors,
        db=dbio,
        last_refresh_s=time.time(),
        demo_mode=demo_mode,
        source_key=source.identity,
        activity_cache_signature=activity_signature,
        adjustments_cache_signature=adjustments_signature,
        project_hierarchy=hierarchy,
    )
    state.snapshot = snapshot
    _persist_state_to_disk_cache(snapshot, cache=cache)
    return warning


async def _refresh_dashboard(
    state: DashboardState, source: runtime.SyncSource, result: SyncResult | None
) -> str | None:
    async with state.lock:
        return await asyncio.to_thread(
            _refresh_state_sync, state, source=source, result=result
        )


def _start_dashboard_refresh(
    state: DashboardState, source: runtime.SyncSource
) -> asyncio.Task[None]:
    return state.sync.start(source, refresh=partial(_refresh_dashboard, state))


def _schedule_stale_state_refresh(
    state: DashboardState, snapshot: DashboardSnapshot, source: runtime.SyncSource
) -> None:
    now = time.time()
    if 0 <= now - state.sync.last_full_attempt_s(source) < _STATE_TTL_S:
        return
    if (
        0 <= now - snapshot.last_refresh_s < _STATE_TTL_S
        and snapshot.activity_cache_signature
        == _activity_cache_signature(Cache(source.cache_dir))
    ):
        return
    try:
        _start_dashboard_refresh(state, source)
    except runtime.SyncBusy:
        # A normal read can keep showing saved data until this source can run.
        return


async def ensure_state(
    state: DashboardState,
    refresh: bool,
    *,
    source: runtime.SyncSource | None = None,
    cached_only: bool = False,
) -> None:
    source = source or runtime.sync_source()
    if cached_only:
        task = state.sync.task
        if state.lock.locked() and task is not None and not task.done():
            ready = state.snapshot_for(source) is not None
        else:
            async with state.lock:
                try:
                    ready = await _reload_saved_activity(state, source=source)
                except Exception as exc:
                    logger.warning("Could not load saved dashboard: {}", exc)
                    raise HTTPException(
                        status_code=503, detail="Could not load saved activity charts."
                    ) from exc
        if ready:
            return
        raise HTTPException(
            status_code=409,
            detail="Saved history is unavailable. Sync Todoist from Activity History first.",
        )

    if not refresh:
        snapshot = state.snapshot_for(source)
        if (
            snapshot is None
            or snapshot.adjustments_cache_signature != _adjustments_cache_signature()
        ) and not state.lock.locked():
            async with state.lock:
                try:
                    await _reload_saved_activity(state, source=source)
                except Exception as exc:
                    logger.warning(
                        "Could not load the saved dashboard snapshot: {}", exc
                    )
            snapshot = state.snapshot_for(source)
        if snapshot is not None:
            _schedule_stale_state_refresh(state, snapshot, source)
            return

    while True:
        try:
            task = _start_dashboard_refresh(state, source)
        except runtime.SyncBusy:
            previous = state.sync.task
            if previous is not None:
                await asyncio.shield(previous)
        else:
            await asyncio.shield(task)
            break

    if state.snapshot_for(source) is not None:
        return
    async with state.lock:
        try:
            if await _reload_saved_activity(state, source=source):
                return
        except Exception as exc:
            logger.warning("Could not load saved dashboard: {}", exc)
    raise HTTPException(
        status_code=503,
        detail=(
            "Could not load the dashboard. Check your Todoist token and try again "
            "from Activity History."
        ),
    )


def dashboard_status(state: DashboardState) -> dict[str, Any]:
    """Report snapshot freshness and local service status."""
    source = runtime.sync_source()
    activity_path = Path(Cache(source.cache_dir).activity.path)
    activity_cache: dict[str, str | int | None] | None
    try:
        activity_path = activity_path.resolve(strict=True)
        stat = activity_path.stat()
    except (FileNotFoundError, NotADirectoryError):
        activity_cache = None
    except OSError:
        activity_cache = {"path": str(activity_path), "mtime": None, "size": None}
    else:
        activity_cache = {
            "path": str(activity_path),
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(
                timespec="seconds"
            ),
            "size": stat.st_size,
        }

    service_specs = (
        ("Todoist token", bool(source.api_key), "API_KEY set", "API_KEY missing"),
        ("Activity cache", activity_cache, activity_cache, "activity.joblib missing"),
    )
    snapshot = state.snapshot_for(source)
    return {
        "services": [
            {
                "name": name,
                "status": "ok" if available else "warn",
                "detail": ok_detail if available else missing_detail,
            }
            for name, available, ok_detail, missing_detail in service_specs
        ],
        "apiCache": {
            "lastRefresh": datetime.fromtimestamp(snapshot.last_refresh_s).isoformat(
                timespec="seconds"
            )
            if snapshot is not None and snapshot.last_refresh_s
            else None
        },
        "activityCache": activity_cache,
    }
