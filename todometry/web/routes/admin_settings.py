"""Admin settings and adjustment routes."""

import asyncio
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Body, HTTPException, Request
from loguru import logger
from omegaconf import DictConfig

from todometry.database.base import Database
from todometry.database.dataframe import (
    ProjectAdjustments,
    normalize_adjustment_filename,
    parse_project_id_mappings,
    project_lineage,
    resolve_project_hierarchy,
)
from todometry.web.api_components import dashboard_runtime
from todometry.web.api_components.configuration import (
    configuration_write_lock,
    read_yaml_config,
    save_yaml_config,
)
from todometry.web.api_components.runtime import REPO_ROOT, resolve_env_path, sync_source
from todometry.web.api_components.settings import dashboard_settings_payload
from todometry.web.dashboard_payload import DEFAULT_URGENCY_SETTINGS
from todometry.web.dashboard_settings import apply_settings_patch, parse_settings_patch
from todometry.web.services.project_adjustments import (
    _REMAPPABLE_ACTIVE_ROOT_PROJECTS,
    _automatic_mappings,
    _available_mapping_files,
    _load_mapping_file,
    _load_projects_for_adjustments_sync,
    _project_choices,
    _save_mapping_file,
)


router = APIRouter()


def _dashboard_settings_response(
    config: DictConfig, config_path: Path, *, saved: bool = False
) -> dict[str, Any]:
    settings = dashboard_settings_payload(
        config, dashboard_config_path=config_path, repo_root=REPO_ROOT
    )
    edit_targets = [
        {
            "key": key,
            "label": label,
            "icon": "wrench",
            "configPath": settings["configPath"],
            "anchor": "dashboard-settings",
        }
        for key, label in (
            ("urgency", "Urgency watch badge"),
            ("plot-events", "Plot event markers"),
        )
    ]
    payload = {"settings": settings, "editTargets": edit_targets}
    return {"saved": True, **payload} if saved else payload


@router.get("/api/admin/project_adjustments", tags=["admin"])
async def admin_project_adjustments(
    request: Request, file: str | None = None, refresh: bool = False
) -> dict[str, Any]:
    """Return mapping files, current mapping content, and project lists for building adjustments."""

    try:
        selected = normalize_adjustment_filename(file) if file else None
        files = _available_mapping_files()
        selected = selected or files[0]
        rules = _load_mapping_file(selected)
        mappings, archived_parents, project_mappings = rules
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    warning: str | None = None
    state: dashboard_runtime.DashboardState = request.app.state.dashboard
    snapshot = state.snapshot_for(sync_source())
    cached_database = snapshot.db if snapshot is not None else None
    try:
        projects = await asyncio.to_thread(
            _load_projects_for_adjustments_sync, cached_database, refresh
        )
    except Exception as exc:
        logger.warning("Failed loading project lists for adjustments: {}", exc)
        projects = []
        warning = f"Project list unavailable ({type(exc).__name__}). Showing saved mappings only."

    active_roots, archived_names = _project_choices(projects)
    remappable_roots = active_roots & _REMAPPABLE_ACTIVE_ROOT_PROJECTS
    sources = archived_names | remappable_roots
    by_id = {str(project.id): project for project in projects}
    hierarchy = resolve_project_hierarchy(by_id, rules)

    return {
        "files": files,
        "selectedFile": selected,
        "mappings": mappings,
        "automaticMappings": _automatic_mappings(by_id, rules, hierarchy),
        "projects": [
            {
                "id": project_id,
                "name": project.project_entry.name,
                "path": " / ".join(
                    by_id[ancestor].project_entry.name
                    for ancestor in reversed(
                        list(project_lineage(project_id, hierarchy.original_parents))
                    )
                ),
                "archived": project.is_archived,
            }
            for project_id, project in by_id.items()
        ],
        "projectMappings": project_mappings,
        "activeRootProjects": sorted(active_roots),
        "remappableActiveRootProjects": sorted(remappable_roots),
        "archivedParentProjects": sorted(archived_parents),
        "archivedProjects": sorted(archived_names),
        "sourceProjects": sorted(sources),
        "warning": warning,
    }


@router.put("/api/admin/project_adjustments", tags=["admin"])
async def admin_save_project_adjustments(
    request: Request,
    file: str,
    refresh: bool = False,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Save named rules or apply an individual-rule patch to the selected file."""

    state: dashboard_runtime.DashboardState = request.app.state.dashboard
    snapshot = state.snapshot_for(sync_source())
    cached_database = snapshot.db if snapshot is not None else None
    refresh_warning: str | None = None
    id_patch = payload.get("projectMappings", {})
    if "projectMappings" in payload:
        mappings = payload.get("mappings")
        archived_parents = payload.get("archivedParents")
    elif isinstance(payload.get("mappings"), dict) or "archivedParents" in payload:
        mappings = cast(dict[str, str], payload.get("mappings") or {})
        archived_parents = cast(list[str], payload.get("archivedParents") or [])
    else:
        mappings = payload
        archived_parents = []

    if mappings is not None and (
        not isinstance(mappings, dict)
        or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in mappings.items()
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="Body must be a JSON object of string->string mappings",
        )
    if archived_parents is not None and (
        not isinstance(archived_parents, list)
        or not all(isinstance(name, str) for name in archived_parents)
    ):
        raise HTTPException(
            status_code=400, detail="archivedParents must be a list of strings"
        )

    try:
        safe_filename = normalize_adjustment_filename(file)
        if not isinstance(id_patch, dict) or any(
            not isinstance(key, str) or not key.strip() for key in id_patch
        ):
            raise TypeError("projectMappings must be an object keyed by project ID.")
        id_mappings = parse_project_id_mappings(
            {key: rule for key, rule in id_patch.items() if rule is not None}
        )
        projects = []
        if mappings or archived_parents or id_mappings:
            try:
                projects = await asyncio.to_thread(
                    _load_projects_for_adjustments_sync, cached_database, False
                )
            except Exception as exc:
                logger.warning("Could not load projects for mapping validation: {}", exc)
        active_roots, archived_names = _project_choices(projects)
        allowed_sources = archived_names | (
            active_roots & _REMAPPABLE_ACTIVE_ROOT_PROJECTS
        )
        allowed_targets = active_roots | archived_names
        known_ids = {str(project.id) for project in projects}
        with configuration_write_lock:
            saved = _load_mapping_file(safe_filename)
            if any(
                saved.project_mappings.get(source) != rule
                and (source not in known_ids or rule["targetId"] not in known_ids)
                for source, rule in id_mappings.items()
            ):
                raise ValueError(
                    "A selected project is missing from the project list. Refresh the list and try again."
                )
            changed_mappings = {
                source: target
                for source, target in (mappings or {}).items()
                if saved.mappings.get(source) != target
            }
            invalid_sources = sorted(changed_mappings.keys() - allowed_sources)
            invalid_targets = sorted(set(changed_mappings.values()) - allowed_targets)
            invalid_parents = sorted(
                set(archived_parents or []) - set(saved.archived_parents) - archived_names
            )
            if invalid_sources:
                raise ValueError(
                    "Mapping sources must be archived projects or explicitly "
                    f"remappable active roots: {', '.join(invalid_sources[:10])}"
                )
            if invalid_targets:
                raise ValueError(
                    "Mapping targets must be active roots or archived projects: "
                    f"{', '.join(invalid_targets[:10])}"
                )
            if invalid_parents:
                raise ValueError(
                    "archivedParents must contain archived projects only: "
                    f"{', '.join(invalid_parents[:10])}"
                )
            merged_ids = {**saved.project_mappings, **id_mappings}
            for source in id_patch.keys() - id_mappings.keys():
                merged_ids.pop(source, None)
            saved = ProjectAdjustments(
                saved.mappings if mappings is None else mappings,
                saved.archived_parents
                if archived_parents is None
                else archived_parents,
                merged_ids,
            )
            _save_mapping_file(safe_filename, saved)
        if refresh:
            try:
                await dashboard_runtime.ensure_state(state, refresh=True)
            except Exception as exc:  # pragma: no cover - network safety
                logger.warning(
                    f"Failed refreshing dashboard state after saving adjustments: {exc}"
                )
                refresh_warning = (
                    f"Saved, but dashboard refresh failed ({type(exc).__name__})."
                )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.warning("Could not save project adjustments: {}", exc)
        raise HTTPException(
            status_code=503, detail="Could not save project adjustments."
        ) from exc
    return {
        "saved": True,
        "file": safe_filename,
        "count": len(saved.mappings),
        "archivedParents": len(saved.archived_parents),
        "projectMappings": saved.project_mappings,
        "warning": refresh_warning,
    }


@router.get("/api/admin/dashboard/settings", tags=["admin"])
async def admin_dashboard_settings(request: Request) -> dict[str, Any]:
    config_path: Path = request.app.state.dashboard_config_path
    config = read_yaml_config(config_path, required=False)
    return _dashboard_settings_response(config, config_path)


@router.get("/api/admin/dashboard/labels", tags=["admin"])
async def admin_dashboard_labels() -> dict[str, Any]:
    dbio = Database(str(resolve_env_path()))
    label_colors = dbio.fetch_label_colors()
    labels: list[dict[str, Any]] = []
    for item in dbio.list_labels():
        name = item["name"].strip()
        labels.append(
            {
                "name": name,
                "color": label_colors.get(name),
            }
        )
    if not any(
        item["name"] == DEFAULT_URGENCY_SETTINGS["fire_label"] for item in labels
    ):
        labels.append(
            {
                "name": DEFAULT_URGENCY_SETTINGS["fire_label"],
                "color": label_colors.get(DEFAULT_URGENCY_SETTINGS["fire_label"]),
            }
        )
    labels.sort(key=lambda item: item["name"].lower())
    return {"labels": labels}


@router.put("/api/admin/dashboard/settings", tags=["admin"])
async def admin_update_dashboard_settings(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    patch = parse_settings_patch(payload)
    config_path: Path = request.app.state.dashboard_config_path
    with configuration_write_lock:
        config = read_yaml_config(config_path, required=False)
        apply_settings_patch(config, patch)
        save_yaml_config(config_path, config)
    return _dashboard_settings_response(config, config_path, saved=True)
