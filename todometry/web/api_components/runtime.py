import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values, set_key, unset_key
from loguru import logger

from todometry.api.client import RequestSpec, TimeoutSettings, TodoistAPIClient
from todometry.api.endpoints import TodoistEndpoints
from todometry.core.env import EnvVar
from todometry.core.runtime_env import resolve_runtime_env_path, runtime_env_lock
from todometry.core.utils import (
    Cache,
    LocalStorageError,
    resolve_cache_dir,
    set_tqdm_progress_callback,
)
from todometry.database.db_activity import DatabaseActivity
from todometry.features.data_sync import SyncResult, sync_recent_activity


REPO_ROOT = Path(__file__).resolve().parents[3]

_API_KEY_PLACEHOLDERS = {
    "put your api here",
    "put your api key here",
    "your todoist api key",
}
_API_KEY_MIN_LENGTH = 20
_API_KEY_HEX_RE = re.compile(r"^[a-fA-F0-9]{32,64}$")
_API_KEY_FALLBACK_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


def resolve_data_dir() -> Path:
    override = os.getenv(str(EnvVar.DATA_DIR)) or os.getenv(str(EnvVar.CACHE_DIR))
    if override:
        return Path(override).expanduser().resolve()
    return REPO_ROOT


def resolve_env_path() -> Path:
    return resolve_runtime_env_path(repo_root=REPO_ROOT)


def normalize_api_key(raw: str | None) -> str:
    if not raw:
        return ""
    value = str(raw).strip().strip("'\"")
    if not value:
        return ""
    if value.strip().lower() in _API_KEY_PLACEHOLDERS:
        return ""
    return value


def looks_like_api_key(value: str) -> bool:
    if len(value) < _API_KEY_MIN_LENGTH:
        return False
    if any(char.isspace() for char in value):
        return False
    if _API_KEY_HEX_RE.fullmatch(value):
        return True
    return _API_KEY_FALLBACK_RE.fullmatch(value) is not None


def _read_runtime_env_value(
    key: str, env_path: Path, normalize: Callable[[str | None], str]
) -> str:
    with runtime_env_lock:
        value = normalize(os.getenv(key))
        if not value and env_path.exists():
            value = normalize(dotenv_values(env_path).get(key))
            if value:
                os.environ[key] = value
        return value


def resolve_api_key(env_path: Path | None = None) -> str:
    return _read_runtime_env_value(
        "API_KEY", env_path or resolve_env_path(), normalize_api_key
    )


def write_runtime_env_value(env_path: Path, key: str, value: str | None) -> None:
    with runtime_env_lock:
        if value is None:
            if env_path.exists():
                unset_key(str(env_path), key)
            os.environ.pop(key, None)
        else:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            set_key(str(env_path), key, value)
            os.environ[key] = value


def is_demo_mode() -> bool:
    value = os.getenv(str(EnvVar.DASHBOARD_DEMO), "").strip().lower()
    return value in {"1", "true", "yes", "on"}


SyncSourceKey = tuple[Path, str, bool, bytes]


@dataclass(frozen=True, slots=True)
class SyncSource:
    env_path: Path
    cache_dir: str
    demo_mode: bool
    api_key: str = field(repr=False)

    @property
    def identity(self) -> SyncSourceKey:
        return (
            self.env_path,
            self.cache_dir,
            self.demo_mode,
            sha256(self.api_key.encode()).digest(),
        )


def sync_source(*, demo_mode: bool | None = None) -> SyncSource:
    env_path = resolve_env_path()
    return SyncSource(
        env_path,
        resolve_cache_dir(),
        is_demo_mode() if demo_mode is None else demo_mode,
        resolve_api_key(env_path),
    )


class ActivitySyncStatus(TypedDict):
    state: Literal["idle", "running", "succeeded", "failed"]
    startedAt: str | None
    newEvents: int | None
    error: str | None
    dashboardError: NotRequired[str]


class SyncBusy(Exception):
    """The runtime is still syncing an earlier source configuration."""


ProgressLaneStatus = Literal["queued", "active", "done"]
_ProgressValue = tuple[str, str | None, int, int, ProgressLaneStatus | None]
_DashboardRefresh = Callable[[SyncSource, SyncResult | None], Awaitable[str | None]]


class _ProgressLane(TypedDict):
    id: str
    label: str
    detail: str
    current: int
    total: int
    unit: str | None
    status: ProgressLaneStatus
    updatedAt: str


@dataclass(slots=True)
class _ProgressState:
    stage: str | None = None
    step: int = 0
    total_steps: int = 0
    updated_at: str | None = None
    detail: str | None = None
    sub_current: int | None = None
    sub_total: int | None = None
    lanes: dict[str, _ProgressLane] = field(default_factory=dict)


_ADAPTIVE_ACTIVITY_STAGES = {
    "Backfilling activity history",
    "Fetching activity history",
    "Fetching archived project activity",
}
_TQDM_STEPS = {
    "Checking Todoist updates": 1,
    "Querying project data": 1,
    "Fetching initial project data": 1,
    "Querying changed project data": 1,
    "Reconciling project data": 1,
    "Checking activity cache": 1,
    "Fetching recent activity": 1,
    "Querying activity data": 1,
    **dict.fromkeys(_ADAPTIVE_ACTIVITY_STAGES, 1),
    "Resolving project hierarchy": 2,
    "Building project hierarchy": 2,
}
_IDLE_SYNC: ActivitySyncStatus = {
    "state": "idle",
    "startedAt": None,
    "newEvents": None,
    "error": None,
}


class ActivitySyncJob:
    """One tracked recent scan, with an optional dashboard continuation."""

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self._source_key: SyncSourceKey | None = None
        self._status: ActivitySyncStatus = _IDLE_SYNC.copy()
        self._refresh: _DashboardRefresh | None = None
        self._last_full_attempt_s = 0.0
        self._dashboard_error: str | None = None
        self._progress = _ProgressState()
        self._progress_lock = Lock()

    def last_full_attempt_s(self, source: SyncSource) -> float:
        return self._last_full_attempt_s if self._source_key == source.identity else 0.0

    def status_for(self, source: SyncSource) -> ActivitySyncStatus:
        if self._source_key != source.identity:
            return _IDLE_SYNC.copy()
        status = self._status.copy()
        if self._dashboard_error is not None:
            status["dashboardError"] = self._dashboard_error
        return status

    def start(
        self, source: SyncSource, *, refresh: _DashboardRefresh | None = None
    ) -> asyncio.Task[None]:
        # Start and upgrade run without yielding on the application's event loop.
        task = self.task
        if task is not None and task.done():
            task = None
        source_key = source.identity
        if task is None:
            if self._source_key != source_key:
                self._last_full_attempt_s = 0.0
                self._dashboard_error = None
            self._source_key = source_key
            self._refresh = None
            started_at = datetime.now(timezone.utc).isoformat()
            self._status = {**_IDLE_SYNC, "state": "running", "startedAt": started_at}
            with self._progress_lock:
                self._progress = _ProgressState(updated_at=started_at)
        elif self._source_key != source_key:
            raise SyncBusy("A previous Todoist connection is still syncing.")
        if refresh is not None and self._refresh is None:
            self._refresh = refresh
            self._last_full_attempt_s = time.time()
            self._dashboard_error = None
        if task is None:
            self.task = task = asyncio.create_task(self._run(source))
        return task

    async def _run(self, source: SyncSource) -> None:
        set_tqdm_progress_callback(self._build_tqdm_progress_callback())
        result: SyncResult | None = None
        error: str | None = None
        try:
            if source.api_key:
                self.set_progress("Fetching recent activity", step=1)
                try:
                    result = await asyncio.to_thread(_sync_activity, source)
                except Exception as exc:
                    _log_sync_failure("Activity sync failed", source, exc)
                    error = (
                        "Could not access saved activity. Check the local data directory and try again."
                        if isinstance(
                            exc, (LocalStorageError, PermissionError, FileNotFoundError)
                        )
                        else "Could not sync with Todoist. Try again."
                    )
            elif not source.demo_mode:
                error = "Set your Todoist API token in Control Panel and try again."

            refresh = self._refresh
            if refresh is not None:
                try:
                    self._dashboard_error = await refresh(source, result)
                except Exception as exc:
                    _log_sync_failure("Dashboard refresh failed", source, exc)
                    self._dashboard_error = "Could not update the dashboard."
        except asyncio.CancelledError:
            if result is None and error is None:
                error = "Activity sync was interrupted. Try again."
            if self._refresh is not None:
                self._dashboard_error = "Dashboard refresh was interrupted."
            raise
        finally:
            state: Literal["failed", "idle", "succeeded"] = "idle"
            if error:
                state = "failed"
            elif result is not None:
                state = "succeeded"
            self._status = {
                **self._status,
                "state": state,
                "error": error,
                "newEvents": len(result.new_events)
                if result is not None and not source.demo_mode
                else None,
            }
            self._refresh = None
            with self._progress_lock:
                self._progress = _ProgressState(
                    updated_at=datetime.now(timezone.utc).isoformat()
                )

    def progress_snapshot(self, source: SyncSource) -> dict[str, Any]:
        status = self.status_for(source)
        with self._progress_lock:
            progress = (
                self._progress
                if self._source_key == source.identity
                else _ProgressState()
            )
            return {
                "active": status["state"] == "running",
                "stage": progress.stage,
                "step": progress.step,
                "totalSteps": progress.total_steps,
                "startedAt": status["startedAt"],
                "updatedAt": progress.updated_at,
                "detail": progress.detail,
                "subCurrent": progress.sub_current,
                "subTotal": progress.sub_total,
                "lanes": sorted(
                    progress.lanes.values(), key=lambda lane: lane["label"].casefold()
                ),
                "error": status.get("dashboardError") or status["error"],
                "sync": status,
            }

    def set_progress(
        self,
        stage: str,
        *,
        step: int | None = None,
        total_steps: int = 3,
        detail: str | None = None,
        sub_current: int | None = None,
        sub_total: int | None = None,
        lane_id: str | None = None,
        lane_label: str | None = None,
        unit: str | None = None,
        lane_status: ProgressLaneStatus | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._progress_lock:
            progress = self._progress
            if lane_id is None and progress.stage != stage:
                progress.lanes.clear()
            progress.stage = stage
            progress.step = step if step is not None else progress.step or 1
            progress.total_steps = total_steps
            progress.updated_at = now
            if lane_id is None:
                progress.detail = detail
                progress.sub_current = sub_current
                progress.sub_total = sub_total
                return

            current = max(sub_current or 0, 0)
            total = max(sub_total or 0, 0)
            status = (
                lane_status if lane_status in {"queued", "active", "done"} else None
            )
            progress.lanes[lane_id] = {
                "id": lane_id,
                "label": lane_label or lane_id,
                "detail": detail or "",
                "current": current,
                "total": total,
                "unit": unit,
                "status": status or ("done" if 0 < total <= current else "active"),
                "updatedAt": now,
            }

    def _build_tqdm_progress_callback(self) -> Callable[..., None]:
        updates: dict[str | None, tuple[float, _ProgressValue]] = {}
        lock = Lock()

        def callback(
            desc: str,
            current: int,
            total: int,
            unit: str | None,
            detail_override: str | None = None,
            lane_id: str | None = None,
            lane_label: str | None = None,
            lane_status: ProgressLaneStatus | None = None,
        ) -> None:
            if desc in _ADAPTIVE_ACTIVITY_STAGES and unit == "page":
                return
            now = time.monotonic()
            value = (desc, unit, current, total, lane_status)
            with lock:
                previous = updates.get(lane_id)
                if previous is not None:
                    last_update, last_value = previous
                    elapsed = now - last_update
                    if (value == last_value and elapsed < 0.4) or (
                        current != total and elapsed < 0.35
                    ):
                        return
                updates[lane_id] = (now, value)
            suffix = f" {unit}" if unit else ""
            self.set_progress(
                desc or "Working",
                step=_TQDM_STEPS.get(desc),
                detail=detail_override or f"{desc}: {current}/{total}{suffix}",
                sub_current=current,
                sub_total=total,
                lane_id=lane_id,
                lane_label=lane_label,
                unit=unit,
                lane_status=lane_status,
            )

        return callback


def _sync_activity(source: SyncSource) -> SyncResult:
    return sync_recent_activity(
        DatabaseActivity(api_key=source.api_key), cache=Cache(source.cache_dir)
    )


def _log_sync_failure(message: str, source: SyncSource, exc: Exception) -> None:
    detail = str(exc)
    if source.api_key:
        detail = detail.replace(source.api_key, "[redacted]")
    logger.warning("{}: {}", message, detail)


def normalize_timezone(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip().strip("'\"")


def is_valid_timezone_name(value: str) -> bool:
    if not value:
        return False
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    except Exception:
        return False
    return True


def detect_system_timezone() -> str:
    candidates = [normalize_timezone(os.getenv("TZ"))]
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    localtime = str(Path("/etc/localtime").resolve())
    _, _, zone_name = localtime.partition("/zoneinfo/")
    candidates.append(zone_name)
    for candidate in candidates:
        if is_valid_timezone_name(candidate):
            return candidate

    local_timezone = datetime.now().astimezone().tzinfo
    if local_timezone is None:
        return "UTC"

    key = getattr(local_timezone, "key", None)
    if isinstance(key, str) and key.strip():
        return key

    timezone_name = local_timezone.tzname(None)
    if isinstance(timezone_name, str) and timezone_name.strip():
        return timezone_name

    return "UTC"


def resolve_timezone_override(env_path: Path) -> str:
    return _read_runtime_env_value(str(EnvVar.TIMEZONE), env_path, normalize_timezone)


def resolve_calendar_timezone() -> ZoneInfo:
    override = resolve_timezone_override(resolve_env_path())
    if is_valid_timezone_name(override):
        return ZoneInfo(override)
    system_timezone = detect_system_timezone()
    return ZoneInfo(
        system_timezone if is_valid_timezone_name(system_timezone) else "UTC"
    )


def safe_display_path(path: Path, *, root: Path | None = None) -> str:
    if root is not None:
        try:
            return str(path.relative_to(root))
        except ValueError:
            pass
    name = path.name.strip()
    return name or str(path)


def mask_api_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"••••{value[-4:]}"


def validate_api_token(token: str) -> tuple[bool, str | None, int | None]:
    client = TodoistAPIClient(max_attempts=1)
    spec = RequestSpec(
        endpoint=TodoistEndpoints.LIST_LABELS,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TimeoutSettings(connect=5.0, read=10.0),
        max_attempts=1,
    )
    try:
        payload = client.request_json(spec, operation_name="validate_api_token")
    except Exception as exc:  # pragma: no cover - network dependent
        return False, f"{type(exc).__name__}: {exc}", None

    if not isinstance(payload, dict):
        return False, f"Unexpected payload type: {type(payload).__name__}", None
    results = payload.get("results")
    if not isinstance(results, list):
        return False, "Unexpected labels response payload", None
    return True, None, len(results)
