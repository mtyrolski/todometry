"""Exercise the lifetimes of in-memory state behind the web routes."""

from threading import Event

from fastapi.testclient import TestClient

from todometry.core.env import EnvVar
from todometry.core.utils import Cache
from todometry.features.data_sync import SyncResult
from todometry.web.api import create_app
from todometry.web.api_components import runtime


def test_web_runtime_state_is_isolated_per_app_lifespan(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(str(EnvVar.DATA_DIR), str(tmp_path))
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path / "cache"))
    monkeypatch.setenv(str(EnvVar.CONFIG_DIR), str(tmp_path / "config"))
    monkeypatch.setenv(str(EnvVar.PERSONAL_DIR), str(tmp_path / "personal"))
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")
    monkeypatch.setenv("API_KEY", "test-token")
    Cache().activity.save(set())

    sync_started = Event()
    finish_sync = Event()

    def sync(_source: runtime.SyncSource) -> SyncResult:
        sync_started.set()
        assert finish_sync.wait(timeout=5)
        return SyncResult(set(), set())

    monkeypatch.setattr(runtime, "_sync_activity", sync)
    app, other = create_app(), create_app()
    saved_home = "/api/dashboard/home?cached_only=true"
    sync_status = "/api/activity/status?sync_only=true"
    with TestClient(app) as client, TestClient(other) as other_client:
        first_dashboard = app.state.dashboard
        first_sync = first_dashboard.sync
        assert first_dashboard.lock is not other.state.dashboard.lock

        try:
            assert client.get(saved_home).status_code == 200
            response = client.post("/api/activity/sync")
            assert response.status_code == 202
            assert sync_started.wait(timeout=5)

            status = response.json()["sync"]
            progress = client.get("/api/dashboard/progress").json()
            assert progress["active"] is True
            assert progress["sync"] == status
            assert client.get(sync_status).json()["sync"] == status
            assert other_client.get("/api/dashboard/progress").json()["active"] is False
            assert other_client.get(sync_status).json()["sync"]["state"] == "idle"
            assert other.state.dashboard.snapshot is None
            assert other.state.dashboard.sync.task is None
            source = runtime.sync_source()
            sync_task = first_sync.task
            assert sync_task is not None
        finally:
            finish_sync.set()

    assert sync_task.done() and not sync_task.cancelled()
    assert first_sync.status_for(source)["state"] == "succeeded"

    with TestClient(app) as restarted:
        assert app.state.dashboard is not first_dashboard
        assert app.state.dashboard.sync is not first_sync
        assert app.state.dashboard.snapshot is None
        assert app.state.dashboard.sync.task is None
        assert restarted.get("/api/dashboard/progress").json()["active"] is False
        assert restarted.get(sync_status).json()["sync"]["state"] == "idle"
        assert restarted.get(saved_home).status_code == 200
