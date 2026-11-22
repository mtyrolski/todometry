"""Tests for FastAPI runtime and status endpoints."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest

import todometry
import todometry.web.api_components.runtime as runtime_component
import todometry.web.api_components.dashboard_runtime as dashboard_runtime_component
from todometry.api import TodoistAPIClient, TodoistEndpoints
from todometry.core.env import EnvVar
from todometry.core.utils import Cache
from todometry.database.base import Database
from todometry.features import data_sync
from tests.unit.database.helpers import event_payload, make_event
from tests.web_api_helpers import _single_event_df

# pylint: disable=protected-access


def _dashboard_log_dir(root: Path) -> Path:
    dashboard_dir = root / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    return dashboard_dir


@pytest.fixture
def activity_job(api_client, monkeypatch, tmp_path):
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path))
    monkeypatch.setenv(str(EnvVar.DATA_DIR), str(tmp_path))
    monkeypatch.setenv(str(EnvVar.PERSONAL_DIR), str(tmp_path / "personal"))
    monkeypatch.setenv(str(EnvVar.TIMEZONE), "Europe/Warsaw")
    monkeypatch.delenv(str(EnvVar.DASHBOARD_DEMO), raising=False)
    monkeypatch.setenv("API_KEY", "test-token")
    monkeypatch.setattr(
        api_client.app.state, "dashboard_config_path", tmp_path / "dashboard.yaml"
    )
    return api_client.app.state.dashboard.sync


def _wait_for_activity_job(api_client, activity_job) -> None:
    async def wait():
        assert activity_job.task is not None
        await activity_job.task

    assert api_client.portal is not None
    api_client.portal.call(wait)


def test_activity_sync_runs_in_background_and_publishes_verified_history(
    monkeypatch, api_client, activity_job
) -> None:
    start = Event()
    release = Event()
    clock = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(data_sync, "_now_utc", lambda: clock)
    Cache().activity.save({make_event("saved", date=datetime(2026, 11, 19, 12))})
    did_fail = False
    calls = []
    response_event = event_payload("fresh", event_date="2026-11-20T23:30:00Z")

    def request_page(_client, spec, **_kwargs):
        calls.append(spec.params)
        start.set()
        assert release.wait(timeout=5)
        if did_fail:
            raise ConnectionError("Todoist unavailable")
        return {"results": [response_event], "next_cursor": None}

    monkeypatch.setattr(TodoistAPIClient, "request_json", request_page)
    month = "/api/activity/status?month=2026-11"
    existing = api_client.get(month).json()
    assert not calls
    assert existing["firstEventDate"] == "2026-11-19"
    assert existing["lastSyncedAt"] is None
    assert existing["days"]["2026-11-19"]["coverage"] == "unverified"
    assert existing["years"]["2026"] == {"coverage": "unverified", "eventCount": 1}
    assert existing["months"]["2026-11"] == {"coverage": "unverified", "eventCount": 1}

    try:
        response = api_client.post("/api/activity/sync")
        assert response.status_code == 202
        assert start.wait(timeout=5)
        status = response.json()["sync"]
        assert status["state"] == "running"
        assert api_client.post("/api/activity/sync").json()["sync"] == status
        poll = api_client.get("/api/activity/status?sync_only=true")
        assert poll.json() == {"demoMode": False, "sync": status}
        assert api_client.get(month).json()["lastSyncedAt"] is None
    finally:
        release.set()

    _wait_for_activity_job(api_client, activity_job)
    completed = api_client.get(month).json()
    assert len(calls) == 1
    assert completed["sync"]["state"] == "succeeded"
    assert completed["sync"]["newEvents"] == 1
    assert completed["lastSyncedAt"] == clock.isoformat()
    assert completed["timeZone"] == "Europe/Warsaw"
    assert completed["latestEventDate"] == "2026-11-21"
    assert completed["days"]["2026-11-21"] == {"coverage": "covered", "eventCount": 1}
    assert completed["days"]["2026-11-20"] == {"coverage": "covered", "eventCount": 0}
    assert completed["years"]["2026"] == {"coverage": "partial", "eventCount": 2}
    assert completed["months"]["2026-11"] == {"coverage": "partial", "eventCount": 2}

    clock += timedelta(hours=1)
    did_fail = True
    api_client.post("/api/activity/sync")
    _wait_for_activity_job(api_client, activity_job)
    failed = api_client.get(month).json()
    assert failed["sync"]["state"] == "failed"
    assert failed["sync"]["error"] == "Could not sync with Todoist. Try again."
    assert failed["sync"]["startedAt"] > completed["sync"]["startedAt"]
    assert failed["lastSyncedAt"] == completed["lastSyncedAt"]
    assert failed["days"] == completed["days"]

    clock += timedelta(days=1, microseconds=321000)
    did_fail = False
    response_event = event_payload("retried", event_date="2026-11-22T12:00:00Z")
    assert api_client.post("/api/activity/sync").status_code == 202
    _wait_for_activity_job(api_client, activity_job)
    retried = api_client.get(month).json()
    assert len(calls) == 3
    assert calls[-1]["date_from"] == calls[-2]["date_from"]
    assert retried["sync"]["state"] == "succeeded"
    assert retried["sync"]["startedAt"] > failed["sync"]["startedAt"]
    assert retried["sync"]["error"] is None
    assert retried["sync"]["newEvents"] == 1
    assert retried["lastSyncedAt"] == clock.isoformat()
    assert retried["days"]["2026-11-22"] == {"coverage": "covered", "eventCount": 1}


@pytest.mark.parametrize(
    "recent_first", [False, True], ids=["full-first", "recent-first"]
)
def test_activity_and_dashboard_refresh_share_scan_with_separate_projection_result(
    monkeypatch, api_client, activity_job, recent_first
) -> None:
    cache = Cache()
    cache.activity.save(
        {make_event("old"), make_event("saved", date=datetime(2026, 11, 19, 12))}
    )
    clock = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(data_sync, "_now_utc", lambda: clock)
    scan_started = Event()
    release_scan = Event()
    projection_started = Event()
    release_projection = Event()
    scans = []
    changed_projects = []

    def request_page(_client, spec, **_kwargs):
        if spec.endpoint == TodoistEndpoints.LIST_LABELS:
            return {"results": [], "next_cursor": None}
        assert spec.endpoint == TodoistEndpoints.LIST_ACTIVITY
        scans.append(spec.params)
        scan_started.set()
        assert release_scan.wait(timeout=5)
        return {
            "results": [event_payload("fresh", event_date="2026-11-20T23:30:00Z")],
            "next_cursor": None,
        }

    def fetch_projects(database, _previous, changed):
        changed_projects.append(changed)
        database.projects_cache = []
        return database.projects_cache

    def fail_projection(_database, *, cache, hierarchy):
        projection_started.set()
        assert release_projection.wait(timeout=5)
        raise ValueError("could not compute the dashboard view")

    monkeypatch.setattr(TodoistAPIClient, "request_json", request_page)
    monkeypatch.setattr(Database, "fetch_projects_incrementally", fetch_projects)
    monkeypatch.setattr(Database, "fetch_archived_projects", lambda _database: [])
    monkeypatch.setattr(
        dashboard_runtime_component, "load_activity_data", fail_projection
    )
    month = "/api/activity/status?month=2026-11"
    home = "/api/dashboard/home"
    saved_home = f"{home}?cached_only=true"
    progress = "/api/dashboard/progress"
    assert api_client.get(saved_home).status_code == 200

    try:
        if recent_first:
            started = api_client.post("/api/activity/sync").json()["sync"]
            assert scan_started.wait(timeout=5)
            assert api_client.get(home).json()["sync"]["state"] == "running"
        else:
            assert api_client.get(home).json()["sync"]["state"] == "running"
            assert scan_started.wait(timeout=5)
            started = api_client.post("/api/activity/sync").json()["sync"]

        assert started["state"] == "running"
        assert api_client.get(progress).json()["sync"] == started
        assert api_client.get(month).json()["sync"] == started
        assert api_client.get(month).json()["lastSyncedAt"] is None

        release_scan.set()
        assert projection_started.wait(timeout=5)
        assert api_client.get(saved_home).status_code == 200
        joined = api_client.post("/api/activity/sync").json()["sync"]
        assert joined["startedAt"] == started["startedAt"]
    finally:
        release_scan.set()
        release_projection.set()

    _wait_for_activity_job(api_client, activity_job)
    assert len(scans) == 1
    assert len(changed_projects) == 1
    assert "proj1" in changed_projects[0]
    completed = api_client.get(month).json()
    assert completed["sync"]["state"] == "succeeded"
    assert completed["sync"]["newEvents"] == 1
    assert completed["sync"]["error"] is None
    assert completed["sync"]["dashboardError"]
    assert completed["lastSyncedAt"] == clock.isoformat()
    assert completed["days"]["2026-11-21"] == {"coverage": "covered", "eventCount": 1}
    status = api_client.get(progress).json()
    assert status["active"] is False
    assert status["error"] == completed["sync"]["dashboardError"]
    assert status["sync"] == completed["sync"]
    saved = api_client.get(saved_home)
    assert saved.status_code == 200
    payload = saved.json()
    assert payload["figures"]
    assert payload["sync"] == completed["sync"]


def test_activity_sync_does_not_join_a_different_connection(
    monkeypatch, api_client, activity_job, tmp_path
) -> None:
    old_cache = Cache()
    old_cache.activity.save({make_event("saved")})
    first_page_started = Event()
    release_first_page = Event()
    requests = []

    def request_page(client, spec, **_kwargs):
        authorization = client._build_headers(spec.headers)["Authorization"]
        if spec.endpoint == TodoistEndpoints.LIST_LABELS:
            requests.append((authorization, "labels"))
            return {"results": [], "next_cursor": None}
        assert spec.endpoint == TodoistEndpoints.LIST_ACTIVITY
        cursor = spec.params.get("cursor")
        requests.append((authorization, cursor))
        date = "2024-01-01T12:00:00Z"
        if len(requests) == 1:
            first_page_started.set()
            assert release_first_page.wait(timeout=5)
            return {
                "results": [event_payload("first", event_date=date)],
                "next_cursor": "next",
            }
        event_id = "second" if cursor else "other-source"
        return {
            "results": [event_payload(event_id, event_date=date)],
            "next_cursor": None,
        }

    def fail_projection(*_args):
        raise ConnectionError("projects unavailable after reading labels")

    monkeypatch.setattr(TodoistAPIClient, "request_json", request_page)
    monkeypatch.setattr(Database, "fetch_projects_incrementally", fail_projection)
    home = "/api/dashboard/home"
    assert api_client.get(f"{home}?cached_only=true").status_code == 200
    try:
        assert api_client.get(home).json()["sync"]["state"] == "running"
        assert first_page_started.wait(timeout=5)
        monkeypatch.setenv("API_KEY", "other-token")
        assert api_client.post("/api/activity/sync").status_code == 409
        monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path / "other"))
        assert api_client.post("/api/activity/sync").status_code == 409
        status = api_client.get("/api/activity/status?sync_only=true").json()["sync"]
        assert status["state"] == "idle"
    finally:
        release_first_page.set()

    _wait_for_activity_job(api_client, activity_job)
    assert {event.id for event in old_cache.activity.load()} == {
        "saved",
        "first",
        "second",
    }
    assert not Path(Cache().activity.path).exists()
    assert api_client.post("/api/activity/sync").status_code == 202
    _wait_for_activity_job(api_client, activity_job)
    assert requests == [
        ("Bearer test-token", None),
        ("Bearer test-token", "next"),
        ("Bearer test-token", "labels"),
        ("Bearer other-token", None),
    ]
    assert {event.id for event in Cache().activity.load()} == {"other-source"}


def test_activity_status_in_demo_hides_live_history_and_rejects_sync(
    monkeypatch, api_client, activity_job
) -> None:
    Cache().activity.save({make_event("saved")})
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "1")

    history = api_client.get("/api/activity/status?month=2024-01").json()
    assert history["demoMode"] is True
    assert history["firstEventDate"] is None
    assert history["lastSyncedAt"] is None
    assert history["days"]["2024-01-01"]["eventCount"] == 0
    assert api_client.post("/api/activity/sync").status_code == 409
    assert activity_job.task is None
    assert api_client.get("/api/activity/status?month=9999-12").status_code == 422


@pytest.fixture
def runtime_logs(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    dashboard_dir = _dashboard_log_dir(cache_dir)
    (dashboard_dir / "api.log").write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    (dashboard_dir / "rogue.log").write_text("rogue line\n", encoding="utf-8")
    (dashboard_dir / "triton.log").write_text("legacy line\n", encoding="utf-8")
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(cache_dir))
    monkeypatch.setenv(str(EnvVar.DATA_DIR), str(tmp_path / "data"))


def test_runtime_logs_lists_only_allowlisted_sources(api_client, runtime_logs) -> None:
    response = api_client.get("/api/runtime/logs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["inspectOnly"] is True
    assert [item["id"] for item in payload["sources"]] == ["api", "frontend"]
    api, frontend = payload["sources"]
    assert api["available"] is True
    assert api["path"] == "dashboard/api.log"
    assert frontend["available"] is False
    assert all(item["inspectOnly"] for item in payload["sources"])


def test_runtime_log_read_pages_an_allowlisted_source(api_client, runtime_logs) -> None:
    response = api_client.get(
        "/api/runtime/logs/read",
        params={"source": "api", "tail_lines": 2, "page": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "api"
    assert payload["label"] == "Backend API"
    assert payload["path"] == "dashboard/api.log"
    assert payload["inspectOnly"] is True
    assert payload["content"] == "line-2\nline-3\n"
    assert payload["totalLines"] == 3


@pytest.mark.parametrize(
    ("source", "detail"),
    [("rogue", "Unknown"), ("triton", "Unknown"), ("frontend", "not available yet")],
)
def test_runtime_log_read_rejects_unavailable_sources(
    api_client, runtime_logs, source, detail
) -> None:
    response = api_client.get("/api/runtime/logs/read", params={"source": source})
    assert response.status_code == 404
    assert detail in response.json()["detail"]


def test_admin_timezone_status_uses_system_timezone_when_not_configured(
    monkeypatch, tmp_path, api_client
) -> None:
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(str(EnvVar.TIMEZONE), raising=False)
    monkeypatch.setattr(runtime_component, "detect_system_timezone", lambda: "UTC")

    res = api_client.get("/api/admin/timezone")
    assert res.status_code == 200
    payload = res.json()
    assert payload["configured"] is False
    assert payload["timezone"] == "UTC"
    assert payload["source"] == "system"
    assert payload["override"] is None
    assert payload["envPath"] == ".env"
    assert payload["overrideValid"] is True


def test_admin_timezone_set_and_clear(monkeypatch, tmp_path, api_client) -> None:
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_component, "detect_system_timezone", lambda: "UTC")

    client = api_client
    set_response = client.post(
        "/api/admin/timezone",
        json={"timezone": "Europe/Warsaw"},
    )
    assert set_response.status_code == 200
    set_payload = set_response.json()
    assert set_payload["configured"] is True
    assert set_payload["timezone"] == "Europe/Warsaw"
    assert set_payload["source"] == "env"
    assert set_payload["override"] == "Europe/Warsaw"
    assert set_payload["overrideValid"] is True
    assert set_payload["envPath"] == ".env"
    assert os.getenv(str(EnvVar.TIMEZONE)) == "Europe/Warsaw"

    env_path = tmp_path / ".env"
    assert env_path.exists()
    env_text = env_path.read_text(encoding="utf-8")
    assert "TODOIST_TIMEZONE" in env_text
    assert "Europe/Warsaw" in env_text

    clear_response = client.delete("/api/admin/timezone")
    assert clear_response.status_code == 200
    clear_payload = clear_response.json()
    assert clear_payload["configured"] is False
    assert clear_payload["timezone"] == "UTC"
    assert clear_payload["source"] == "system"
    assert clear_payload["override"] is None
    assert clear_payload["envPath"] == ".env"
    assert os.getenv(str(EnvVar.TIMEZONE)) is None

    env_text_after_clear = env_path.read_text(encoding="utf-8")
    assert "TODOIST_TIMEZONE" not in env_text_after_clear


def test_admin_timezone_rejects_invalid_timezone(
    monkeypatch, tmp_path, api_client
) -> None:
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_component, "detect_system_timezone", lambda: "UTC")

    response = api_client.post(
        "/api/admin/timezone",
        json={"timezone": "Invalid/Timezone"},
    )
    assert response.status_code == 400
    payload = response.json()
    assert "Invalid timezone" in payload["detail"]


def test_admin_api_token_status_uses_safe_env_label(
    monkeypatch, tmp_path, api_client
) -> None:
    monkeypatch.setenv(str(EnvVar.DATA_DIR), str(tmp_path))
    monkeypatch.setenv("API_KEY", "test_api_key_12345")

    res = api_client.get("/api/admin/api_token")

    assert res.status_code == 200
    payload = res.json()
    assert payload["configured"] is True
    assert payload["masked"] == "••••2345"
    assert payload["envPath"] == ".env"


def test_admin_api_token_save_clear_and_status_cycle(
    monkeypatch, tmp_path, api_client
) -> None:
    env_path = tmp_path / ".env"
    token = "a" * 32
    monkeypatch.setenv(str(EnvVar.DATA_DIR), str(tmp_path))
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(
        runtime_component, "validate_api_token", lambda _token: (True, None, 3)
    )

    client = api_client

    save = client.post("/api/admin/api_token", json={"token": token, "validate": True})
    assert save.status_code == 200
    assert save.json()["configured"] is True
    assert save.json()["validated"] is True
    assert save.json()["labelsCount"] == 3
    assert os.environ["API_KEY"] == token
    assert f"API_KEY='{token}'" in env_path.read_text(encoding="utf-8")

    status = client.get("/api/admin/api_token")
    assert status.status_code == 200
    assert status.json()["configured"] is True
    assert status.json()["masked"] == "••••aaaa"

    clear = client.delete("/api/admin/api_token")
    assert clear.status_code == 200
    assert clear.json()["configured"] is False
    assert "API_KEY" not in os.environ

    status_after_clear = client.get("/api/admin/api_token")
    assert status_after_clear.status_code == 200
    assert status_after_clear.json()["configured"] is False
    assert status_after_clear.json()["masked"] == ""
    assert "API_KEY" not in env_path.read_text(encoding="utf-8")


def test_openapi_includes_app_version(api_client) -> None:
    res = api_client.get("/openapi.json")
    assert res.status_code == 200
    payload = res.json()
    assert payload["info"]["version"] == api_client.app.version == todometry.__version__


def test_dashboard_progress_reports_idle_state(api_client, activity_job) -> None:
    res = api_client.get("/api/dashboard/progress")
    assert res.status_code == 200
    payload = res.json()

    assert {
        "active",
        "stage",
        "step",
        "totalSteps",
        "startedAt",
        "updatedAt",
        "detail",
        "lanes",
        "error",
    } <= set(payload)
    assert payload["active"] is False
    assert payload["stage"] is None
    assert payload["step"] == payload["totalSteps"] == 0
    assert payload["startedAt"] is None
    assert payload["error"] is None
    assert payload["sync"]["state"] == "idle"


@pytest.fixture
def progress_job(monkeypatch, api_client, activity_job):
    started = Event()
    release = Event()

    def sync(_source):
        started.set()
        assert release.wait(timeout=5)
        return data_sync.SyncResult(set(), set())

    monkeypatch.setattr(runtime_component, "_sync_activity", sync)
    try:
        assert api_client.post("/api/activity/sync").status_code == 202
        assert started.wait(timeout=5)
        yield activity_job
    finally:
        release.set()
        if activity_job.task is not None:
            _wait_for_activity_job(api_client, activity_job)


@pytest.mark.parametrize(
    ("stage", "page_current", "page_total", "window_current", "window_total"),
    [
        ("Fetching activity history", 1, 2, 1, 3),
        ("Fetching archived project activity", 6, 7, 2, 4),
    ],
)
def test_dashboard_progress_ignores_activity_page_counts(
    stage,
    page_current,
    page_total,
    window_current,
    window_total,
    api_client,
    progress_job,
) -> None:
    callback = progress_job._build_tqdm_progress_callback()
    path = "/api/dashboard/progress"
    before = api_client.get(path).json()
    callback(stage, page_current, page_total, "page")

    assert api_client.get(path).json() == before
    callback(stage, window_current, window_total, "window")

    payload = api_client.get(path).json()
    assert payload["active"] is True
    assert payload["detail"] == f"{stage}: {window_current}/{window_total} window"
    assert payload["subCurrent"] == window_current
    assert payload["subTotal"] == window_total


def test_dashboard_progress_uses_verbose_tqdm_detail(api_client, progress_job) -> None:
    callback = progress_job._build_tqdm_progress_callback()
    callback(
        "Fetching activity history",
        1,
        3,
        "window",
        "Fetching activity history: window 1 scanning 2026-03-05 to 2026-05-14 UTC; workers=1",
    )

    payload = api_client.get("/api/dashboard/progress").json()
    assert payload["detail"] == (
        "Fetching activity history: window 1 scanning 2026-03-05 to 2026-05-14 UTC; workers=1"
    )
    assert payload["subCurrent"] == 1
    assert payload["subTotal"] == 3


def test_dashboard_progress_reports_parallel_worker_lanes(
    api_client, progress_job
) -> None:
    callback = progress_job._build_tqdm_progress_callback()
    stage = "Fetching archived project activity"
    callback(stage, 0, 2, "project", f"{stage}: workers=2")
    callback(
        stage,
        4,
        4,
        "window",
        "Skynet: complete",
        "archived-project:skynet",
        "Skynet",
    )
    callback(
        stage,
        0,
        4,
        "window",
        "Academy: scanning",
        "archived-project:academy",
        "Academy",
    )
    callback(
        stage,
        0,
        4,
        "window",
        "Health: queued",
        "archived-project:health",
        "Health",
        "queued",
    )

    payload = api_client.get("/api/dashboard/progress").json()
    lanes = {lane["id"]: lane for lane in payload["lanes"]}

    assert payload["detail"] == f"{stage}: workers=2"
    assert payload["subCurrent"] == 0
    assert payload["subTotal"] == 2
    assert lanes["archived-project:skynet"] == {
        "id": "archived-project:skynet",
        "label": "Skynet",
        "detail": "Skynet: complete",
        "current": 4,
        "total": 4,
        "unit": "window",
        "status": "done",
        "updatedAt": lanes["archived-project:skynet"]["updatedAt"],
    }
    assert lanes["archived-project:academy"]["status"] == "active"
    assert lanes["archived-project:health"]["status"] == "queued"


def test_archived_activity_scan_cache_skips_unchanged_projects_and_invalidates_safely(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path))
    cache = Cache()
    target_holder = {
        "target": dashboard_runtime_component._ArchivedActivityTarget(
            project_id="archived-1",
            label="Archived One",
            updated_at="2026-01-01T00:00:00Z",
        )
    }
    monkeypatch.setattr(
        dashboard_runtime_component,
        "_configured_archived_parent_projects",
        lambda _dbio: {"archived-1": target_holder["target"]},
    )
    state = dashboard_runtime_component.DashboardState()
    event = make_event("archived-event", parent_project_id="archived-1")
    dbio = MagicMock()
    dbio.fetch_activity_for_parent_projects.return_value = [event]

    dashboard_runtime_component._sync_configured_archived_parent_activity(
        state, dbio, set(), cache=cache
    )
    assert cache.activity.load() == {event}
    dashboard_runtime_component._sync_configured_archived_parent_activity(
        state, dbio, {event}, cache=cache
    )
    assert dbio.fetch_activity_for_parent_projects.call_count == 1
    saved_state = cache.archived_activity_scans.load()
    assert saved_state["projects"]["archived-1"]["projectUpdatedAt"] == (
        "2026-01-01T00:00:00Z"
    )

    target_holder["target"] = dashboard_runtime_component._ArchivedActivityTarget(
        project_id="archived-1",
        label="Archived One",
        updated_at="2026-02-01T00:00:00Z",
    )
    dashboard_runtime_component._sync_configured_archived_parent_activity(
        state, dbio, {event}, cache=cache
    )
    assert dbio.fetch_activity_for_parent_projects.call_count == 2

    cache.activity.save(set())
    dashboard_runtime_component._sync_configured_archived_parent_activity(
        state, dbio, set(), cache=cache
    )
    assert dbio.fetch_activity_for_parent_projects.call_count == 3


def test_dashboard_status_excludes_triton_service(api_client) -> None:
    res = api_client.get("/api/dashboard/status")

    assert res.status_code == 200
    payload = res.json()
    assert all(svc.get("name") != "Triton" for svc in payload["services"])


def _activity_cache_for_test(monkeypatch, tmp_path):
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path))
    monkeypatch.setenv(str(EnvVar.DATA_DIR), str(tmp_path))
    monkeypatch.setenv(str(EnvVar.PERSONAL_DIR), str(tmp_path / "personal"))
    monkeypatch.setenv("API_KEY", "test-token")
    cache = Cache()
    cache.activity.save(set())
    return cache


def test_cancelled_cached_read_holds_lock_until_worker_finishes(monkeypatch, tmp_path):
    import asyncio

    runtime = dashboard_runtime_component
    _activity_cache_for_test(monkeypatch, tmp_path)
    state = runtime.DashboardState()
    started = Event()
    release = Event()

    def slow_cache_load(state, *, source):
        started.set()
        if not release.wait(5):
            raise TimeoutError("test did not release cache load")
        state.snapshot = runtime.DashboardSnapshot(
            _single_event_df(), demo_mode=source.demo_mode, source_key=source.identity
        )
        return True

    monkeypatch.setattr(runtime, "_load_saved_activity_state", slow_cache_load)
    source = runtime_component.sync_source(demo_mode=False)

    async def run():
        request = asyncio.create_task(
            runtime.ensure_state(state, refresh=False, cached_only=True, source=source)
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            request.cancel()
            await asyncio.sleep(0)
            assert state.lock.locked()
            assert not request.done()
            next_read = asyncio.create_task(
                runtime.ensure_state(
                    state, refresh=False, cached_only=True, source=source
                )
            )
            await asyncio.sleep(0)
            assert not next_read.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        await next_read
        assert not state.lock.locked()
        assert state.snapshot_for(source)

    asyncio.run(run())


def test_partial_background_refresh_keeps_reads_responsive_and_throttles_retries(
    monkeypatch, tmp_path
):
    import asyncio
    import time
    from todometry.web.api_components import dashboard_runtime as runtime

    cache = _activity_cache_for_test(monkeypatch, tmp_path)
    source = runtime_component.sync_source(demo_mode=False)
    snapshot = runtime.DashboardSnapshot(
        _single_event_df(),
        last_refresh_s=time.time() - 3600,
        source_key=source.identity,
        activity_cache_signature=runtime._activity_cache_signature(cache),
    )
    state = runtime.DashboardState(snapshot=snapshot)
    started = Event()
    release = Event()
    attempts = []

    def sync(_source):
        event = make_event(f"event-{len(attempts)}")
        events = cache.activity.update(lambda existing: existing | {event})
        return data_sync.SyncResult(events, {event})

    def partial_failure(_state, *, source, result):
        assert result is not None
        attempts.append(source.demo_mode)
        started.set()
        if not release.wait(10):
            raise TimeoutError("test did not release refresh")
        raise ConnectionError("project fetch failed")

    monkeypatch.setattr(runtime_component, "_sync_activity", sync)
    monkeypatch.setattr(runtime, "_refresh_state_sync", partial_failure)

    async def run():
        await runtime.ensure_state(state, refresh=False, source=source)
        task = state.sync.task
        assert task is not None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            assert (
                snapshot.activity_cache_signature
                != runtime._activity_cache_signature(cache)
            )
            # Must return before the running refresh releases the state lock.
            await asyncio.wait_for(
                runtime.ensure_state(state, refresh=False, source=source), timeout=2
            )
            assert state.snapshot is snapshot
        finally:
            release.set()
            await task

        assert state.sync.status_for(source)["state"] == "succeeded"
        assert state.sync.status_for(source)["dashboardError"]
        for _ in range(2):
            await runtime.ensure_state(state, refresh=False, source=source)
        assert attempts == [False]
        assert state.sync.task is task

        # Explicit retries start during cooldown and survive a disconnected request.
        started.clear()
        release.clear()
        request = asyncio.create_task(
            runtime.ensure_state(state, refresh=True, source=source)
        )
        try:
            assert await asyncio.to_thread(started.wait, 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert state.sync.status_for(source)["state"] == "running"
        finally:
            release.set()
            assert state.sync.task is not None
            await state.sync.task
        assert attempts == [False, False]

        # A later normal read schedules one retry, despite the changed signature.
        monkeypatch.setattr(runtime, "_STATE_TTL_S", 0.0)
        previous_task = state.sync.task
        await runtime.ensure_state(state, refresh=False, source=source)
        retry = state.sync.task
        assert retry is not None and retry is not previous_task
        await retry
        assert attempts == [False, False, False]

    asyncio.run(run())


def test_changed_activity_cache_refreshes_fresh_snapshot_in_background(
    monkeypatch, tmp_path
):
    import asyncio
    import time
    from todometry.web.api_components import dashboard_runtime as runtime

    cache = _activity_cache_for_test(monkeypatch, tmp_path)
    source = runtime_component.sync_source(demo_mode=False)
    state = runtime.DashboardState(
        snapshot=runtime.DashboardSnapshot(
            _single_event_df(),
            last_refresh_s=time.time(),
            source_key=source.identity,
            activity_cache_signature=runtime._activity_cache_signature(cache),
        )
    )
    event = make_event("new-event")
    cache.activity.save({event})
    attempts = []

    def refresh(state, *, source, result):
        assert result is not None and result.events == {event}
        attempts.append(source.demo_mode)
        state.snapshot.activity_cache_signature = runtime._activity_cache_signature(
            cache
        )

    monkeypatch.setattr(
        runtime_component,
        "_sync_activity",
        lambda _source: data_sync.SyncResult({event}, set()),
    )
    monkeypatch.setattr(runtime, "_refresh_state_sync", refresh)

    async def run():
        await runtime.ensure_state(state, refresh=False, source=source)
        task = state.sync.task
        assert task is not None
        await task
        assert attempts == [False]

    asyncio.run(run())


@pytest.mark.parametrize(
    "history_succeeds, contexts, expected_backfills",
    [
        (True, [("token-a", "cache-a", False)] * 2, 1),
        (False, [("token-a", "cache-a", False)] * 2, 2),
        (True, [("token-a", "cache-a", False), ("token-b", "cache-a", False)], 2),
        (True, [("token-a", "cache-a", False), ("token-a", "cache-b", False)], 2),
        (True, [("token-a", "cache-a", False), ("token-a", "cache-a", True)], 2),
    ],
)
def test_history_backfill_attempt_is_retained_for_the_same_scope(
    monkeypatch, tmp_path, history_succeeds, contexts, expected_backfills
):
    _activity_cache_for_test(monkeypatch, tmp_path)
    state = dashboard_runtime_component.DashboardState()

    class ProjectFetchFails:
        def __init__(self, _path, *, api_key):
            pass

        def fetch_projects_incrementally(self, _previous, _changed):
            raise ConnectionError("projects unavailable after history sync")

    monkeypatch.setattr(dashboard_runtime_component, "Database", ProjectFetchFails)
    monkeypatch.setattr(
        dashboard_runtime_component, "needs_activity_history", lambda _: True
    )
    backfills = []

    def backfill(_db, **_kwargs):
        backfills.append(True)
        if not history_succeeds:
            raise ConnectionError("history unavailable")
        return data_sync.SyncResult(set(), set())

    monkeypatch.setattr(
        dashboard_runtime_component, "backfill_activity_history", backfill
    )
    for current_token, cache_name, demo_mode in contexts:
        monkeypatch.setenv("API_KEY", current_token)
        monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path / cache_name))
        source = runtime_component.sync_source(demo_mode=demo_mode)
        with pytest.raises(ConnectionError, match="projects unavailable"):
            dashboard_runtime_component._refresh_state_sync(
                state, source=source, result=data_sync.SyncResult(set(), set())
            )
    assert len(backfills) == expected_backfills
