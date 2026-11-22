"""Tests for FastAPI dashboard home endpoints."""

import asyncio
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi import HTTPException

from tests.factories import make_project, make_project_entry, make_task
from tests.unit.database.helpers import make_event
from tests.web_api_helpers import _single_event_df
from todometry.api.client import TodoistAPIClient
from todometry.core.env import EnvVar
from todometry.core.utils import Cache
from todometry.database.dataframe import render_adjustments_file_content
import todometry.web.routes.dashboard as dashboard_routes
from todometry.web.api_components import dashboard_runtime, runtime
from todometry.web.api_components.dashboard_runtime import (
    DashboardSnapshot,
    DashboardState,
)
from todometry.web.api_components.runtime import REPO_ROOT
from todometry.web.routes import admin_settings

# pylint: disable=protected-access


@pytest.fixture
def saved_dashboard(monkeypatch, tmp_path, api_client):
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path / "cache"))
    monkeypatch.setenv(str(EnvVar.DATA_DIR), str(tmp_path))
    monkeypatch.setenv(str(EnvVar.PERSONAL_DIR), str(tmp_path / "personal"))
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")
    monkeypatch.setenv("API_KEY", "")

    def seed(activity, **metadata):
        snapshot = DashboardSnapshot(activity, last_refresh_s=time.time(), **metadata)
        api_client.app.state.dashboard.snapshot = snapshot
        return snapshot

    return seed


def _event_df(rows: list[dict[str, str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def _empty_event_df() -> pd.DataFrame:
    return _event_df(
        [
            {
                "date": "2025-01-15",
                "id": "anchor",
                "title": "anchor",
                "type": "added",
                "parent_project_name": "A",
                "root_project_name": "A",
                "task_id": "anchor",
            }
        ]
    ).iloc[:0]


def _home(api_client, query: str = "weeks=12&granularity=W"):
    return api_client.get(f"/api/dashboard/home?{query}")


def _history(api_client, beg: str, end: str):
    return api_client.get("/api/dashboard/history", params={"beg": beg, "end": end})


def test_project_timeline_endpoint_returns_structured_archived_history(
    api_client, saved_dashboard, tmp_path
) -> None:
    root = make_project(
        project_id="root",
        project_entry=make_project_entry(
            project_id="root", name="Product", created_at="2026-01-01T00:00:00Z"
        ),
    )
    child = make_project(
        project_id="child",
        project_entry=make_project_entry(
            project_id="child",
            name="Shipped",
            parent_id="root",
            created_at="2026-08-01T00:00:00Z",
            updated_at="2026-08-19T00:00:00Z",
        ),
        is_archived=True,
    )
    activity = _event_df(
        [
            {
                "date": "2026-08-18",
                "id": "e1",
                "title": "done",
                "type": "completed",
                "parent_project_id": "child",
                "parent_project_name": "Shipped",
                "root_project_name": "Product",
                "task_id": "1",
            }
        ]
    )
    snapshot = saved_dashboard(
        activity, active_projects=[root], archived_projects=[child]
    )

    response = api_client.get(
        "/api/dashboard/project-timeline?beg=2026-08-01&end=2026-08-31"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["range"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert payload["parents"][0]["id"] == "root"
    assert payload["parents"][0]["children"][0]["status"] == "completed"
    assert payload["parents"][0]["children"][0]["archived"] is True

    full_history = api_client.get("/api/dashboard/project-timeline").json()
    assert full_history["range"]["start"] == "2026-08-18"

    sibling = make_project(
        project_id="sibling",
        name="Upcoming",
        parent_id="root",
        created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-20T00:00:00Z",
    )
    snapshot.active_projects.append(sibling)
    archived_group = {"targetId": "child", "targetName": "Shipped"}
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "adj_private.py").write_text(
        render_adjustments_file_content(
            {}, [], {"child": archived_group, "sibling": archived_group}
        ),
        encoding="utf-8",
    )
    grouped = api_client.get(
        "/api/dashboard/project-timeline?beg=2026-08-01&end=2026-08-31"
    ).json()
    assert [parent["id"] for parent in grouped["parents"]] == ["child"]
    assert {item["id"] for item in grouped["parents"][0]["children"]} == {
        "child",
        "sibling",
    }


def test_hierarchy_playback_builds_cumulative_frames_on_demand(
    api_client, saved_dashboard
) -> None:
    activity = _event_df(
        [
            {
                "date": timestamp,
                "id": f"e{index}",
                "title": "done",
                "type": "completed",
                "parent_project_id": "root",
                "parent_project_name": "Product",
                "root_project_name": "Product",
                "task_id": str(index),
            }
            for index, timestamp in enumerate(["2026-08-02", "2026-08-14"])
        ]
    )
    saved_dashboard(activity, active_projects=[make_project(project_id="root")])
    response = api_client.get(
        "/api/dashboard/hierarchy-playback?beg=2026-08-01&end=2026-08-14"
    )

    assert response.status_code == 200
    frames = response.json()["frames"]
    assert len(frames) == 14
    assert frames[0]["date"] == "2026-08-01"
    assert frames[1]["figure"]["data"][0]["values"][0] == 1
    assert frames[-1]["figure"]["data"][0]["values"][0] == 2
    assert frames[-1]["date"] == "2026-08-14"


def test_hierarchy_playback_matches_rolling_dashboard_boundaries(
    api_client, saved_dashboard
) -> None:
    activity = _event_df(
        [
            {
                "date": timestamp,
                "id": f"event-{index}",
                "title": "done",
                "type": "completed",
                "parent_project_id": "root",
                "parent_project_name": "Product",
                "root_project_name": "Product",
                "task_id": str(index),
            }
            for index, timestamp in enumerate(
                ["2026-08-24T08:00:00", "2026-08-24T14:00:00", "2026-11-16T12:00:00"]
            )
        ]
    )
    saved_dashboard(activity, active_projects=[make_project(project_id="root")])
    current = _home(api_client, "weeks=12&granularity=W").json()
    assert current["range"]["mode"] == "rolling"
    playback = api_client.get("/api/dashboard/hierarchy-playback?weeks=12").json()
    current_figure = current["figures"]["activeProjectHierarchy"]
    final_figure = playback["frames"][-1]["figure"]
    assert current_figure["data"][0]["values"] == final_figure["data"][0]["values"]
    assert current_figure["data"][0]["values"][0] == 2

    custom = _home(api_client, "beg=2026-08-24&end=2026-11-16").json()
    assert custom["range"]["mode"] == "custom"
    custom_playback = api_client.get(
        "/api/dashboard/hierarchy-playback?beg=2026-08-24&end=2026-11-16"
    ).json()
    assert custom["figures"]["activeProjectHierarchy"]["data"][0]["values"][0] == 3
    assert (
        custom["figures"]["activeProjectHierarchy"]["data"][0]["values"]
        == (custom_playback["frames"][-1]["figure"]["data"][0]["values"])
    )


def _dashboard_cache(monkeypatch, tmp_path) -> Cache:
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cache = Cache()
    cache.activity.save(set())
    return cache


def _save_dashboard_snapshot(cache: Cache, **overrides) -> None:
    payload = {
        "version": dashboard_runtime._DASHBOARD_STATE_SCHEMA_VERSION,
        "created_at": "2025-01-01T00:00:00",
        "last_refresh_s": 123.0,
        "demo_mode": False,
        "activity_cache_signature": dashboard_runtime._activity_cache_signature(cache),
        "adjustments_cache_signature": [],
        "df_activity": _single_event_df(),
        "active_projects": [],
        "archived_projects": [],
        "project_colors": {},
    }
    payload.update(overrides)
    cache.dashboard_state.save(payload)


@pytest.mark.parametrize("source", ["memory", "disk", "missing"])
def test_history_reads_saved_dashboard_without_sync(
    monkeypatch, tmp_path, api_client, source
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    state = api_client.app.state.dashboard
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")
    monkeypatch.setenv(str(EnvVar.PERSONAL_DIR), str(tmp_path / "personal"))
    monkeypatch.setenv(str(EnvVar.TIMEZONE), "UTC")

    def unexpected_sync(*args, **kwargs):
        pytest.fail("History must not schedule or run a Todoist sync")

    monkeypatch.setattr(
        dashboard_runtime, "_schedule_stale_state_refresh", unexpected_sync
    )
    monkeypatch.setattr(dashboard_runtime, "_refresh_state_sync", unexpected_sync)
    monkeypatch.setattr(TodoistAPIClient, "request_json", unexpected_sync)
    if source == "missing":
        Path(cache.activity.path).unlink()
    else:
        cache.activity.save({make_event("e1", date=datetime(2025, 1, 15))})
        if source == "memory":
            state.snapshot = DashboardSnapshot(
                df_activity=_single_event_df(),
                activity_cache_signature=dashboard_runtime._activity_cache_signature(
                    cache
                ),
            )
        else:
            _save_dashboard_snapshot(cache)

    history = _history(api_client, "2025-01-15", "2025-01-15")
    playback = api_client.get(
        "/api/dashboard/hierarchy-playback?cached_only=true&beg=2025-01-15&end=2025-01-15"
    )
    if source == "missing":
        assert history.status_code == playback.status_code == 409
        assert "Saved history is unavailable" in history.json()["detail"]
        return

    assert history.status_code == playback.status_code == 200
    assert history.json()["rangeHasActivity"] is True
    assert history.json()["eventCount"] == 1
    assert playback.json()["frames"]
    without_activity = _history(api_client, "2025-01-14", "2025-01-14")
    assert without_activity.status_code == 200
    assert without_activity.json()["rangeHasActivity"] is False
    assert without_activity.json()["figures"] == {}


@pytest.mark.parametrize(
    ("beg", "end", "month", "start_utc", "stop_utc"),
    [
        ("2024-12-30", "2025-01-05", "2025-01", "2024-12-29T23:00", "2025-01-05T23:00"),
        ("2025-03-24", "2025-03-30", "2025-03", "2025-03-23T23:00", "2025-03-30T22:00"),
    ],
    ids=["year-boundary", "dst-23-hour-sunday"],
)
def test_history_week_builds_only_its_four_figures_and_matches_calendar(
    monkeypatch, tmp_path, api_client, beg, end, month, start_utc, stop_utc
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")
    monkeypatch.setenv(str(EnvVar.TIMEZONE), "Europe/Warsaw")
    start = datetime.fromisoformat(start_utc).replace(tzinfo=timezone.utc)
    stop = datetime.fromisoformat(stop_utc).replace(tzinfo=timezone.utc)
    completed = make_event("at-start", date=start)
    added = make_event("before-start", date=start - timedelta(minutes=1))
    added.event_entry.event_type = "added"
    added.event_entry.object_id = completed.event_entry.object_id
    archived = make_event("archived", date=start + timedelta(hours=1))
    archived.event_entry.object_type = "project"
    archived.event_entry.event_type = "archived"
    archived.event_entry.extra_data = None
    cache.activity.save(
        {
            added,
            completed,
            archived,
            make_event("before-end", date=stop - timedelta(seconds=1)),
            make_event("at-end", date=stop),
        }
    )
    _save_dashboard_snapshot(
        cache,
        activity_cache_signature=None,
        active_projects=[make_project(project_id="proj1", name="Work")],
    )

    def unexpected(*args, **kwargs):
        pytest.fail("Reading a saved week ran sync or Overview-only work")

    monkeypatch.setattr(TodoistAPIClient, "request_json", unexpected)
    monkeypatch.setattr(dashboard_runtime, "_schedule_stale_state_refresh", unexpected)
    for name in (
        "plot_weekly_completion_trend",
        "plot_task_count_trend",
        "plot_completed_tasks_periodically",
        "cumsum_completed_tasks_periodically",
        "_extract_metrics_dict",
        "_compute_insights",
        "_build_habit_tracker_payload",
        "_completed_share_leaderboard",
        "read_yaml_config",
    ):
        monkeypatch.setattr(dashboard_routes, name, unexpected)

    plots = {
        "eventsOverTime": "plot_events_over_time",
        "heatmapEventsByDayHour": "plot_heatmap_of_events_by_day_and_hour",
        "taskLifespansPeriod": "plot_task_lifespans",
        "activeProjectHierarchy": "plot_active_project_hierarchy",
    }
    built = {key: [] for key in plots}

    def record(key, plot):
        def build(*args, **kwargs):
            figure = plot(*args, **kwargs)
            built[key].append(figure)
            return figure

        return build

    for key, name in plots.items():
        monkeypatch.setattr(
            dashboard_routes, name, record(key, getattr(dashboard_routes, name))
        )

    response = _history(api_client, beg, end)
    assert response.status_code == 200
    payload = response.json()
    assert payload["range"] == {"beg": beg, "end": end, "mode": "custom"}
    assert payload["timeZone"] == "Europe/Warsaw"
    assert payload["eventCount"] == 3
    assert set(payload["figures"]) == set(plots)
    assert all(len(figures) == 1 for figures in built.values())

    days = api_client.get("/api/activity/status", params={"month": month}).json()[
        "days"
    ]
    counts = Counter()
    for trace in built["eventsOverTime"][0].data:
        for stamp, count in zip(trace.x, trace.y, strict=True):
            counts[pd.Timestamp(stamp).date().isoformat()] += int(count)
    assert counts == {
        day: value["eventCount"] for day, value in days.items() if beg <= day <= end
    }
    assert counts[beg] == 2
    assert counts[end] == 1

    heatmap = built["heatmapEventsByDayHour"][0].data[0].z
    assert sum(sum(row) for row in heatmap) == 3
    assert heatmap[0][0] == heatmap[0][1] == heatmap[6][23] == 1
    assert built["activeProjectHierarchy"][0].data[0].values[0] == 2
    assert (
        built["taskLifespansPeriod"][0].layout.title.text
        == "Task Lifespans: Time to Completion"
    )
    assert api_client.app.state.dashboard.sync.task is None


def test_history_extends_the_axis_to_the_chosen_end_without_future_observations(
    monkeypatch, tmp_path, api_client
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")
    monkeypatch.setenv(str(EnvVar.TIMEZONE), "UTC")
    monkeypatch.setattr(
        dashboard_routes, "_end_of_today", lambda _calendar: datetime(2025, 1, 16)
    )
    cache.activity.save({make_event("observed", date=datetime(2025, 1, 15, 9))})

    response = _history(api_client, "2025-01-13", "2025-01-19")
    assert response.status_code == 200
    payload = response.json()
    assert payload["eventCount"] == 1
    chart = payload["figures"]["eventsOverTime"]
    assert chart["layout"]["xaxis"]["range"] == [
        "2025-01-13T00:00:00",
        "2025-01-20T00:00:00",
    ]
    assert chart["data"]
    assert all(
        [str(stamp)[:10] for stamp in trace["x"]]
        == ["2025-01-13", "2025-01-14", "2025-01-15"]
        for trace in chart["data"]
    )


def test_history_rebuilds_stale_snapshot_offline_after_activity_sync(
    monkeypatch, tmp_path, api_client
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    monkeypatch.setenv(str(EnvVar.TIMEZONE), "Europe/Warsaw")
    cache.activity.save({make_event("legacy", date=datetime(2025, 1, 15, 12))})
    project = make_project(
        project_id="proj1",
        project_entry=make_project_entry(
            project_id="proj1", name="Saved project", parent_id="missing-parent"
        ),
    )
    _save_dashboard_snapshot(cache, active_projects=[project])
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")

    def unexpected_todoist_request(*args, **kwargs):
        pytest.fail("Rebuilding saved charts contacted Todoist")

    monkeypatch.setattr(TodoistAPIClient, "request_json", unexpected_todoist_request)

    assert _history(api_client, "2025-01-16", "2025-01-16").json()["eventCount"] == 0
    fresh = make_event("fresh", date=datetime(2025, 1, 15, 23, 30, tzinfo=timezone.utc))
    cache.activity.update(lambda events: events | {fresh})

    history = _history(api_client, "2025-01-16", "2025-01-16")
    playback = api_client.get(
        "/api/dashboard/hierarchy-playback?cached_only=true&beg=2025-01-16&end=2025-01-16"
    )

    assert history.status_code == 200
    assert history.json()["rangeHasActivity"] is True
    assert history.json()["eventCount"] == 1
    assert playback.json()["frames"][-1]["date"] == "2025-01-16"
    activity = api_client.app.state.dashboard.snapshot.df_activity
    assert activity["id"].tolist() == ["legacy", "fresh"]
    assert activity["parent_project_name"].tolist() == ["Saved project"] * 2


def test_failed_explicit_dashboard_refresh_uses_saved_snapshot(
    monkeypatch, tmp_path, api_client
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    _save_dashboard_snapshot(cache)
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")
    monkeypatch.setenv("API_KEY", "")

    def offline(state, *, source, result):
        raise ConnectionError("project metadata unavailable")

    monkeypatch.setattr(dashboard_runtime, "_refresh_state_sync", offline)

    response = _home(api_client, "refresh=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sync"]["dashboardError"] == "Could not update the dashboard."
    assert payload["figures"]


def test_load_state_from_disk_cache_restores_payload(monkeypatch, tmp_path) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    df = _single_event_df()
    _save_dashboard_snapshot(cache, df_activity=df, project_colors={"A": "#123456"})

    state = DashboardState()
    loaded = dashboard_runtime._load_state_from_disk_cache(
        state, source=runtime.sync_source(demo_mode=False), cache=cache
    )
    assert loaded is True
    assert state.snapshot.df_activity is not None
    assert len(state.snapshot.df_activity) == 1
    assert state.snapshot.project_colors == {"A": "#123456"}
    assert state.snapshot.active_projects == []
    assert state.snapshot.archived_projects == []


def test_load_state_from_disk_cache_rejects_stale_activity_signature(
    monkeypatch, tmp_path
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    _save_dashboard_snapshot(cache)

    # Mutate activity cache so signature no longer matches cached dashboard snapshot.
    cache.activity.save({"new-event"})

    state = DashboardState()
    loaded = dashboard_runtime._load_state_from_disk_cache(
        state, source=runtime.sync_source(demo_mode=False), cache=cache
    )
    assert loaded is False


def test_project_refresh_reuses_snapshot_when_activity_signature_changes(
    monkeypatch, tmp_path
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    project = make_project(project_id="cached")
    old_child = make_project(
        project_id="archived", parent_id="cached", is_archived=True
    )
    refreshed_at = datetime.now(timezone.utc).timestamp() - 3600
    _save_dashboard_snapshot(
        cache,
        active_projects=[project],
        archived_projects=[old_child],
        last_refresh_s=refreshed_at,
    )
    cache.activity.save({make_event("new-event")})
    state = DashboardState()

    source = runtime.sync_source(demo_mode=False)
    assert not dashboard_runtime._load_state_from_disk_cache(
        state, source=source, cache=cache
    )
    projects, archived, snapshot_time = dashboard_runtime._project_refresh_baseline(
        state, source=source, cache=cache
    )

    assert projects is not None
    assert [item.id for item in projects] == ["cached"]
    assert [item.id for item in archived] == ["archived"]
    assert archived[0].project_entry.parent_id == "cached"
    assert snapshot_time == refreshed_at


def test_reopened_projects_use_only_events_after_the_saved_snapshot() -> None:
    now = datetime.now(timezone.utc)
    reopened = make_event("reopened", date=now)
    reopened.event_entry.object_type = "project"
    reopened.event_entry.event_type = "unarchived"
    reopened.event_entry.object_id = "child"
    reopened.event_entry.v2_object_id = "child-v2"
    older = make_event("older", date=now - timedelta(days=2))
    older.event_entry.object_type = "project"
    older.event_entry.event_type = "unarchived"
    older.event_entry.object_id = "old-child"
    assert dashboard_runtime._reopened_project_ids(
        {reopened, older}, (now - timedelta(days=1)).timestamp()
    ) == {"child", "child-v2"}


def test_project_refresh_counts_events_since_snapshot_even_if_already_cached() -> None:
    refreshed_at = datetime.now(timezone.utc).timestamp() - 3600
    changed = make_event(
        "changed",
        date=datetime.now(timezone.utc),
        parent_project_id="changed-project",
    )
    old = make_event(
        "old",
        date=datetime.now(timezone.utc) - timedelta(days=4),
        parent_project_id="old-project",
    )

    project_ids = dashboard_runtime._project_ids_changed_since(
        {changed, old}, refreshed_at
    )

    assert "changed-project" in project_ids
    assert "old-project" not in project_ids


def test_load_state_from_disk_cache_rejects_stale_adjustment_signature(
    monkeypatch, tmp_path
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    _save_dashboard_snapshot(cache)

    personal_dir = tmp_path / "personal"
    personal_dir.mkdir()
    (personal_dir / "adj_private.py").write_text(
        "link_adjustements = {}\narchived_parent_projects = ['MSFT']\n",
        encoding="utf-8",
    )

    state = DashboardState()
    loaded = dashboard_runtime._load_state_from_disk_cache(
        state, source=runtime.sync_source(demo_mode=False), cache=cache
    )
    assert loaded is False


@pytest.mark.parametrize(
    "projection_only", [False, True], ids=["saved-events", "saved-projection"]
)
def test_history_reflects_saved_project_rules_using_local_metadata(
    monkeypatch, tmp_path, api_client, projection_only
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")
    monkeypatch.setenv(str(EnvVar.PERSONAL_DIR), str(tmp_path / "personal"))
    monkeypatch.setenv(str(EnvVar.TIMEZONE), "UTC")
    event = make_event("saved-event", parent_project_id="child")
    if projection_only:
        Path(cache.activity.path).unlink()
    else:
        cache.activity.save({event})
    previous = make_project(project_id="previous", name="Work")
    child = make_project(project_id="child", name="Child", parent_id="previous")
    archive = make_project(project_id="archive", name="Archive", is_archived=True)
    _save_dashboard_snapshot(
        cache,
        activity_cache_signature=None,
        active_projects=[previous, child],
        archived_projects=[archive],
        df_activity=_event_df(
            [
                {
                    "date": event.date.isoformat(),
                    "id": event.id,
                    "type": "completed",
                    "task_id": "task",
                    "parent_project_id": "child",
                    "parent_project_name": "Child",
                    "root_project_id": "previous",
                    "root_project_name": "Work",
                }
            ]
        ),
    )

    def unexpected_todoist_request(*args, **kwargs):
        pytest.fail("Rebuilding mapped history contacted Todoist")

    monkeypatch.setattr(TodoistAPIClient, "request_json", unexpected_todoist_request)
    old = _history(api_client, "2024-01-01", "2024-01-01")
    assert old.status_code == 200
    assert (
        old.json()["figures"]["activeProjectHierarchy"]["data"][0]["ids"][1]
        == "previous"
    )

    monkeypatch.setattr(
        admin_settings,
        "_load_projects_for_adjustments_sync",
        lambda _db, _refresh: [previous, child, archive],
    )
    group = {"targetId": "archive", "targetName": "Archive"}
    saved = api_client.put(
        "/api/admin/project_adjustments?file=projects.py",
        json={"projectMappings": {"archive": group, "child": group}},
    )
    assert saved.status_code == 200

    updated = _history(api_client, "2024-01-01", "2024-01-01")
    assert updated.status_code == 200
    assert updated.json()["eventCount"] == old.json()["eventCount"] == 1
    assert (
        updated.json()["figures"]["activeProjectHierarchy"]["data"][0]["ids"][1]
        == "archive"
    )

    state = api_client.app.state.dashboard
    assert state.snapshot.df_activity.iloc[0]["root_project_id"] == "archive"
    assert state.snapshot.df_activity.iloc[0]["root_project_name"] == "Archive"
    assert state.snapshot.db is None
    if projection_only:
        assert not Path(cache.activity.path).exists()
    else:
        assert {item.id for item in cache.activity.load()} == {"saved-event"}


def test_load_state_from_disk_cache_rejects_legacy_demo_snapshot(
    monkeypatch, tmp_path
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    _save_dashboard_snapshot(
        cache,
        demo_mode=True,
        demo_state_version=dashboard_runtime._DEMO_DASHBOARD_STATE_SCHEMA_VERSION - 1,
        project_colors={"A": "#123456"},
    )

    state = DashboardState()
    loaded = dashboard_runtime._load_state_from_disk_cache(
        state, source=runtime.sync_source(demo_mode=True), cache=cache
    )
    assert loaded is False


def test_load_state_from_disk_cache_restores_current_demo_snapshot(
    monkeypatch, tmp_path
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    df = _single_event_df()
    _save_dashboard_snapshot(
        cache,
        demo_mode=True,
        demo_state_version=dashboard_runtime._DEMO_DASHBOARD_STATE_SCHEMA_VERSION,
        df_activity=df,
        project_colors={"A": "#123456"},
    )

    state = DashboardState()
    loaded = dashboard_runtime._load_state_from_disk_cache(
        state, source=runtime.sync_source(demo_mode=True), cache=cache
    )
    assert loaded is True
    assert state.snapshot.df_activity is not None
    assert len(state.snapshot.df_activity) == 1
    assert state.snapshot.project_colors == {"A": "#123456"}
    assert state.snapshot.active_projects == []
    assert state.snapshot.archived_projects == []
    assert state.snapshot.demo_mode is True


def test_dashboard_home_bootstraps_from_disk_cache_without_refresh(
    monkeypatch, tmp_path, api_client
) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    _save_dashboard_snapshot(cache, last_refresh_s=time.time())
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")

    def unexpected_refresh(state, *, source, result):
        pytest.fail("Dashboard startup must reuse the fresh saved snapshot")

    monkeypatch.setattr(dashboard_runtime, "_refresh_state_sync", unexpected_refresh)

    res = _home(api_client)
    assert res.status_code == 200
    state = api_client.app.state.dashboard
    assert state.snapshot.df_activity["id"].tolist() == ["e1"]
    assert state.sync.task is None


@pytest.mark.parametrize(
    "query", ["weeks=10000", "beg=2025-01-01"], ids=["weeks-too-large", "missing-end"]
)
def test_dashboard_home_rejects_invalid_ranges(api_client, saved_dashboard, query) -> None:
    saved_dashboard(_single_event_df())

    assert _home(api_client, query).status_code == 400


def test_task_count_trend_uses_selected_dashboard_viewport(
    api_client, saved_dashboard
) -> None:
    saved_dashboard(_single_event_df())

    res = _home(
        api_client,
        "beg=2025-01-01&end=2025-01-15&granularity=W",
    )

    assert res.status_code == 200
    xaxis = res.json()["figures"]["taskCountTrend"]["layout"]["xaxis"]
    assert xaxis["range"] == ["2025-01-01T00:00:00", "2025-01-16T00:00:00"]
    assert xaxis["rangeslider"]["visible"] is True


@pytest.mark.parametrize(
    ("granularity", "forecast_date", "viewport_end"),
    [
        ("W", "2025-05-11T00:00:00", "2025-05-12T00:00:00"),
        ("ME", "2025-05-31T00:00:00", "2025-06-01T00:00:00"),
        ("3ME", "2025-06-30T00:00:00", "2025-07-01T00:00:00"),
    ],
)
def test_dashboard_completion_charts_show_the_entire_forecast_period(
    monkeypatch, api_client, saved_dashboard, granularity, forecast_date, viewport_end
) -> None:
    monkeypatch.setenv("TODOIST_TIMEZONE", "Europe/Warsaw")
    saved_dashboard(
        _event_df(
            [
                {
                    "date": timestamp,
                    "id": str(index),
                    "title": "done",
                    "type": "completed",
                    "parent_project_name": "Product",
                    "root_project_name": "Product",
                    "task_id": str(index),
                }
                for index, timestamp in enumerate(
                    ["2025-02-20T09:00:00", "2025-05-07T10:00:00"]
                )
            ]
        ),
    )
    rolling = _home(api_client, f"weeks=12&granularity={granularity}").json()
    history = _home(
        api_client, f"beg=2025-05-04&end=2025-05-07&granularity={granularity}"
    ).json()
    assert rolling["range"]["end"] == "2025-05-07"

    for key in ("completedTasksPeriodically", "cumsumCompletedTasksPeriodically"):
        figure = rolling["figures"][key]
        forecast = next(
            trace for trace in figure["data"] if trace["name"] == "Product (forecast)"
        )
        assert forecast["x"] == [forecast_date]
        axis = figure["layout"]["xaxis"]
        assert axis["range"] == ["2025-02-12T11:00:00.000001", viewport_end]
        assert axis["rangeslider"]["visible"] is True
        assert history["figures"][key]["layout"]["xaxis"]["range"] == [
            "2025-05-04T00:00:00",
            "2025-05-08T00:00:00",
        ]


def test_dashboard_home_last_completed_week_parent_share(
    api_client, saved_dashboard
) -> None:
    saved_dashboard(
        _event_df(
            [
                {
                    "date": "2025-01-06",
                    "id": "c1",
                    "title": "x",
                    "type": "completed",
                    "parent_project_name": "Parent A",
                    "root_project_name": "Root 1",
                    "task_id": "t1",
                },
                {
                    "date": "2025-01-07",
                    "id": "c2",
                    "title": "y",
                    "type": "completed",
                    "parent_project_name": "Parent A",
                    "root_project_name": "Root 1",
                    "task_id": "t2",
                },
                {
                    "date": "2025-01-08",
                    "id": "c3",
                    "title": "z",
                    "type": "completed",
                    "parent_project_name": "Parent B",
                    "root_project_name": "Root 2",
                    "task_id": "t3",
                },
                # Not completed:
                {
                    "date": "2025-01-09",
                    "id": "a1",
                    "title": "n",
                    "type": "added",
                    "parent_project_name": "Parent A",
                    "root_project_name": "Root 1",
                    "task_id": "t4",
                },
                # Anchor (partial week, should be excluded from last completed week):
                {
                    "date": "2025-01-15",
                    "id": "c4",
                    "title": "w",
                    "type": "completed",
                    "parent_project_name": "Parent C",
                    "root_project_name": "Root 3",
                    "task_id": "t5",
                },
            ]
        ),
    )

    res = _home(api_client)
    assert res.status_code == 200
    payload = res.json()

    last_week = payload["leaderboards"]["lastCompletedWeek"]
    assert "weeklyCompletionTrend" in payload["figures"]
    assert "activeProjectHierarchy" in payload["figures"]
    assert "mostPopularLabels" not in payload["figures"]
    assert last_week["label"] == "2025-01-06 to 2025-01-12"
    parent_items = last_week["parentProjects"]["items"]
    assert last_week["parentProjects"]["totalCompleted"] == 3

    by_name = {it["name"]: it for it in parent_items}
    assert by_name["Parent A"]["completed"] == 2
    assert by_name["Parent B"]["completed"] == 1
    assert abs(by_name["Parent A"]["percentOfCompleted"] - 66.67) < 0.02
    assert abs(by_name["Parent B"]["percentOfCompleted"] - 33.33) < 0.02


def test_dashboard_home_handles_empty_activity(api_client, saved_dashboard) -> None:
    saved_dashboard(_empty_event_df())

    res = _home(api_client, "weeks=12")
    assert res.status_code == 200
    payload = res.json()
    assert payload["range"]["weeks"] == 12
    assert payload["leaderboards"]["lastCompletedWeek"]["label"]
    assert payload.get("noData") is True


def test_dashboard_home_includes_habit_tracker_summary(api_client, saved_dashboard) -> None:
    snapshot = saved_dashboard(
        _event_df(
            [
                {
                    "date": "2025-01-07",
                    "id": "c1",
                    "title": "Morning walk",
                    "type": "completed",
                    "parent_project_name": "Health",
                    "root_project_name": "Health",
                    "task_id": "habit-1",
                },
                {
                    "date": "2025-01-08",
                    "id": "r1",
                    "title": "Morning walk",
                    "type": "rescheduled",
                    "parent_project_name": "Health",
                    "root_project_name": "Health",
                    "task_id": "habit-1",
                },
                {
                    "date": "2025-01-15",
                    "id": "anchor-1",
                    "title": "Another task",
                    "type": "completed",
                    "parent_project_name": "Health",
                    "root_project_name": "Health",
                    "task_id": "anchor-task",
                },
            ]
        ),
    )
    snapshot.active_projects = [
        make_project(
            project_id="project-1",
            project_entry=make_project_entry(
                project_id="project-1",
                name="Health",
                color="green",
            ),
            tasks=[
                make_task(
                    "habit-1",
                    content="Morning walk",
                    project_id="project-1",
                    labels=["track_habit"],
                )
            ],
        )
    ]
    snapshot.project_colors = {"Health": "#00aa88"}

    res = _home(api_client)
    assert res.status_code == 200
    payload = res.json()

    habit_tracker = payload["habitTracker"]
    assert habit_tracker["trackedCount"] == 1
    assert habit_tracker["totals"]["weeklyCompleted"] == 1
    assert habit_tracker["totals"]["weeklyRescheduled"] == 1
    assert habit_tracker["items"][0]["name"] == "Morning walk"
    assert habit_tracker["figure"]["data"]


def test_dashboard_home_normalizes_integer_activity_index(
    api_client, saved_dashboard
) -> None:
    df = pd.DataFrame(
        [
            {
                "date": "2025-01-07",
                "id": "e1",
                "title": "Morning walk",
                "type": "completed",
                "parent_project_name": "Health",
                "root_project_name": "Health",
                "task_id": "habit-1",
            },
            {
                "date": "2025-01-08",
                "id": "e2",
                "title": "Morning walk",
                "type": "rescheduled",
                "parent_project_name": "Health",
                "root_project_name": "Health",
                "task_id": "habit-1",
            },
        ]
    )
    df.index = [0, 1]
    saved_dashboard(df, project_colors={"Health": "#00aa88"})

    res = _home(api_client)
    assert res.status_code == 200
    payload = res.json()
    assert payload["metrics"]["items"]


def test_dashboard_status_returns_services(monkeypatch, tmp_path, api_client) -> None:
    cache = _dashboard_cache(monkeypatch, tmp_path)
    activity_path = Path(cache.activity.path).resolve()
    res = api_client.get("/api/dashboard/status")
    assert res.status_code == 200
    payload = res.json()
    services = {service["name"]: service for service in payload["services"]}
    assert "Todoist token" in services
    assert services["Activity cache"]["status"] == "ok"
    assert services["Activity cache"]["detail"] == payload["activityCache"]
    assert payload["activityCache"]["path"] == str(activity_path)
    assert payload["activityCache"]["size"] == activity_path.stat().st_size
    assert api_client.app.state.dashboard.sync.task is None


def test_dashboard_home_includes_urgency_status(api_client, saved_dashboard) -> None:
    snapshot = saved_dashboard(_single_event_df())
    api_client.app.state.dashboard_config_path = (
        REPO_ROOT / "configs" / "dashboard.yaml"
    )

    snapshot.active_projects = [
        make_project(
            project_id="proj-urgency",
            project_entry=make_project_entry(project_id="proj-urgency", name="Urgency"),
            tasks=[
                make_task("p1-1", content="Priority 1", priority=4),
                make_task(
                    "due-1",
                    content="Due today",
                    due={"date": date.today().isoformat()},
                ),
            ],
        )
    ]
    snapshot.project_colors = {"Urgency": "#44aa66"}

    res = _home(api_client)

    assert res.status_code == 200
    payload = res.json()
    urgency_status = payload["urgencyStatus"]
    assert urgency_status["state"] == "warn"
    assert urgency_status["badgeLabel"] == "Watch"
    assert urgency_status["total"] == 2
    assert urgency_status["counts"]["p1Tasks"] == 1
    assert urgency_status["counts"]["dueTasks"] == 1
    assert urgency_status["counts"]["fireTasks"] == 0
    assert urgency_status["visibleChips"] == [
        "fireTasks",
        "p1Tasks",
        "dueTasks",
        "deadlineTasks",
    ]
    assert payload["configurableItems"][0]["icon"] == "wrench"
    assert isinstance(payload["figures"]["activeProjectHierarchy"], dict)


def test_dashboard_home_renders_active_and_archived_main_projects(
    api_client, saved_dashboard, tmp_path
) -> None:
    parent = make_project(project_id="parent", name="Parent")
    active = make_project(
        project_id="active",
        project_entry=make_project_entry(
            project_id="active", name="Active", parent_id="parent"
        ),
    )
    archived = make_project(
        project_id="archived",
        project_entry=make_project_entry(
            project_id="archived", name="Archived", parent_id="active"
        ),
        is_archived=True,
    )
    saved_dashboard(
        _event_df(
            [
                {
                    "date": "2025-01-15",
                    "id": project_id,
                    "title": "Task",
                    "type": "completed",
                    "parent_project_id": project_id,
                    "parent_project_name": name,
                    "root_project_name": "Active",
                    "task_id": project_id,
                }
                for project_id, name in [("active", "Active"), ("archived", "Archived")]
            ]
        ),
        active_projects=[parent, active],
        archived_projects=[archived],
    )
    id_rule = {"active": {"targetId": "active", "targetName": "Active"}}
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "adj_private.py").write_text(
        render_adjustments_file_content({"Archived": "Active"}, ["Archived"], id_rule),
        encoding="utf-8",
    )

    response = _home(api_client)

    assert response.status_code == 200
    figure = response.json()["figures"]["activeProjectHierarchy"]["data"][0]
    levels = dict(zip(figure["ids"], figure["parents"], strict=True))
    assert levels["active"] == levels["archived"] == "active-projects"


def test_stale_ready_state_refreshes_in_background_without_blocking_cached_reads(
    monkeypatch,
    tmp_path,
):
    _dashboard_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("API_KEY", "")
    source = runtime.sync_source(demo_mode=False)
    state = DashboardState()
    state.snapshot = DashboardSnapshot(
        df_activity=_single_event_df(), last_refresh_s=time.time() - 3600
    )
    monkeypatch.setattr(
        dashboard_runtime, "_activity_cache_signature", lambda _cache: None
    )
    refreshed = []

    def refresh(state, *, source, result):
        refreshed.append(True)
        state.snapshot.last_refresh_s = time.time()

    monkeypatch.setattr(dashboard_runtime, "_refresh_state_sync", refresh)

    async def run():
        await dashboard_runtime.ensure_state(state, refresh=False, source=source)
        assert state.sync.task is not None
        assert state.snapshot.df_activity is not None
        await state.sync.task
        assert refreshed == [True]
        await dashboard_runtime.ensure_state(state, refresh=False, source=source)
        assert refreshed == [True]

    asyncio.run(run())


def test_failed_background_refresh_keeps_cached_data_and_throttles_retries(
    monkeypatch, tmp_path
):
    _dashboard_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("API_KEY", "")
    source = runtime.sync_source(demo_mode=False)
    state = DashboardState()
    state.snapshot = DashboardSnapshot(
        df_activity=_single_event_df(), last_refresh_s=time.time() - 3600
    )
    monkeypatch.setattr(
        dashboard_runtime, "_activity_cache_signature", lambda _cache: None
    )

    def offline(state, *, source, result):
        raise ConnectionError("offline")

    monkeypatch.setattr(dashboard_runtime, "_refresh_state_sync", offline)

    async def run():
        await dashboard_runtime.ensure_state(state, refresh=False, source=source)
        task = state.sync.task
        await task
        assert len(state.snapshot.df_activity) == 1
        assert state.sync.status_for(source)["dashboardError"] == "Could not update the dashboard."
        await dashboard_runtime.ensure_state(state, refresh=False, source=source)
        assert state.sync.task is task

    asyncio.run(run())


def test_explicit_refresh_joins_an_existing_background_sync(monkeypatch, tmp_path):
    _dashboard_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("API_KEY", "")
    source = runtime.sync_source(demo_mode=False)
    state = DashboardState(snapshot=DashboardSnapshot(_single_event_df()))

    async def run():
        gate = asyncio.Event()

        async def refresh_dashboard(_state, _source, _result):
            await gate.wait()

        monkeypatch.setattr(dashboard_runtime, "_refresh_dashboard", refresh_dashboard)
        await dashboard_runtime.ensure_state(state, refresh=False, source=source)
        task = state.sync.task
        assert task is not None
        request = asyncio.create_task(
            dashboard_runtime.ensure_state(state, refresh=True, source=source)
        )
        await asyncio.sleep(0)
        assert not request.done()
        assert state.sync.task is task
        gate.set()
        await request
        assert task.done()

    asyncio.run(run())


def test_state_refresh_uses_the_runtime_credential_file(monkeypatch, tmp_path):
    env = tmp_path / "runtime" / ".env"
    env.parent.mkdir()
    env.write_text("API_KEY=fixture-token\n", encoding="utf-8")
    received = []

    class StopAtDatabase:
        def __init__(self, path, *, api_key):
            received.append((path, api_key))
            raise RuntimeError("stop before network access")

    monkeypatch.setattr(dashboard_runtime, "Database", StopAtDatabase)
    monkeypatch.setenv(str(EnvVar.DATA_DIR), str(env.parent))
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path / "cache"))
    monkeypatch.setenv("API_KEY", "")
    with pytest.raises(RuntimeError, match="stop before network"):
        dashboard_runtime._refresh_state_sync(
            DashboardState(), source=runtime.sync_source(demo_mode=False), result=None
        )
    assert received == [(str(env), "fixture-token")]


def test_background_refresh_preserves_partial_sync_warnings(monkeypatch, tmp_path):
    _dashboard_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("API_KEY", "")
    source = runtime.sync_source(demo_mode=False)
    state = DashboardState(snapshot=DashboardSnapshot(_single_event_df()))

    def partial_refresh(state, *, source, result):
        return "Activity history could not be refreshed."

    monkeypatch.setattr(dashboard_runtime, "_refresh_state_sync", partial_refresh)

    async def run():
        await dashboard_runtime.ensure_state(state, refresh=False, source=source)
        assert state.sync.task is not None
        await state.sync.task
        assert (
            state.sync.status_for(source)["dashboardError"]
            == "Activity history could not be refreshed."
        )

    asyncio.run(run())


def test_failed_mode_change_does_not_return_data_from_the_previous_mode(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path))
    monkeypatch.setenv("API_KEY", "")
    source = runtime.sync_source(demo_mode=False)
    state = DashboardState()
    state.snapshot = DashboardSnapshot(df_activity=_single_event_df(), demo_mode=True)

    def offline(state, *, source, result):
        raise ConnectionError("offline")

    monkeypatch.setattr(dashboard_runtime, "_refresh_state_sync", offline)

    async def run():
        with pytest.raises(HTTPException) as error:
            await dashboard_runtime.ensure_state(state, refresh=True, source=source)
        assert error.value.status_code == 503

    asyncio.run(run())
