# Architecture

## Desktop runtime

`make dashboard` starts Uvicorn and Next through `scripts/dashboard_stack.sh`.
The installed launcher prepares local paths and starts the same
`todometry.web.api:app`. The FastAPI composition root owns a `DashboardState`
and resolved settings path per lifespan. Routes use these resources directly
and call the dashboard runtime and settings services through explicit imports.

The browser validates the token through the admin API before requesting
`/api/dashboard/home`. The dashboard runtime publishes one `DashboardSnapshot`
with activity, project metadata and the resolved hierarchy. It can restore
`dashboard_state.joblib` or rebuild from saved activity before requiring a
network refresh. The snapshot memoizes the latest applicable Overview
response. Activity and adjustment file revisions, date, range, timezone and
dashboard settings affect the result. The current sync status is attached to
each response separately from its cached figures.

The web UI loads Plotly on demand. Its narrow type declarations cover the
dynamic Plotly and React Plotly factory imports.

The Live Logs page reads `/api/runtime/logs` and
`/api/runtime/logs/read`. The allowlist contains the API and frontend logs.
The page can select and page through available sources.

## Synchronization and history

`DashboardState.sync` owns the task, progress and result for a web lifespan.
Overview can queue a full refresh when the snapshot ages or activity changes.
**Refresh data** opens Activity History. History observes an already-running
job or starts one with `POST /api/activity/sync`. The browser polls the job
with `/api/activity/status?sync_only=true`. Navigation can stop reading the job
while the server continues it. A full refresh uses the same recent scan and
can also recover older history, load project metadata and rebuild the dashboard.
A projection error can accompany a successful activity sync. Saved views remain
available during a retry or failed refresh. Shutdown waits for the job's file
writes to finish.

The job captures its token, cache path and demo mode at start. Concurrent
requests in the same lifespan can join for the same source. An activity sync
POST for a different source receives HTTP 409 while the job runs. An explicit
Overview refresh waits for the other job. CLI commands `make sync` and
`make sync_history` use the same activity scanning and merge code. The per-cache
OS file lock serializes recent and historical scans between processes where
supported. Job status is local to each process.

Recent scans overlap the last successful scan boundary by one UTC calendar day
to catch late events. They advance that boundary only after all pages and the
event merge succeed. Historical scans checkpoint each completed window. The
calendar counts saved events in `activity.joblib` and reads verified coverage
and last success from `activity_coverage.joblib`. An unverified day can
contain saved events, and a verified day can have zero events. An open calendar
observes the web job. CLI changes appear after a full status reload.

Activity History has one selected range. A closed week spans Monday through
Sunday in the app timezone, including weeks crossing a month or year. The
current week ends today. The range makes one cached request to
`/api/dashboard/history` with inclusive dates that the backend converts to a
half-open UTC interval. Four figures use local data, including a saved
projection when the original event ledger is unavailable. Hierarchy playback
loads on demand. The selected dates set the History viewport. Overview
forecasts extend to the end of their week, month or quarter.

## Project mapping and Android

The shared React wizard selects main projects and assigns other active or
archived projects by ID. It derives a partial patch from the current draft.
Skipping keeps the saved rule. Save errors keep the choices and current step
available for retry. Desktop opens the wizard after token setup and from
**Project Adjustments**. The API merges the patch with the selected file's
saved rules under the configuration lock and replaces the file atomically.
The next local read uses the updated projection.

`todometry/database/dataframe.py` resolves project grouping for web reports,
hierarchy and timeline. An ID mapped to itself creates an explicit root.
Existing name rules resolve when their targets are known. Ambiguous names use
the saved label. Saved rules survive missing metadata. Python skips an
unavailable ID target while projecting data and rejects the edge that would
close a cycle. Archive-parent names also scope the desktop's supplemental
archive scans.

Android packages the shared React UI in a WebView. Native code owns account
state, Todoist sync and local snapshot storage. Guided setup opens after a
first sync started from the connection form and can be reopened in **Project
grouping**. `DashboardViewModel` handles `save-adjustments` through the native
bridge, writes the account snapshot and publishes the model before sending
the matching `todoist-adjustments-result`. The result also covers rejected
saves. Android uses `ProjectAdjustments.java` for local project grouping.
It can keep a named ID bucket for an unavailable target and falls back to the
origin's physical or promoted root when it encounters a cycle. Physical
project paths come from Todoist metadata on both platforms.

The desktop export and Android import use
`todoist-assistant-project-adjustments-v1`, which carries name rules,
archived-parent names and individual ID rules. Each app stores its own data.

## Local paths and compatibility

A source checkout uses `.cache/todometry` in the working directory unless
`TODOIST_CACHE_DIR` is set. The runtime looks for the credential file using
`TODOIST_DATA_DIR`, `TODOIST_CACHE_DIR`, the working directory and the
repository fallback in that order. Dashboard settings live under
`TODOIST_CONFIG_DIR` or the applicable data/repository `configs` directory.
Adjustments live in `personal/`, with `TODOIST_PERSONAL_DIR` as an override.
Files accept `link_adjustements`, `archived_parent_projects` and
`project_id_mappings` as literal values. A process lock serializes settings and
mapping writes. A separate lock protects dotenv initialization and updates.

The installed launcher copies retained files from former `todoist-assistant`
defaults on upgrade. Existing destination files win, the original files stay
available and an interrupted copy resumes on the next launch. Explicit path
overrides bypass the default migration. The `todoist.core.types` shim remains
because v0.4.0 joblib files name those classes when loading their objects.
New cache objects are written with the `todometry` namespace.

## Verification

`make ci` runs Python and frontend checks. Browser tests use Chromium and
emulate Todoist and the Android bridge. See the
[development setup](../README.md#development).

The architecture guard `python -m scripts.check_architecture` checks API
dependencies, namespace injection, catch-all web exports and Ruff undefined-name
exemptions. The installed launcher is an explicit ASGI consumer.

Native Android tests and builds use JDK 17 and Android SDK 36.
See [Android builds and device tests](ANDROID.md) and
[Windows and macOS build instructions](BUILDING.md).
