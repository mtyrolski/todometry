"""Activity sync, runtime configuration, and log routes."""

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response, status
from loguru import logger

from todometry.core.env import EnvVar
from todometry.core.utils import Cache, LocalStorageError
from todometry.features.data_sync import activity_history_status
from todometry.web.api_components import logs, runtime
from todometry.web.api_components.dashboard_runtime import DashboardState

router = APIRouter()


def _resolve_timezone_status() -> dict[str, Any]:
    env_path = runtime.resolve_env_path()
    system_timezone = runtime.detect_system_timezone()
    override = runtime.resolve_timezone_override(env_path)

    payload: dict[str, Any] = {
        "configured": False,
        "timezone": system_timezone,
        "source": "system",
        "override": None,
        "overrideValid": True,
        "system": system_timezone,
        "envPath": runtime.safe_display_path(env_path, root=runtime.REPO_ROOT),
    }
    if not override:
        return payload

    payload["override"] = override
    if runtime.is_valid_timezone_name(override):
        payload["configured"] = True
        payload["timezone"] = override
        payload["source"] = "env"
        return payload

    payload["overrideValid"] = False
    payload["invalidOverride"] = override
    return payload


def _admin_api_token_from_env_path() -> tuple[str, Path]:
    env_path = runtime.resolve_env_path()
    return runtime.resolve_api_key(env_path), env_path


@router.get("/api/activity/status", tags=["activity"])
async def activity_status(
    request: Request,
    response: Response,
    month: Annotated[str | None, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] = None,
    sync_only: bool = False,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    source = runtime.sync_source()
    state: DashboardState = request.app.state.dashboard
    payload = {"demoMode": source.demo_mode, "sync": state.sync.status_for(source)}
    if sync_only:
        return payload

    if month is not None and not 1900 <= int(month[:4]) <= 9998:
        raise HTTPException(
            status_code=422, detail="Choose a month between 1900 and 9998."
        )
    first_day = date.fromisoformat(f"{month}-01") if month else None
    try:
        zone = await asyncio.to_thread(runtime.resolve_calendar_timezone)
        history = await asyncio.to_thread(
            activity_history_status,
            first_day,
            timezone_name=zone.key,
            demo_mode=source.demo_mode,
            cache=Cache(source.cache_dir) if not source.demo_mode else None,
        )
    except (LocalStorageError, OSError) as exc:
        logger.warning("Could not read activity history: {}", exc)
        raise HTTPException(
            status_code=503, detail="Could not read saved activity history."
        ) from exc
    return {**history, **payload}


@router.post(
    "/api/activity/sync", status_code=status.HTTP_202_ACCEPTED, tags=["activity"]
)
async def start_activity_sync(request: Request) -> dict[str, Any]:
    source = runtime.sync_source()
    if source.demo_mode:
        raise HTTPException(
            status_code=409, detail="Activity sync is unavailable in demo mode."
        )
    state: DashboardState = request.app.state.dashboard
    try:
        state.sync.start(source)
    except runtime.SyncBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"sync": state.sync.status_for(source)}


@router.get("/api/admin/api_token", tags=["admin"])
async def admin_api_token_status() -> dict[str, Any]:
    token, env_path = _admin_api_token_from_env_path()
    return {
        "configured": bool(token),
        "masked": runtime.mask_api_key(token),
        "envPath": runtime.safe_display_path(env_path, root=runtime.REPO_ROOT),
    }
@router.post("/api/admin/api_token", tags=["admin"])
async def admin_set_api_token(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    token = runtime.normalize_api_key(payload.get("token"))
    if not token:
        raise HTTPException(status_code=400, detail="API token is required.")
    if not runtime.looks_like_api_key(token):
        raise HTTPException(
            status_code=400,
            detail="API token looks invalid. Use the Todoist API token (no spaces, at least 20 characters).",
        )
    validate = payload.get("validate", True)
    labels_count = None
    if validate:
        ok, detail, labels_count = runtime.validate_api_token(token)
        if not ok:
            status = 400 if detail and ("403" in detail or "401" in detail) else 502
            raise HTTPException(
                status_code=status, detail=f"API token validation failed: {detail}"
            )
    env_path = runtime.resolve_env_path()
    runtime.write_runtime_env_value(env_path, "API_KEY", token)
    return {
        "configured": True,
        "masked": runtime.mask_api_key(token),
        "envPath": runtime.safe_display_path(env_path, root=runtime.REPO_ROOT),
        "validated": bool(validate),
        "labelsCount": labels_count,
    }


@router.post("/api/admin/api_token/validate", tags=["admin"])
async def admin_validate_api_token(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    resolved_token, _ = _admin_api_token_from_env_path()
    token = runtime.normalize_api_key(payload.get("token")) or resolved_token
    if not token:
        return {"configured": False, "valid": False, "detail": "API token missing."}
    ok, detail, labels_count = runtime.validate_api_token(token)
    return {
        "configured": True,
        "valid": ok,
        "detail": detail or "",
        "masked": runtime.mask_api_key(token),
        "labelsCount": labels_count,
    }


@router.delete("/api/admin/api_token", tags=["admin"])
async def admin_clear_api_token() -> dict[str, Any]:
    env_path = runtime.resolve_env_path()
    runtime.write_runtime_env_value(env_path, "API_KEY", None)
    return {
        "configured": False,
        "masked": "",
        "envPath": runtime.safe_display_path(env_path, root=runtime.REPO_ROOT),
    }


@router.get("/api/admin/timezone", tags=["admin"])
async def admin_timezone_status() -> dict[str, Any]:
    return _resolve_timezone_status()


@router.post("/api/admin/timezone", tags=["admin"])
async def admin_set_timezone(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    timezone_name = runtime.normalize_timezone(payload.get("timezone"))
    if not timezone_name:
        raise HTTPException(status_code=400, detail="Timezone is required.")
    if not runtime.is_valid_timezone_name(timezone_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid timezone. Use a valid IANA timezone name "
                "(example: Europe/Warsaw)."
            ),
        )
    runtime.write_runtime_env_value(
        runtime.resolve_env_path(), str(EnvVar.TIMEZONE), timezone_name
    )
    return _resolve_timezone_status()


@router.delete("/api/admin/timezone", tags=["admin"])
async def admin_clear_timezone() -> dict[str, Any]:
    runtime.write_runtime_env_value(
        runtime.resolve_env_path(), str(EnvVar.TIMEZONE), None
    )
    return _resolve_timezone_status()


@router.get("/api/runtime/logs", tags=["runtime"])
async def runtime_logs() -> dict[str, Any]:
    return {
        "inspectOnly": True,
        "sources": logs.runtime_log_sources(
            data_dir=runtime.resolve_data_dir(), cache_dir=Path(Cache().path)
        ),
    }


@router.get("/api/runtime/logs/read", tags=["runtime"])
async def runtime_read_log(
    source: str | None = None,
    path: str | None = None,
    tail_lines: int = 120,
    page: int = 1,
) -> dict[str, Any]:
    spec, abs_path = logs.resolve_runtime_log_request(
        data_dir=runtime.resolve_data_dir(),
        cache_dir=Path(Cache().path),
        source=source,
        path=path,
    )
    if not abs_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Runtime log is not available yet: {spec.label}",
        )

    stat = abs_path.stat()
    payload = logs.read_log_file(abs_path, tail_lines=tail_lines, page=page)
    return {
        "id": spec.key,
        "source": spec.key,
        "label": spec.label,
        "kind": spec.category,
        "category": spec.category,
        "description": spec.description,
        "path": logs.display_log_path(
            abs_path, data_dir=runtime.resolve_data_dir(), cache_dir=Path(Cache().path)
        ),
        "available": True,
        "inspectOnly": True,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        **payload,
    }
