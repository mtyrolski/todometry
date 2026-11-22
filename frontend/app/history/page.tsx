"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { HistoryDashboard } from "../components/DashboardView";
import { readJson } from "../lib/adminApi";
import type { ActivitySync, HistoryRange } from "../lib/dashboardData";
import styles from "./calendar.module.css";

const DAY = 86_400_000;
const WEEK = 7 * DAY;
const MAX_DAYS = 260 * 7;
const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const monthName = new Intl.DateTimeFormat("en", { month: "long", timeZone: "UTC" });
const dayName = new Intl.DateTimeFormat("en", { dateStyle: "full", timeZone: "UTC" });
const weekdayDate = new Intl.DateTimeFormat("en", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
const monthDay = new Intl.DateTimeFormat("en", { month: "short", day: "numeric", timeZone: "UTC" });
const shortDate = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" });
const months = Array.from({ length: 12 }, (_, month) => monthName.format(Date.UTC(2000, month)));
const historyLevels = ["years", "year", "month", "week"] as const;

type HistoryLevel = (typeof historyLevels)[number];
type PeriodLevel = Exclude<HistoryLevel, "years">;
type BrowseSelection = { level: "years" }
  | { level: "year" | "month"; date: number }
  | { level: "week"; date: number; day?: number };
type HistorySelection = BrowseSelection & { custom?: HistoryRange };
type Coverage = "covered" | "partial" | "unverified";
type CoverageCounts = Record<Coverage, number>;
type PeriodStats = { coverage: Coverage; eventCount: number };
type SyncStatus = { sync: ActivitySync; demoMode: boolean };
type ActivityCalendar = {
  timeZone: string;
  today: string;
  firstEventDate: string | null;
  latestEventDate: string | null;
  coverageStartDate: string | null;
  lastSyncedAt: string | null;
  years: Record<string, PeriodStats>;
  months: Record<string, PeriodStats>;
  days: Record<string, PeriodStats>;
  weeks: Record<string, Coverage>;
  summary: { days: CoverageCounts; weeks: CoverageCounts };
};
type ActivityStatus = SyncStatus & ActivityCalendar;
type CalendarRead = {
  month: string;
  calendar: ActivityCalendar;
  afterJob: string | null;
  previousRevision?: string | null;
};
type SyncRequest = { state: "pending" | "accepted" } | { state: "failed"; error: string };
type ActivityRead = {
  calendar: CalendarRead | null;
  job: SyncStatus | null;
  readState: "initial" | "observing" | "unavailable";
  request: SyncRequest | null;
};

function activityLabel(stats: PeriodStats) {
  const count = stats.eventCount;
  if (count > 0) return `${count.toLocaleString()} saved ${count === 1 ? "event" : "events"}`;
  return stats.coverage === "covered" ? "No activity" : "No saved events";
}

function periodLabel(label: string, stats: PeriodStats | undefined) {
  if (!stats) return label;
  const checked = stats.coverage === "covered" ? ", checked" : stats.coverage === "partial" ? ", partly checked" : "";
  return `${label}, ${activityLabel(stats)}${checked}`;
}

function CoverageMark({ coverage }: { coverage: Coverage | undefined }) {
  if (!coverage || coverage === "unverified") return null;
  return <span className={styles.marker} data-coverage={coverage} aria-hidden="true">{coverage === "covered" ? "✓" : "◐"}</span>;
}

function CalendarCount({ stats }: { stats: PeriodStats | undefined }) {
  if (!stats) return null;
  const label = stats.eventCount > 0 || stats.coverage === "covered" ? stats.eventCount.toLocaleString() : "–";
  return <span data-has-events={stats.eventCount > 0}>{label} <CoverageMark coverage={stats.coverage} /></span>;
}

function PeriodTile({ label, title, stats, onClick, pressed, disabled }: {
  label: string;
  title: string;
  stats: PeriodStats | undefined;
  onClick: () => void;
  pressed?: boolean;
  disabled?: boolean;
}) {
  const description = periodLabel(label, stats);
  return (
    <button className={styles.tile} type="button" aria-label={description} title={description}
      aria-pressed={pressed} disabled={disabled} onClick={onClick}>
      <strong>{title}</strong>
      {stats && (
        <span className={styles.tileDetail} data-has-events={stats.eventCount > 0}>
          {activityLabel(stats)} <CoverageMark coverage={stats.coverage} />
        </span>
      )}
    </button>
  );
}

function coveredCount(counts: CoverageCounts) {
  return `${counts.covered.toLocaleString()} of ${(counts.covered + counts.partial + counts.unverified).toLocaleString()}`;
}

async function requestActivity<T extends { sync: ActivitySync }>(path: string, init: RequestInit, fallback: string): Promise<T> {
  const response = await fetch(path, init).catch(() => null);
  const payload = response ? await readJson<T & { detail?: string }>(response).catch(() => null) : null;
  if (!response?.ok || !payload?.sync) {
    throw new Error(typeof payload?.detail === "string" ? payload.detail : fallback);
  }
  return payload;
}

function confirmedRevision(read: CalendarRead | null) {
  if (read?.previousRevision !== undefined) return read.previousRevision;
  return read?.calendar.lastSyncedAt ?? null;
}

function needsCalendar(read: CalendarRead | null, month: string | null, sync: ActivitySync | undefined) {
  return !read || (month !== null && month !== read.month)
    || (sync?.state === "succeeded" && sync.startedAt !== read.afterJob);
}

function syncIntent() {
  const url = new URL(window.location.href);
  return url.searchParams.get("sync") === "1" ? url : null;
}

function consumeSyncIntent() {
  const url = syncIntent();
  if (!url) return false;
  url.searchParams.delete("sync");
  window.history.replaceState(window.history.state, "", url);
  return true;
}

function useActivityStatus(month: string | null) {
  const [activity, setActivity] = useState<ActivityRead>({
    calendar: null, job: null, readState: "initial", request: null
  });
  const observer = useRef<AbortController | null>(null);
  const { request } = activity;

  const startSync = useCallback(async () => {
    const current = observer.current;
    if (!current || current.signal.aborted) return;
    current.abort();
    consumeSyncIntent();
    setActivity((previous) => ({ ...previous, request: { state: "pending" } }));
    const fallback = "Could not start activity sync. Try again.";
    try {
      const { sync } = await requestActivity<Pick<SyncStatus, "sync">>("/api/activity/sync", { method: "POST" }, fallback);
      setActivity((previous) => ({ ...previous, job: { sync, demoMode: false }, request: { state: "accepted" } }));
    } catch (error) {
      setActivity((previous) => ({
        ...previous, request: { state: "failed", error: error instanceof Error ? error.message : fallback }
      }));
    }
  }, []);

  // The latest response seeds the loop whenever its month or submission changes.
  useEffect(() => {
    if (request?.state === "pending") return;
    setActivity((previous) => previous.readState === "initial" ? { ...previous, readState: "observing" } : previous);
    const controller = new AbortController();
    observer.current = controller;
    const { signal } = controller;
    const options: RequestInit = { signal, cache: "no-store" };
    const path = month ? `/api/activity/status?month=${month}` : "/api/activity/status";
    let { calendar, job } = activity;
    let refreshCalendar = needsCalendar(calendar, month, job?.sync);
    let timer: ReturnType<typeof setTimeout> | undefined;

    const readJob = () => requestActivity<SyncStatus>(
      "/api/activity/status?sync_only=true", options, "Could not check sync status."
    );
    const pollDelay = () => job?.sync.state === "running" ? 1500 : 10_000;

    const poll = async () => {
      if (signal.aborted) return;
      let statusUnavailable = false;
      try {
        if (refreshCalendar) {
          const { sync, demoMode, ...data } = await requestActivity<ActivityStatus>(path, options, "Could not load saved activity.");
          // A full dashboard job may have saved recent activity before publishing its new charts.
          const provisional = sync.state === "running" && data.lastSyncedAt && sync.startedAt
            && Date.parse(data.lastSyncedAt) >= Date.parse(sync.startedAt);
          calendar = {
            month: month ?? data.today.slice(0, 7),
            calendar: data,
            afterJob: sync.state === "succeeded" ? sync.startedAt : null,
            ...(provisional ? { previousRevision: confirmedRevision(calendar) } : {})
          };
          job = { sync, demoMode };
        } else {
          job = await readJob();
        }
        refreshCalendar = needsCalendar(calendar, month, job.sync);
      } catch {
        if (signal.aborted) return;
        statusUnavailable = true;
        if (refreshCalendar && (job?.sync.state === "running" || syncIntent())) {
          job = await readJob().catch(() => job);
        }
      }
      if (signal.aborted) return;
      const response: Pick<ActivityRead, "calendar" | "job" | "readState"> = {
        calendar, job, readState: statusUnavailable ? "unavailable" : "observing"
      };
      setActivity((previous) => ({
        ...response,
        request: response.job?.sync.state === "running" && previous.request?.state === "failed"
          ? null : previous.request
      }));
      if (consumeSyncIntent() && !job?.demoMode && job?.sync.state !== "running") {
        void startSync();
        return;
      }
      timer = setTimeout(poll, statusUnavailable ? 3000 : refreshCalendar ? 0 : pollDelay());
    };

    if (refreshCalendar || syncIntent()) queueMicrotask(poll);
    else timer = setTimeout(poll, pollDelay());
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [month, request, startSync]);

  const sync = activity.job?.sync;
  const syncError = activity.request?.state === "failed" ? activity.request.error
    : sync?.state === "failed" ? sync.error ?? "Activity sync failed." : null;
  return {
    calendar: activity.calendar, sync, syncError, startSync,
    demoMode: activity.job?.demoMode, syncing: request?.state === "pending" || sync?.state === "running",
    canBrowse: activity.readState !== "initial", statusUnavailable: activity.readState === "unavailable"
  };
}

function dayKey(time: number) {
  return new Date(time).toISOString().slice(0, 10);
}

function startOfWeek(time: number) {
  return time - ((new Date(time).getUTCDay() + 6) % 7) * DAY;
}

function weekNumber(monday: number) {
  const thursday = new Date(monday + 3 * DAY);
  return Math.floor((thursday.getTime() - Date.UTC(thursday.getUTCFullYear(), 0)) / WEEK) + 1;
}

function periodStart(level: PeriodLevel, time: number, offset = 0) {
  if (level === "week") return startOfWeek(time) + offset * WEEK;
  const date = new Date(time);
  const year = date.getUTCFullYear();
  return level === "year" ? Date.UTC(year + offset, 0) : Date.UTC(year, date.getUTCMonth() + offset);
}

function selectedRange(selection: HistorySelection, today: number): HistoryRange | null {
  if (selection.custom) return selection.custom;
  if (selection.level === "years") return null;
  if (selection.level === "week" && selection.day !== undefined) {
    const day = dayKey(Math.min(selection.day, today));
    return { beg: day, end: day };
  }
  const date = Math.min(selection.date, today);
  return {
    beg: dayKey(periodStart(selection.level, date)),
    end: dayKey(Math.min(periodStart(selection.level, date, 1) - DAY, today))
  };
}

function browserToday() {
  const now = new Date();
  return dayKey(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
}

function weekStats(status: ActivityCalendar | undefined, monday: number): PeriodStats | undefined {
  const coverage = status?.weeks[dayKey(monday)];
  if (!coverage) return undefined;
  const eventCount = weekdays.reduce((total, _, index) => (
    total + (status?.days[dayKey(monday + index * DAY)]?.eventCount ?? 0)
  ), 0);
  return { coverage, eventCount };
}

export default function ActivityHistoryPage() {
  const [selection, setSelection] = useState<HistorySelection>({ level: "years" });
  const [draft, setDraft] = useState<HistoryRange>({ beg: "", end: "" });
  const browseHeading = useRef<HTMLHeadingElement>(null);
  const focusBrowseHeading = useRef(false);
  const { level } = selection;
  const requestedMonth = level === "month" || level === "week" ? dayKey(selection.date).slice(0, 7) : null;
  const { calendar, demoMode, sync, syncing, syncError, canBrowse, statusUnavailable, startSync } = useActivityStatus(requestedMonth);
  const status = calendar?.calendar;
  const today = status?.today ?? (canBrowse ? browserToday() : "");
  const todayTime = today ? Date.parse(today) : 0;
  const activeDate = level === "years" ? todayTime : Math.min(selection.date, todayTime);
  const range = selectedRange(selection, todayTime);
  const cursor = new Date(activeDate);
  const year = cursor.getUTCFullYear();
  const monthIndex = cursor.getUTCMonth();
  const month = Date.UTC(year, monthIndex);
  const nextMonth = Date.UTC(year, monthIndex + 1);
  const calendarStatus = calendar?.month === dayKey(month).slice(0, 7) ? status : undefined;
  const week = startOfWeek(activeDate);
  const weekTitle = `${monthDay.format(week)} – ${shortDate.format(week + 6 * DAY)}`;
  const monthTitle = `${months[monthIndex]} ${year}`;
  const browseTitle = { years: "Years", year: String(year), month: monthTitle, week: weekTitle }[level];
  const firstMonday = startOfWeek(month);
  const availableYears = Object.keys(status?.years ?? {}).map(Number).sort((left, right) => right - left);
  const years = availableYears.length ? availableYears : [new Date(todayTime).getUTCFullYear()];
  const firstYear = years[years.length - 1];
  const days = (Date.parse(draft.end) - Date.parse(draft.beg)) / DAY + 1;
  const entireRange = Boolean(draft.beg && draft.end);
  const valid = entireRange && days > 0 && days <= MAX_DAYS && draft.end <= today;
  const syncDate = status?.lastSyncedAt ? new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short", timeZone: status.timeZone
  }).format(new Date(status.lastSyncedAt)) : null;
  const savedStart = status?.firstEventDate;
  const savedEnd = status?.latestEventDate;
  const coverageStart = status?.coverageStartDate;
  const checkedFrom = coverageStart ? shortDate.format(Date.parse(coverageStart)) : null;
  const savedRange = savedStart
    ? [savedStart, ...(savedEnd && savedEnd !== savedStart ? [savedEnd] : [])].map((date) => shortDate.format(Date.parse(date))).join(" → ")
    : "No saved events";

  useEffect(() => {
    if (focusBrowseHeading.current) {
      focusBrowseHeading.current = false;
      browseHeading.current?.focus({ preventScroll: true });
    }
  }, [level]);

  const openPeriod = (nextLevel: HistoryLevel, time = activeDate, day?: number) => {
    focusBrowseHeading.current = level !== nextLevel;
    const date = Math.min(time, todayTime);
    if (nextLevel === "years") setSelection({ level: nextLevel });
    else if (nextLevel === "week") setSelection({ level: nextLevel, date, day });
    else setSelection({ level: nextLevel, date });
  };

  const previous = level === "years" ? null : periodStart(level, activeDate, -1);
  const next = level === "years" ? null : periodStart(level, activeDate, 1);

  return (
    <>
      <header className="pageHeader">
        <h1>Activity History</h1>
      </header>
      <section className={`card ${styles.panel}`} aria-labelledby="saved-activity-title">
        <div className={styles.panelHeader}>
          <h2 id="saved-activity-title">Saved activity</h2>
          {demoMode ? <span className="muted">Demo data</span> : (
            <button className="button" type="button" onClick={startSync} disabled={syncing || !canBrowse}>
              {syncing ? "Syncing…" : syncError ? "Retry sync" : "Sync now"}
            </button>
          )}
        </div>
        {status ? (
          <>
            <dl className={styles.summary}>
              <div><dt>Recorded dates</dt><dd>{savedRange}</dd></div>
              <div><dt>Last successful sync</dt><dd>{syncDate
                ? <time dateTime={status.lastSyncedAt ?? undefined} title={status.timeZone}>{syncDate}</time>
                : "No sync recorded yet"}</dd></div>
            </dl>
            {savedStart && (!coverageStart || savedStart < coverageStart) && (
              <p className="muted">{checkedFrom
                ? `Coverage tracking starts on ${checkedFrom}. Earlier saved events are available to browse.`
                : "These events were saved before sync coverage was tracked. A sync records coverage for the dates it checks."}</p>
            )}
            {(savedStart || coverageStart) && (
              <details className={styles.disclosure}>
                <summary>Sync coverage</summary>
                <dl className={styles.summary}>
                  <div><dt>Checks start on</dt><dd>{checkedFrom ?? "No dates checked yet"}</dd></div>
                  <div><dt>Days checked</dt><dd>{coveredCount(status.summary.days)}</dd></div>
                  <div><dt>Weeks checked</dt><dd>{coveredCount(status.summary.weeks)}</dd></div>
                </dl>
              </details>
            )}
          </>
        ) : !statusUnavailable && <p className="muted">Loading saved activity…</p>}
        {statusUnavailable && (
          <p role="status" className="muted">
            {status ? "Could not check for sync updates. Retrying…" : "Could not load saved activity. Retrying…"}
          </p>
        )}
        <div className={styles.syncMessage} aria-live="polite">
          {syncing && <p>Fetching the latest Todoist activity…</p>}
          {!syncing && !syncError && sync?.state === "succeeded" && (
            <p>Sync complete. {sync.newEvents === null ? null : `${sync.newEvents.toLocaleString()} new ${sync.newEvents === 1 ? "event" : "events"}.`}</p>
          )}
        </div>
        {!syncing && syncError && <p className={styles.syncError} role="alert">{syncError}</p>}
      </section>
      {today ? (
        <section className={`card ${styles.panel}`} aria-labelledby="history-browse-title">
          <div className={styles.panelHeader}>
            <h2 id="history-browse-title">Browse history</h2>
            {(level !== "week" || week !== startOfWeek(todayTime)) && (
              <button className="button buttonGhost buttonSmall" type="button" onClick={() => openPeriod("week", todayTime)}>This week</button>
            )}
          </div>
          {level !== "years" && (
            <nav aria-label="History levels">
              <ol className={styles.breadcrumbs}>
                {historyLevels.slice(0, historyLevels.indexOf(level)).map((parent) => (
                  <li key={parent}>
                    <button className="button buttonGhost buttonSmall" type="button" onClick={() => openPeriod(parent)}>
                      {parent === "years" ? "Years" : parent === "year" ? year : months[monthIndex]}
                    </button>
                    <span aria-hidden="true">›</span>
                  </li>
                ))}
              </ol>
            </nav>
          )}
          <div className={styles.periodHeader}>
            <h3 ref={browseHeading} tabIndex={-1}>{browseTitle}</h3>
            <div className={styles.periodActions}>
              {range && <a className="button buttonGhost buttonSmall" href="#history-results">Go to charts</a>}
              {level === "week" && (
                <button className="button buttonGhost buttonSmall" type="button" onClick={() => openPeriod("week")}>Whole week</button>
              )}
              {level !== "years" && previous !== null && next !== null && (
                <>
                  <button className="button buttonGhost buttonSmall" type="button" aria-label={`Previous ${level}`}
                    disabled={previous < periodStart(level, Date.UTC(firstYear, 0))} onClick={() => openPeriod(level, previous)}>←</button>
                  <button className="button buttonGhost buttonSmall" type="button" aria-label={`Next ${level}`}
                    disabled={next > todayTime} onClick={() => openPeriod(level, next)}>→</button>
                </>
              )}
            </div>
          </div>
          {level === "years" && (
            <div className={styles.tiles}>
              {years.map((value) => (
                <PeriodTile key={value} label={String(value)} title={String(value)} stats={status?.years?.[value]}
                  onClick={() => openPeriod("year", Date.UTC(value, 0))} />
              ))}
            </div>
          )}
          {level === "year" && (
            <div className={styles.tiles}>
              {months.map((name, index) => {
                const start = Date.UTC(year, index);
                if (start > todayTime) return null;
                return <PeriodTile key={name} label={`${name} ${year}`} title={name} stats={status?.months?.[dayKey(start).slice(0, 7)]}
                  onClick={() => openPeriod("month", start)} />;
              })}
            </div>
          )}
          {level === "month" && (
            <>
              <p className={styles.legend}>Counts show saved events. <CoverageMark coverage="covered" /> Checked <CoverageMark coverage="partial" /> Partly checked · – No saved events</p>
              <table className={styles.calendar} aria-label={monthTitle}>
                <thead><tr><th scope="col">Week</th>{weekdays.map((name) => <th scope="col" key={name}>{name}</th>)}</tr></thead>
                <tbody>
                  {Array.from({ length: Math.ceil((nextMonth - firstMonday) / WEEK) }, (_, index) => firstMonday + index * WEEK).map((monday) => {
                    const stats = weekStats(calendarStatus, monday);
                    const label = periodLabel(`Week of ${shortDate.format(monday)}`, stats);
                    return <tr key={monday}>
                      <th scope="row">
                        <button className={styles.dateCell} type="button" aria-label={label} title={label} disabled={monday > todayTime}
                          onClick={() => openPeriod("week", Math.max(month, monday))}>
                          <strong>Week {weekNumber(monday)}</strong>
                          <CalendarCount stats={stats} />
                        </button>
                      </th>
                      {weekdays.map((name, index) => {
                        const day = monday + index * DAY;
                        const key = dayKey(day);
                        const stats = calendarStatus?.days[key];
                        const label = periodLabel(dayName.format(day), stats);
                        return <td key={name}>
                          <button className={styles.dateCell} type="button" aria-label={label} title={label} aria-current={key === today ? "date" : undefined}
                            data-outside={day < month || day >= nextMonth} disabled={day > todayTime}
                            onClick={() => openPeriod("week", Math.max(month, monday), day)}>
                            <strong>{new Date(day).getUTCDate()}</strong>
                            <CalendarCount stats={stats} />
                          </button>
                        </td>;
                      })}
                    </tr>;
                  })}
                </tbody>
              </table>
            </>
          )}
          {level === "week" && (
            <div className={`${styles.tiles} ${styles.weekTiles}`}>
              {weekdays.map((name, index) => {
                const day = week + index * DAY;
                const key = dayKey(day);
                return <PeriodTile key={name} label={dayName.format(day)} title={weekdayDate.format(day)} stats={calendarStatus?.days[key]}
                  pressed={range?.beg === key && range.end === key} disabled={day > todayTime} onClick={() => openPeriod("week", activeDate, day)} />;
              })}
            </div>
          )}
          <details className={styles.disclosure}>
            <summary>Custom date range</summary>
            <form className={styles.customRange} onSubmit={(event) => {
              event.preventDefault();
              if (valid) setSelection((previous) => ({ ...previous, custom: draft }));
            }}>
              <label className="control">
                <span className="muted tiny">From date</span>
                <input className="dateInput" type="date" max={today} value={draft.beg} onChange={(event) => setDraft({ ...draft, beg: event.target.value })} />
              </label>
              <label className="control">
                <span className="muted tiny">To date</span>
                <input className="dateInput" type="date" max={today} value={draft.end} onChange={(event) => setDraft({ ...draft, end: event.target.value })} />
              </label>
              <button className="button" type="submit" disabled={!valid}>Load charts</button>
              {entireRange && !valid && <p className="muted tiny" role="status">
                {days <= 0 ? "The end date must be on or after the start date."
                  : days > MAX_DAYS ? "Choose a range of up to 260 weeks."
                  : "Choose dates through today."}
              </p>}
            </form>
          </details>
        </section>
      ) : null}
      {range && <HistoryDashboard range={range} lastSyncedAt={confirmedRevision(calendar)} />}
    </>
  );
}
