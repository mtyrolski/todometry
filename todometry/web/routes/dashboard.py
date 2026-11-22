"""Dashboard FastAPI routes."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response
from loguru import logger
from omegaconf import DictConfig, OmegaConf
from pandas import DataFrame, DatetimeIndex

from todometry.core.utils import Cache, LocalStorageError
from todometry.dashboard._plot_task_counts import plot_task_count_trend
from todometry.dashboard._plot_activity import (
    plot_events_over_time,
    plot_heatmap_of_events_by_day_and_hour,
)
from todometry.dashboard._plot_common import apply_date_axis_viewport
from todometry.dashboard._plot_lifespans import plot_task_lifespans
from todometry.dashboard._plot_periodic import (
    _periodic_timezone,
    cumsum_completed_tasks_periodically,
    plot_completed_tasks_periodically,
)
from todometry.dashboard._plot_project_hierarchy import (
    plot_active_project_hierarchy_sunburst as plot_active_project_hierarchy,
)
from todometry.dashboard._plot_weekly_trend import plot_weekly_completion_trend
from todometry.dashboard._plot_project_lifecycle import build_project_lifecycle_data
from todometry.database.dataframe import ProjectHierarchy
from todometry.features.activity import activity_datetime_utc, load_activity_cache
from todometry.features.habit_tracker import extract_tracked_habit_tasks
from todometry.features.stats import p1_tasks, p2_tasks, p3_tasks, p4_tasks
from todometry.web.api_components import dashboard_runtime
from todometry.web.api_components.configuration import read_yaml_config
from todometry.web.api_components.runtime import (
    REPO_ROOT,
    resolve_calendar_timezone,
    sync_source,
)
from todometry.web.api_components.settings import dashboard_settings_payload
from todometry.web.dashboard_payload import (
    add_plot_event_markers as _add_plot_event_markers,
    build_habit_tracker_payload as _build_habit_tracker_payload,
    completed_share_leaderboard as _completed_share_leaderboard,
    compute_custom_plot_range as _compute_custom_plot_range,
    compute_insights as _compute_insights,
    compute_plot_history_beg as _compute_plot_history_beg,
    compute_plot_range as _compute_plot_range,
    evaluate_urgency_status as _evaluate_urgency_status,
    extract_metrics_dict as _extract_metrics_dict,
    fig_to_dict as _fig_to_dict,
    last_completed_week_bounds as _last_completed_week_bounds,
    normalize_activity_df as _normalize_activity_df,
    period_bounds as _period_bounds,
    safe_activity_anchor as _safe_activity_anchor,
)

Granularity = Literal["W", "ME", "3ME"]


router = APIRouter()


def _utc_bounds(
    beg: datetime, end: datetime, calendar: ZoneInfo | None
) -> tuple[datetime, datetime]:
    if calendar is None:
        return beg, end
    return (
        beg.replace(tzinfo=calendar).astimezone(timezone.utc).replace(tzinfo=None),
        end.replace(tzinfo=calendar).astimezone(timezone.utc).replace(tzinfo=None),
    )


def _calendar_frame(activity: DataFrame, calendar: ZoneInfo | None) -> DataFrame:
    if calendar is None:
        return activity
    index = DatetimeIndex(activity.index, tz=timezone.utc).tz_convert(calendar)
    return activity.set_axis(index.tz_localize(None))


def _end_of_today(calendar: ZoneInfo) -> datetime:
    return datetime.combine(
        datetime.now(calendar).date() + timedelta(days=1), datetime.min.time()
    )


def _hierarchy_figure(
    activity: DataFrame,
    snapshot: dashboard_runtime.DashboardSnapshot,
    beg: datetime,
    end: datetime,
    hierarchy: ProjectHierarchy,
) -> dict[str, Any]:
    return _fig_to_dict(
        plot_active_project_hierarchy(
            activity,
            beg,
            end,
            [*snapshot.active_projects, *snapshot.archived_projects],
            snapshot.project_colors,
            hierarchy=hierarchy,
        )
    )


def _range_figures(
    activity: DataFrame,
    calendar_activity: DataFrame,
    snapshot: dashboard_runtime.DashboardSnapshot,
    hierarchy: ProjectHierarchy,
    beg: datetime,
    end: datetime,
    *,
    calendar: ZoneInfo | None,
    rolling_beg: datetime | None = None,
    viewport_end: datetime | None = None,
    granularity: Granularity = "W",
    include_other: bool = False,
) -> dict[str, Any]:
    activity_beg, activity_end = _utc_bounds(beg, end, calendar)
    events = apply_date_axis_viewport(
        plot_events_over_time(
            calendar_activity,
            rolling_beg or beg,
            end,
            granularity,
            daily=rolling_beg is None,
            include_other=include_other,
        ),
        beg=beg,
        end=viewport_end or end,
        rangeslider=rolling_beg is not None,
    )
    return {
        "eventsOverTime": _fig_to_dict(events),
        "heatmapEventsByDayHour": _fig_to_dict(
            plot_heatmap_of_events_by_day_and_hour(calendar_activity, beg, end)
        ),
        "taskLifespansPeriod": _fig_to_dict(
            plot_task_lifespans(activity, activity_beg, activity_end)
        ),
        "activeProjectHierarchy": _hierarchy_figure(
            activity, snapshot, activity_beg, activity_end, hierarchy
        ),
    }


@router.get("/api/dashboard/hierarchy-playback", tags=["dashboard"])
async def hierarchy_playback(
    request: Request,
    weeks: int = 12,
    beg: str | None = None,
    end: str | None = None,
    cached_only: bool = False,
) -> dict[str, Any]:
    """Build cumulative hierarchy snapshots on demand for the selected period."""
    state: dashboard_runtime.DashboardState = request.app.state.dashboard
    source = sync_source()
    await dashboard_runtime.ensure_state(
        state, refresh=False, source=source, cached_only=cached_only
    )
    snapshot = state.snapshot_for(source)
    if snapshot is None:
        return {"error": "Dashboard data unavailable.", "frames": []}

    activity = _normalize_activity_df(snapshot.df_activity)
    range_beg, range_end = _compute_plot_range(activity, weeks=weeks, beg=beg, end=end)
    calendar = resolve_calendar_timezone() if cached_only and beg is not None else None
    if calendar is not None:
        range_end = min(range_end, _end_of_today(calendar))
    if range_end <= range_beg:
        return {"frames": []}
    hierarchy = snapshot.hierarchy()
    frame_count = min(16, max(1, (range_end.date() - range_beg.date()).days))
    span = range_end - range_beg
    frames = []
    for index in range(1, frame_count + 1):
        frame_end = range_beg + span * index / frame_count
        activity_beg, activity_end = _utc_bounds(range_beg, frame_end, calendar)
        figure = _hierarchy_figure(
            activity,
            snapshot,
            activity_beg,
            activity_end,
            hierarchy,
        )
        frame_date = (frame_end - timedelta(microseconds=1)).date()
        frames.append({"date": frame_date.isoformat(), "figure": figure})
    return {"frames": frames}


@router.get("/api/dashboard/project-timeline", tags=["dashboard"])
async def project_timeline(
    request: Request,
    weeks: int = 0,
    beg: str | None = None,
    end: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Return structured project lifecycle data for the dedicated timeline view."""

    state: dashboard_runtime.DashboardState = request.app.state.dashboard
    source = sync_source()
    await dashboard_runtime.ensure_state(state, refresh=refresh, source=source)
    snapshot = state.snapshot_for(source)
    if snapshot is None:
        return {"error": "Dashboard data unavailable.", "range": None, "parents": []}
    activity = _normalize_activity_df(snapshot.df_activity)
    if weeks == 0 and beg is None and end is None:
        anchor = _safe_activity_anchor(activity)
        range_end = datetime.combine(
            anchor.date() + timedelta(days=1), datetime.min.time()
        )
        first_event = (
            cast(datetime, activity.index.min()) if not activity.empty else anchor
        )
        range_beg = datetime.combine(first_event.date(), datetime.min.time())
    else:
        range_beg, range_end = _compute_plot_range(
            activity, weeks=weeks, beg=beg, end=end
        )
    projects = [*snapshot.active_projects, *snapshot.archived_projects]
    payload = build_project_lifecycle_data(
        activity,
        range_beg,
        range_end,
        projects,
        hierarchy=snapshot.hierarchy(),
    )
    payload["refreshedAt"] = datetime.now().isoformat(timespec="seconds")
    return dict(payload)


@router.get("/api/dashboard/status", tags=["dashboard"])
async def dashboard_status(request: Request) -> dict[str, Any]:
    """Read local status for UI badges without contacting Todoist."""
    return dashboard_runtime.dashboard_status(request.app.state.dashboard)


@router.get("/api/dashboard/progress", tags=["dashboard"])
async def dashboard_progress(request: Request) -> dict[str, Any]:
    """Return current data refresh progress for the dashboard."""

    state: dashboard_runtime.DashboardState = request.app.state.dashboard
    return state.sync.progress_snapshot(sync_source())


def _recorded_activity(
    snapshot: dashboard_runtime.DashboardSnapshot,
    cache_dir: str,
    calendar: ZoneInfo,
    beg: datetime,
    end: datetime,
) -> DataFrame:
    activity_beg, activity_end = _utc_bounds(beg, end, calendar)
    if not snapshot.demo_mode:
        try:
            cache = Cache(cache_dir)
            events = load_activity_cache(cache=cache)
            if events or Path(cache.activity.path).is_file():
                lower = activity_beg.replace(tzinfo=timezone.utc)
                upper = activity_end.replace(tzinfo=timezone.utc)
                dates: list[datetime] = []
                types: list[str] = []
                for event in events:
                    date = activity_datetime_utc(event.date)
                    if lower <= date < upper:
                        dates.append(date.astimezone(calendar).replace(tzinfo=None))
                        types.append(event.event_type)
                return DataFrame({"type": types}, index=DatetimeIndex(dates))
        except (LocalStorageError, OSError) as exc:
            logger.warning("Could not read saved events for history: {}", exc)

    # Demo uses its anonymized projection. An older dashboard may have only a
    # saved task projection if the original activity file is unavailable.
    activity = snapshot.df_activity
    selected = activity.loc[
        (activity.index >= activity_beg) & (activity.index < activity_end)
    ]
    return _calendar_frame(selected, calendar)


def _history_figures(
    snapshot: dashboard_runtime.DashboardSnapshot,
    cache_dir: str,
    calendar: ZoneInfo,
    beg: datetime,
    end: datetime,
) -> tuple[int, dict[str, Any]]:
    observed_end = min(end, _end_of_today(calendar))
    if beg >= observed_end:
        return 0, {}
    recorded = _recorded_activity(snapshot, cache_dir, calendar, beg, observed_end)
    if recorded.empty:
        return 0, {}
    return len(recorded), _range_figures(
        snapshot.df_activity,
        recorded,
        snapshot,
        snapshot.hierarchy(),
        beg,
        observed_end,
        calendar=calendar,
        viewport_end=end,
        include_other=True,
    )


@router.get("/api/dashboard/history", tags=["dashboard"])
async def dashboard_history(
    request: Request, response: Response, beg: str, end: str
) -> dict[str, Any]:
    """Read saved history through the inclusive dates in the dashboard timezone."""
    range_beg, range_end = _compute_custom_plot_range(beg, end)
    state: dashboard_runtime.DashboardState = request.app.state.dashboard
    source = sync_source()
    calendar = resolve_calendar_timezone()
    await dashboard_runtime.ensure_state(
        state, refresh=False, source=source, cached_only=True
    )
    snapshot = state.snapshot_for(source)
    if snapshot is None:
        return {"error": "Saved history is unavailable."}

    count, figures = await asyncio.to_thread(
        _history_figures, snapshot, source.cache_dir, calendar, range_beg, range_end
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "range": {
            "beg": range_beg.date().isoformat(),
            "end": (range_end - timedelta(days=1)).date().isoformat(),
            "mode": "custom",
        },
        "timeZone": calendar.key,
        "eventCount": count,
        "rangeHasActivity": count > 0,
        "figures": figures,
        "refreshedAt": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/dashboard/home", tags=["dashboard"])
async def dashboard_home(
    request: Request,
    granularity: Granularity = "W",
    weeks: int = 12,
    beg: str | None = None,
    end: str | None = None,
    refresh: bool = False,
    cached_only: bool = False,
) -> dict[str, Any]:
    """
    Home dashboard payload: metrics, badges, and Plotly figures.

    Notes:
    - `weeks` controls the date range used for time-series plots (default ~12 weeks).
    - `beg`/`end` (YYYY-MM-DD) override `weeks` when provided.
    - `granularity` controls periodic aggregation where applicable.
    - `refresh=true` forces a Todoist API pull + activity reload (otherwise cached state is reused).
    - `cached_only=true` serves a saved dashboard without contacting Todoist.
    """
    state: dashboard_runtime.DashboardState = request.app.state.dashboard
    source = sync_source()
    await dashboard_runtime.ensure_state(
        state,
        refresh=refresh and not cached_only,
        source=source,
        cached_only=cached_only,
    )
    snapshot = state.snapshot_for(source)
    if snapshot is None:
        return {
            "error": "Dashboard data unavailable. Please ensure the database is configured and accessible."
        }

    active_projects = snapshot.active_projects
    archived_projects = snapshot.archived_projects
    hierarchy = snapshot.hierarchy()
    config_path: Path = request.app.state.dashboard_config_path
    dashboard_settings_cfg = read_yaml_config(config_path, required=False)
    dashboard_settings = dashboard_settings_payload(
        dashboard_settings_cfg, dashboard_config_path=config_path, repo_root=REPO_ROOT
    )

    beg_range, end_range = _compute_plot_range(
        snapshot.df_activity, weeks=weeks, beg=beg, end=end
    )
    calendar = resolve_calendar_timezone() if cached_only and beg is not None else None
    periodic_calendar = _periodic_timezone()
    local_end = (
        end_range.replace(tzinfo=timezone.utc)
        .astimezone(periodic_calendar)
        .replace(tzinfo=None)
        if beg is None
        else end_range
    )
    # Open forecasts can change with the clock even when the saved events match.
    memoize = local_end <= datetime.now(periodic_calendar).replace(tzinfo=None)
    today = datetime.now().date()
    cache = Cache(source.cache_dir)
    urgency_config = dashboard_settings_cfg.get("urgency")
    urgency_settings = (
        cast(dict[str, Any], OmegaConf.to_container(urgency_config, resolve=True))
        if isinstance(urgency_config, DictConfig)
        else None
    )
    cache_key = (
        granularity,
        weeks,
        beg,
        end,
        beg_range,
        end_range,
        calendar,
        periodic_calendar,
        today,
        hierarchy if snapshot.project_hierarchy is None else None,
        dashboard_settings,
        urgency_settings,
        dashboard_runtime._activity_cache_signature(cache),
    )
    sync = state.sync.status_for(source)
    cached = snapshot.home_payload
    if memoize and cached is not None and cached[0] == cache_key and not refresh:
        return {**cached[1], "sync": sync}

    df_activity = _normalize_activity_df(snapshot.df_activity)
    no_data = df_activity.empty
    calendar_activity = _calendar_frame(df_activity, calendar)
    history_beg_range = _compute_plot_history_beg(calendar_activity, end=end_range)
    periods = _period_bounds(df_activity, granularity)
    metrics = _extract_metrics_dict(df_activity, periods)
    urgency_status = _evaluate_urgency_status(
        active_projects, today=today, settings=urgency_settings
    )
    plot_events = dashboard_settings["plotEvents"]
    archived_plot_projects = {
        hierarchy.groups[str(project.id)].name
        for project in archived_projects
        if hierarchy.groups[str(project.id)].root_id == str(project.id)
    }
    always_visible_projects = {
        hierarchy.groups[project_id].name
        for project_id in hierarchy.explicit_roots | hierarchy.promoted_roots
    }
    project_colors = snapshot.project_colors.copy()
    for project in (*active_projects, *archived_projects):
        project_id = str(project.id)
        color = snapshot.project_colors.get(project.project_entry.name)
        if color:
            project_colors.setdefault(hierarchy.labels[project_id], color)
            alias = hierarchy.groups[project_id].alias
            if alias is not None:
                project_colors.setdefault(alias, color)

    p1 = sum(map(p1_tasks, active_projects))
    p2 = sum(map(p2_tasks, active_projects))
    p3 = sum(map(p3_tasks, active_projects))
    p4 = sum(map(p4_tasks, active_projects))

    anchor_dt = _safe_activity_anchor(df_activity)
    last_week_beg, last_week_end, last_week_label = _last_completed_week_bounds(
        anchor_dt
    )
    tracked_habit_tasks = extract_tracked_habit_tasks(active_projects)
    habit_tracker = _build_habit_tracker_payload(
        df_activity,
        tracked_habit_tasks,
        anchor=anchor_dt,
        project_colors=project_colors,
    )

    if no_data:
        figures = {}
        parent_completed_share = {"items": [], "totalCompleted": 0, "figure": {}}
        root_completed_share = {"items": [], "totalCompleted": 0, "figure": {}}
    else:
        weekly_completion_fig = plot_weekly_completion_trend(df_activity, end_range)
        completed_periodic_fig = _add_plot_event_markers(
            plot_completed_tasks_periodically(
                df_activity,
                history_beg_range,
                end_range,
                granularity,
                project_colors,
                visibility_beg_date=beg_range,
                visibility_end_date=end_range,
                always_visible_projects=always_visible_projects,
                bounds_are_utc=beg is None,
                archived_project_names=archived_plot_projects,
            ),
            plot_events,
            beg=history_beg_range,
            end=end_range,
        )
        cumsum_completed_fig = _add_plot_event_markers(
            cumsum_completed_tasks_periodically(
                df_activity,
                history_beg_range,
                end_range,
                granularity,
                project_colors,
                visibility_beg_date=beg_range,
                visibility_end_date=end_range,
                always_visible_projects=always_visible_projects,
                bounds_are_utc=beg is None,
                archived_project_names=archived_plot_projects,
            ),
            plot_events,
            beg=history_beg_range,
            end=end_range,
        )
        task_count_trend_fig = apply_date_axis_viewport(
            plot_task_count_trend(
                active_projects,
                load_activity_cache(cache=cache),
                today=today,
            ),
            beg=beg_range,
            end=end_range,
        )
        figures = {
            "weeklyCompletionTrend": _fig_to_dict(weekly_completion_fig),
            "taskCountTrend": _fig_to_dict(task_count_trend_fig),
            "taskLifespans": _fig_to_dict(plot_task_lifespans(df_activity)),
            "completedTasksPeriodically": _fig_to_dict(completed_periodic_fig),
            "cumsumCompletedTasksPeriodically": _fig_to_dict(cumsum_completed_fig),
            **_range_figures(
                df_activity,
                calendar_activity,
                snapshot,
                hierarchy,
                beg_range,
                end_range,
                calendar=calendar,
                rolling_beg=history_beg_range if beg is None else None,
                granularity=granularity,
            ),
        }
        parent_completed_share = _completed_share_leaderboard(
            df_activity,
            beg=last_week_beg,
            end=last_week_end,
            column="parent_project_name",
            project_colors=project_colors,
        )
        root_completed_share = _completed_share_leaderboard(
            df_activity,
            beg=last_week_beg,
            end=last_week_end,
            column="root_project_name",
            project_colors=project_colors,
        )

    payload = {
        "noData": no_data,
        "range": {
            "beg": beg if beg is not None else beg_range.strftime("%Y-%m-%d"),
            "end": end if end is not None else end_range.strftime("%Y-%m-%d"),
            "mode": "custom" if beg is not None and end is not None else "rolling",
            "granularity": granularity,
            "weeks": weeks,
        },
        "metrics": {
            "items": metrics,
            "currentPeriod": periods["currentLabel"],
            "previousPeriod": periods["previousLabel"],
        },
        "urgencyStatus": urgency_status,
        "configurableItems": [
            {
                "key": "urgency",
                "label": "Urgency watch badge",
                "icon": "wrench",
                "configPath": dashboard_settings["configPath"],
                "anchor": "dashboard-settings",
                "summary": (
                    f"Priority thresholds {dashboard_settings['warnPriorityThresholds']}; "
                    f"due within {dashboard_settings['warnDueWithinDays']} days; "
                    f"deadline within {dashboard_settings['warnDeadlineWithinDays']} days."
                ),
            },
            {
                "key": "plot-events",
                "label": "Plot event markers",
                "icon": "wrench",
                "configPath": dashboard_settings["configPath"],
                "anchor": "dashboard-settings",
                "summary": f"{len(plot_events)} annotated event markers configured.",
            },
        ],
        "badges": {"p1": p1, "p2": p2, "p3": p3, "p4": p4},
        "habitTracker": habit_tracker,
        "insights": {
            "label": last_week_label,
            "items": []
            if no_data
            else _compute_insights(
                df_activity,
                beg=last_week_beg,
                end=last_week_end,
                project_colors=project_colors,
            ),
        },
        "leaderboards": {
            "lastCompletedWeek": {
                "label": last_week_label,
                "beg": last_week_beg.strftime("%Y-%m-%d"),
                "end": (last_week_end - timedelta(days=1)).strftime("%Y-%m-%d"),
                "parentProjects": parent_completed_share,
                "rootProjects": root_completed_share,
            }
        },
        "figures": figures,
        "refreshedAt": datetime.now().isoformat(timespec="seconds"),
    }
    if memoize:
        snapshot.home_payload = (cache_key, payload)
    return {**payload, "sync": sync}
