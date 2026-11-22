"""Tests for FastAPI admin endpoints."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import todometry.database.dataframe as dataframe_module
from tests.web_api_helpers import _single_event_df
from todometry.core.env import EnvVar
from todometry.core.utils import MaxRetriesExceeded
from todometry.web.api import create_app
from todometry.web.api_components import dashboard_runtime
from todometry.web.api_components.dashboard_runtime import DashboardSnapshot
from todometry.web.dashboard_payload import DEFAULT_URGENCY_SETTINGS
from todometry.web.routes import admin_settings

# pylint: disable=protected-access


def _write_yaml(path, *lines: str) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def test_admin_project_adjustments_returns_manual_and_automatic_groups(
    monkeypatch, tmp_path, api_client, project_factory
) -> None:
    monkeypatch.setenv(str(EnvVar.PERSONAL_DIR), str(tmp_path))
    (tmp_path / "map.py").write_text(
        dataframe_module.render_adjustments_file_content({"Old Root": "Academy"}),
        encoding="utf-8",
    )
    api_client.app.state.dashboard.snapshot = DashboardSnapshot(
        df_activity=pd.DataFrame(),
        db=SimpleNamespace(
            fetch_projects=lambda include_tasks: [
                project_factory(project_id="academy", name="Academy"),
                project_factory(project_id="inbox", name="Inbox"),
                project_factory(project_id="skynet", name="skynet"),
            ],
            fetch_archived_projects=lambda: [
                project_factory(project_id="old-root", name="Old Root", is_archived=True),
                project_factory(
                    project_id="old-child",
                    name="Old Child",
                    parent_id="old-root",
                    is_archived=True,
                ),
            ],
        ),
    )

    response = api_client.get("/api/admin/project_adjustments")

    assert response.status_code == 200
    payload = response.json()
    assert payload["selectedFile"] == "map.py"
    assert payload["remappableActiveRootProjects"] == ["Inbox"]
    assert payload["sourceProjects"] == ["Inbox", "Old Child", "Old Root"]
    assert payload["mappings"] == {"Old Root": "Academy"}
    assert payload["automaticMappings"] == {"Old Child": "Academy"}


def test_admin_project_adjustments_rejects_path_traversal(
    monkeypatch, tmp_path, api_client
) -> None:
    monkeypatch.setenv("TODOIST_PERSONAL_DIR", str(tmp_path / "personal"))
    client = api_client
    invalid = client.get(
        "/api/admin/project_adjustments", params={"file": "../evil.py"}
    )
    assert invalid.status_code == 400
    assert "path separators" in invalid.json()["detail"]

    saved = client.put(
        "/api/admin/project_adjustments",
        params={"file": "../evil.py"},
        json={"mappings": {}},
    )
    assert saved.status_code == 400
    assert "path separators" in saved.json()["detail"]


def test_admin_save_project_adjustments_roundtrips_safe_literals(
    monkeypatch, tmp_path, api_client, project_factory
) -> None:
    personal_dir = tmp_path / "personal"
    monkeypatch.setenv("TODOIST_PERSONAL_DIR", str(personal_dir))
    monkeypatch.setattr(
        admin_settings,
        "_load_projects_for_adjustments_sync",
        lambda cached_database, refresh: [
            project_factory(project_id="academy", name="Academy / North Wing"),
            project_factory(
                project_id="research", name='Archived "Research"', is_archived=True
            ),
            project_factory(project_id="parent", name='Parent "One"', is_archived=True),
        ],
    )
    response = api_client.put(
        "/api/admin/project_adjustments",
        params={"file": "adj_private.py", "refresh": "false"},
        json={
            "mappings": {'Archived "Research"': "Academy / North Wing"},
            "archivedParents": ['Parent "One"'],
        },
    )
    assert response.status_code == 200
    assert response.json()["file"] == "adj_private.py"

    saved = (personal_dir / "adj_private.py").read_text(encoding="utf-8")
    assert "link_adjustements =" in saved
    assert "archived_parent_projects =" in saved

    saved_rules = dataframe_module.load_adjustments_file(
        personal_dir / "adj_private.py"
    )
    assert saved_rules.mappings == {'Archived "Research"': "Academy / North Wing"}
    assert saved_rules.archived_parents == ['Parent "One"']


def test_guided_project_mapping_roundtrips_project_ids_and_preserves_settings(
    monkeypatch, tmp_path, api_client, project_factory
) -> None:
    monkeypatch.setenv("TODOIST_PERSONAL_DIR", str(tmp_path))
    path = tmp_path / "projects.py"
    previous = {"missing": {"targetId": "older", "targetName": "Former group"}}
    previous_names = {"Archive": "Work", "Former project": "Archive"}
    previous_parents = ["Former root", "Kept"]
    path.write_text(
        dataframe_module.render_adjustments_file_content(
            previous_names, previous_parents, previous
        ),
        encoding="utf-8",
    )
    active = [
        project_factory(project_id="work", name="Work"),
        project_factory(project_id="child", name="Current", parent_id="work"),
    ]
    archived = [
        project_factory(project_id="archive-a", name="Archive", is_archived=True),
        project_factory(project_id="archive-b", name="Archive", is_archived=True),
        project_factory(project_id="kept", name="Kept", is_archived=True),
    ]
    api_client.app.state.dashboard.snapshot = DashboardSnapshot(
        df_activity=pd.DataFrame(),
        db=SimpleNamespace(
            fetch_projects=lambda include_tasks: active,
            fetch_archived_projects=lambda: archived,
        ),
    )
    endpoint = "/api/admin/project_adjustments?file=projects.py"
    loaded = api_client.get(endpoint)
    assert loaded.status_code == 200
    current = loaded.json()
    by_id = {project["id"]: project for project in current["projects"]}
    assert set(by_id) == {"work", "child", "archive-a", "archive-b", "kept"}
    assert by_id["child"]["path"] == "Work / Current"
    assert by_id["archive-a"]["archived"] and by_id["archive-b"]["archived"]
    assert current["archivedParentProjects"] == previous_parents
    assert current["mappings"] == previous_names
    assert current["projectMappings"] == previous

    archive = {"targetId": "archive-b", "targetName": "Archive"}
    wanted = {**previous, "archive-b": archive, "child": archive}
    saved = api_client.put(endpoint, json={"projectMappings": wanted})
    assert saved.status_code == 200
    assert saved.json()["projectMappings"] == wanted
    assert dataframe_module.load_adjustments_file(path) == (
        previous_names,
        previous_parents,
        wanted,
    )

    bad = {"targetId": "missing-target", "targetName": "Gone"}
    original = path.read_bytes()
    assert (
        api_client.put(endpoint, json={"projectMappings": {"child": bad}}).status_code
        == 400
    )
    assert path.read_bytes() == original

    def fail_replace(source: Path, target: Path) -> Path:
        assert target == path
        assert source.parent == path.parent
        assert source.read_bytes() != original
        raise OSError("synthetic replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        failed = api_client.put(
            endpoint,
            json={
                "projectMappings": {"child": {"targetId": "work", "targetName": "Work"}}
            },
        )
    assert failed.status_code == 503
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    assert api_client.get(endpoint).json()["projectMappings"] == wanted

    updated_names = {**previous_names, "Archive": "Kept"}
    named_save = api_client.put(
        endpoint, json={"mappings": updated_names, "archivedParents": previous_parents}
    )
    assert named_save.status_code == 200
    assert named_save.json()["projectMappings"] == wanted
    assert dataframe_module.load_adjustments_file(path) == (
        updated_names,
        previous_parents,
        wanted,
    )

    deleted = api_client.put(endpoint, json={"projectMappings": {"child": None}})
    assert deleted.status_code == 200
    assert api_client.get(endpoint).json()["projectMappings"] == {
        **previous,
        "archive-b": archive,
    }


def test_admin_save_project_adjustments_succeeds_when_refresh_fails(
    monkeypatch, tmp_path, api_client, project_factory
) -> None:
    personal_dir = tmp_path / "personal"
    monkeypatch.setenv("TODOIST_PERSONAL_DIR", str(personal_dir))
    monkeypatch.setattr(
        admin_settings,
        "_load_projects_for_adjustments_sync",
        lambda cached_database, refresh: [
            project_factory(project_id="academy", name="Academy"),
            project_factory(
                project_id="archived", name="Archived Research", is_archived=True
            ),
        ],
    )

    async def _boom(state, *, refresh: bool) -> None:
        _ = refresh
        raise MaxRetriesExceeded("Failed to execute list labels after 3 retry attempts")

    monkeypatch.setattr(dashboard_runtime, "ensure_state", _boom)

    response = api_client.put(
        "/api/admin/project_adjustments",
        params={"file": "adj_private.py", "refresh": "true"},
        json={"mappings": {"Archived Research": "Academy"}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["saved"] is True
    assert (
        payload["warning"]
        == "Saved, but dashboard refresh failed (MaxRetriesExceeded)."
    )
    saved_rules = dataframe_module.load_adjustments_file(
        personal_dir / "adj_private.py"
    )
    assert saved_rules.mappings == {"Archived Research": "Academy"}
    assert saved_rules.archived_parents == []


@pytest.mark.parametrize(
    ("body", "expected_detail"),
    [
        (
            {"mappings": {"DeepMhcFlare": "deepflare"}},
            "Mapping sources must be archived projects",
        ),
        (
            {"mappings": {}, "archivedParents": ["DeepMhcFlare"]},
            "archivedParents must contain archived projects only",
        ),
    ],
)
def test_admin_save_project_adjustments_rejects_active_project_inputs(
    monkeypatch, tmp_path, body, expected_detail, api_client, project_factory
) -> None:
    personal_dir = tmp_path / "personal"
    monkeypatch.setenv("TODOIST_PERSONAL_DIR", str(personal_dir))
    monkeypatch.setattr(
        admin_settings,
        "_load_projects_for_adjustments_sync",
        lambda cached_database, refresh: [
            project_factory(project_id="academy", name="Academy"),
            project_factory(project_id="deepflare", name="deepflare", is_archived=True),
            project_factory(
                project_id="child", name="DeepMhcFlare", parent_id="academy"
            ),
        ],
    )

    response = api_client.put(
        "/api/admin/project_adjustments",
        params={"file": "adj_private.py", "refresh": "false"},
        json=body,
    )

    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]


def test_admin_dashboard_settings_roundtrip(tmp_path, api_client) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "dashboard.yaml").write_text(
        "urgency:\n  enabled: true\n  warn_priority_thresholds: [4, 3]\n",
        encoding="utf-8",
    )
    api_client.app.state.dashboard_config_path = config_dir / "dashboard.yaml"

    client = api_client
    res = client.get("/api/admin/dashboard/settings")
    assert res.status_code == 200
    payload = res.json()
    assert payload["settings"]["enabled"] is True
    assert payload["editTargets"][0]["icon"] == "wrench"
    assert payload["settings"]["fireLabels"] == [DEFAULT_URGENCY_SETTINGS["fire_label"]]

    update = client.put(
        "/api/admin/dashboard/settings",
        json={
            "enabled": False,
            "fireLabels": ["fire 🧯🚒", "hot"],
            "warnPriorityThresholds": [4],
            "warnPriorityMinCount": 2,
            "warnDueWithinDays": 2,
            "warnDueMinCount": 3,
            "warnDeadlineMinCount": 2,
            "badgeLabels": {"warn": "Check"},
            "plotEvents": [
                {"date": "2025-01-05", "label": "Kickoff", "color": "#00ffaa"}
            ],
        },
    )
    assert update.status_code == 200
    updated = update.json()
    assert updated["settings"]["enabled"] is False
    assert updated["settings"]["fireLabels"] == ["fire 🧯🚒", "hot"]
    assert updated["settings"]["warnPriorityThresholds"] == [4]
    assert updated["settings"]["warnPriorityMinCount"] == 2
    assert updated["settings"]["warnDueWithinDays"] == 2
    assert updated["settings"]["warnDueMinCount"] == 3
    assert updated["settings"]["warnDeadlineMinCount"] == 2
    assert updated["settings"]["badgeLabels"]["warn"] == "Check"
    assert updated["settings"]["plotEvents"] == [
        {"date": "2025-01-05", "label": "Kickoff", "color": "#00ffaa"}
    ]

    saved_text = (config_dir / "dashboard.yaml").read_text(encoding="utf-8")
    assert "fire_labels:" in saved_text
    assert "- hot" in saved_text
    assert "warn_priority_min_count: 2" in saved_text
    assert "warn_due_within_days: 2" in saved_text
    assert "warn_due_min_count: 3" in saved_text
    assert "warn_deadline_min_count: 2" in saved_text
    assert "enabled: false" in saved_text
    assert "plot_events:" in saved_text
    assert "label: Kickoff" in saved_text


def test_admin_dashboard_labels_returns_sorted_local_labels(
    monkeypatch, api_client
) -> None:
    class _FakeDatabase:
        def __init__(self, dotenv_path: str) -> None:
            _ = dotenv_path
            self._items = [
                {"name": "zeta", "color": "red"},
                {"name": "alpha", "color": "blue"},
            ]

        def fetch_label_colors(self) -> dict[str, str]:
            return {"alpha": "#0000ff", "zeta": "#ff0000"}

        def list_labels(self) -> list[dict[str, str]]:
            return list(self._items)

    monkeypatch.setattr(admin_settings, "Database", _FakeDatabase)

    res = api_client.get("/api/admin/dashboard/labels")
    assert res.status_code == 200
    payload = res.json()
    assert payload["labels"] == [
        {"name": "alpha", "color": "#0000ff"},
        {"name": DEFAULT_URGENCY_SETTINGS["fire_label"], "color": None},
        {"name": "zeta", "color": "#ff0000"},
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/automations",
        "/api/admin/observer",
        "/api/admin/multiplication",
        "/api/admin/stale_tasks",
        "/api/dashboard/executive_review",
    ],
)
def test_removed_runtime_features_have_no_routes(api_client, path: str) -> None:
    assert api_client.get(path).status_code == 404


def test_dashboard_settings_create_directory_and_update_overview(
    monkeypatch, tmp_path, api_client
):
    monkeypatch.setenv(str(EnvVar.CACHE_DIR), str(tmp_path / "cache"))
    monkeypatch.setenv(str(EnvVar.PERSONAL_DIR), str(tmp_path / "personal"))
    monkeypatch.setenv(str(EnvVar.DASHBOARD_DEMO), "0")
    path = tmp_path / "new-configs" / "dashboard.yaml"
    api_client.app.state.dashboard_config_path = path
    api_client.app.state.dashboard.snapshot = DashboardSnapshot(_single_event_df())
    overview = "/api/dashboard/home?cached_only=true"
    before = api_client.get(overview).json()["configurableItems"][0]["summary"]
    response = api_client.put(
        "/api/admin/dashboard/settings",
        json={
            "fireLabels": [],
            "warnPriorityThresholds": [],
            "dangerOnFireLabel": False,
            "warnOnPriority": False,
            "warnOnDue": False,
            "warnOnDeadline": False,
        },
    )
    assert response.status_code == 200
    after = api_client.get(overview).json()["configurableItems"][0]["summary"]
    assert after != before
    assert "Priority thresholds []" in after
    reread = api_client.get("/api/admin/dashboard/settings").json()["settings"]
    assert reread["fireLabels"] == []
    assert reread["warnPriorityThresholds"] == []
    assert reread["dangerOnFireLabel"] is False
    assert reread["warnOnPriority"] is False
    assert path.exists()


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "false"},
        {"warnOnPriority": 0},
        {"fireLabels": "fire"},
        {"warnPriorityThresholds": "43"},
        {"warnPriorityThresholds": [0, 5]},
        {"warnPriorityMinCount": 1.5},
        {"warnDueWithinDays": -1},
        {"warnDueMinCount": 0},
        {"warnDeadlineWithinDays": True},
        {"fireLabels": [None]},
        {"plotEvents": [{"date": "2026-02-30", "label": "Invalid"}]},
        {"plotEvents": [{"date": "2026-11-01", "label": "Launch", "color": "garbage"}]},
    ],
)
def test_invalid_settings_do_not_modify_saved_rules(tmp_path, api_client, patch):
    path = tmp_path / "dashboard.yaml"
    path.write_text("urgency:\n  enabled: true\n  warn_priority_thresholds: [4]\n")
    api_client.app.state.dashboard_config_path = path
    before = path.read_bytes()
    response = api_client.put("/api/admin/dashboard/settings", json=patch)
    assert response.status_code == 400
    assert path.read_bytes() == before
    assert response.json()["detail"]


def test_concurrent_partial_settings_updates_preserve_each_other(monkeypatch, tmp_path):
    path = tmp_path / "dashboard.yaml"
    path.write_text("urgency:\n  enabled: true\n  warn_priority_thresholds: [4]\n")
    monkeypatch.setenv("TODOIST_CONFIG_DIR", str(tmp_path))
    endpoint = "/api/admin/dashboard/settings"

    with (
        TestClient(create_app()) as first,
        TestClient(create_app()) as second,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        disable = workers.submit(first.put, endpoint, json={"enabled": False})
        threshold = workers.submit(
            second.put, endpoint, json={"warnPriorityThresholds": [2]}
        )
        assert disable.result(timeout=5).status_code == 200
        assert threshold.result(timeout=5).status_code == 200
        settings = first.get(endpoint).json()["settings"]
        assert settings["enabled"] is False
        assert settings["warnPriorityThresholds"] == [2]
