"use client";

import { readJson, requestJson } from "./adminApi";
import { useEffect, useMemo, useRef, useState } from "react";
import type { DashboardHome, DashboardStatus, Granularity, Health, HistoryCharts, HistoryRange } from "./dashboardData";
import type { DashboardProgress } from "../components/ProgressSteps";
import type { ProjectTimelineData } from "./projectTimeline";

const DASHBOARD_RETRY_LIMIT = 300;
const DASHBOARD_RETRY_DELAY_MS = 2500;
const DASHBOARD_POLL_DELAY_MS = 2000;

function isRetryableFetchError(message: string): boolean {
  return /invalid json response|failed to fetch|networkerror|socket hang up|econnrefused|econnreset|timed out|timeout/i.test(
    message
  );
}

export function useApiHealth(pollMs = 10_000) {
  const [health, setHealth] = useState<Health>(null);
  const [loadingHealth, setLoadingHealth] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchHealth = async () => {
      try {
        setLoadingHealth(true);
        setError(null);
        const res = await fetch("/api/health");
        if (!res.ok) throw new Error("API unavailable");
        const data = await readJson<Health>(res);
        setHealth(data);
      } catch {
        setError("Unable to connect to the API server. Please check that the backend is running.");
      } finally {
        setLoadingHealth(false);
      }
    };

    fetchHealth();
    const interval = setInterval(fetchHealth, pollMs);
    return () => clearInterval(interval);
  }, [pollMs]);

  return { health, loadingHealth, error };
}

type HistoryRead = {
  request: string;
  history: HistoryCharts | null;
  error?: string;
};

function historyForRange(history: HistoryCharts | null | undefined, beg: string, end: string) {
  return history?.range.beg === beg && history.range.end === end ? history : null;
}

export function useHistoryCharts({ beg, end }: HistoryRange, lastSyncedAt: string | null) {
  const request = `${beg}:${end}:${lastSyncedAt ?? ""}`;
  const [read, setRead] = useState<HistoryRead | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const load = async () => {
      const fallback = "Could not load saved activity";
      try {
        const query = new URLSearchParams({ beg, end });
        const payload = await requestJson<HistoryCharts>(`/api/dashboard/history?${query}`, { signal, cache: "no-store" }, fallback);
        if (!payload?.range || !historyForRange(payload, beg, end) || !payload.figures) throw new Error(fallback);
        if (!signal.aborted) setRead({ request, history: payload });
      } catch (error) {
        if (signal.aborted) return;
        const message = error instanceof Error ? error.message : fallback;
        setRead((previous) => ({
          request,
          history: historyForRange(previous?.history, beg, end),
          error: /invalid json response/i.test(message) ? fallback : message
        }));
      }
    };
    queueMicrotask(() => {
      if (!signal.aborted) void load();
    });
    return () => controller.abort();
  }, [beg, end, request]);

  const history = historyForRange(read?.history, beg, end);
  const loading = read?.request !== request;
  const error = loading ? null : read?.error ?? null;
  return {
    history,
    loading,
    error: error && history ? `${error.replace(/\.$/, "")}. Showing the last loaded view.` : error
  };
}

export function useDashboardHome({ enabled = true }: { enabled?: boolean } = {}) {
  const [granularity, setGranularity] = useState<Granularity>("W");
  const [weeks, setWeeks] = useState(12);
  const [dashboard, setDashboard] = useState<DashboardHome | null>(null);
  const latestDashboard = useRef<DashboardHome | null>(null);
  const [loadingDashboard, setLoadingDashboard] = useState(false);
  const [dashboardError, setDashboardError] = useState<string | null>(null);
  const [progress, setProgress] = useState<DashboardProgress | null>(null);
  const [retrying, setRetrying] = useState(false);
  const refreshing = dashboard?.sync?.state === "running";

  useEffect(() => {
    setDashboardError(null);
    setRetrying(false);
    if (!enabled) {
      setLoadingDashboard(false);
      return;
    }
    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;

    const load = async () => {
      if (signal.aborted) return;
      const previous = latestDashboard.current;
      let status = 0;
      try {
        setLoadingDashboard(true);
        const qs = new URLSearchParams({ granularity, weeks: String(weeks) });
        if (previous) qs.set("cached_only", "true");
        const response = await fetch(`/api/dashboard/home?${qs}`, { signal });
        status = response.status;
        const payload = await readJson<DashboardHome & { detail?: string }>(response);
        if (signal.aborted) return;
        if (!response.ok || payload.error) {
          throw new Error(payload.error ?? payload.detail ?? `Failed to load dashboard (${response.status})`);
        }
        latestDashboard.current = payload;
        setDashboard(payload);
        setDashboardError(null);
        setRetrying(false);
        failures = 0;
        if (payload.sync?.state === "running") {
          timer = setTimeout(load, DASHBOARD_POLL_DELAY_MS);
        }
      } catch (e) {
        if (signal.aborted) return;
        const message = e instanceof Error ? e.message : "Failed to load dashboard";
        const transient = isRetryableFetchError(message) || (previous !== null && status >= 500);
        const canRetry = transient && failures < DASHBOARD_RETRY_LIMIT &&
          (!previous || previous.sync?.state === "running");
        if (canRetry) {
          failures += 1;
          timer = setTimeout(load, DASHBOARD_RETRY_DELAY_MS);
        }
        if (!previous && canRetry) {
          setRetrying(true);
          setDashboardError("Dashboard API is starting. Retrying shortly...");
        } else {
          setRetrying(false);
          const displayMessage = /invalid json response/i.test(message) ? "Could not load dashboard data" : message;
          setDashboardError(previous ? `${displayMessage}. Showing the last loaded view.` : displayMessage);
        }
      } finally {
        if (!signal.aborted) setLoadingDashboard(false);
      }
    };
    queueMicrotask(load);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [enabled, granularity, weeks]);

  const shouldPollProgress = enabled && !dashboard &&
    (loadingDashboard || retrying || !dashboardError);

  useEffect(() => {
    if (!shouldPollProgress) {
      setProgress(null);
      return;
    }
    const controller = new AbortController();
    let active = true;

    const loadProgress = async () => {
      try {
        const res = await fetch("/api/dashboard/progress", { signal: controller.signal });
        if (!res.ok) return;
        const payload = await readJson<DashboardProgress>(res);
        if (!active) return;
        setProgress(payload);
      } catch (e) {
        if (e && typeof e === "object" && "name" in e && (e as { name?: string }).name === "AbortError") {
          return;
        }
      }
    };

    loadProgress();
    const interval = setInterval(loadProgress, 700);
    return () => {
      active = false;
      controller.abort();
      clearInterval(interval);
    };
  }, [shouldPollProgress]);

  const progressDisplay = useMemo(() => {
    if (progress?.active) return progress;
    if (!shouldPollProgress) return null;
    return {
      active: true,
      stage: null,
      step: 1,
      totalSteps: 3,
      startedAt: null,
      updatedAt: null,
      detail: "Connecting to the API and preparing the dashboard...",
      error: null
    } satisfies DashboardProgress;
  }, [progress, shouldPollProgress]);

  return {
    dashboard,
    loadingDashboard,
    dashboardError,
    progressDisplay,
    refreshing,
    retrying,
    granularity,
    setGranularity,
    weeks,
    setWeeks
  };
}

export function useDashboardStatus({ pollMs = 30_000 }: { pollMs?: number } = {}) {
  const [status, setStatus] = useState<DashboardStatus | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [statusRefreshNonce, setStatusRefreshNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      try {
        setLoadingStatus(true);
        const res = await fetch("/api/dashboard/status", { signal: controller.signal, cache: "no-store" });
        if (!res.ok) throw new Error("status");
        const payload = await readJson<DashboardStatus>(res);
        setStatus(payload);
      } catch (e) {
        if (e && typeof e === "object" && "name" in e && (e as { name?: string }).name === "AbortError") {
          return;
        }
        setStatus(null);
      } finally {
        setLoadingStatus(false);
      }
    };
    load();
    return () => controller.abort();
  }, [statusRefreshNonce]);

  const refreshStatus = () => setStatusRefreshNonce((value) => value + 1);

  useEffect(() => {
    if (!pollMs) return;
    const interval = setInterval(() => setStatusRefreshNonce((value) => value + 1), pollMs);
    return () => clearInterval(interval);
  }, [pollMs]);

  return { status, loadingStatus, refreshStatus };
}

export function useSyncLabel(status: DashboardStatus | null) {
  const [syncClock, setSyncClock] = useState(() => Date.now());

  useEffect(() => {
    const interval = setInterval(() => setSyncClock(Date.now()), 60_000);
    return () => clearInterval(interval);
  }, []);

  const label = useMemo(() => {
    if (!status) return "Sync status unavailable";
    const lastRefresh = status.apiCache.lastRefresh ?? status.activityCache?.mtime ?? null;
    if (!lastRefresh) {
      return status.activityCache ? "Sync time unknown" : "Local cache missing";
    }
    const lastMs = Date.parse(lastRefresh);
    if (Number.isNaN(lastMs)) return "Sync unknown";
    const diffMs = Math.max(0, syncClock - lastMs);
    if (diffMs < 60_000) return "Synced just now";
    const minutes = Math.floor(diffMs / 60_000);
    if (minutes < 60) return `Synced ${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `Synced ${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `Synced ${days}d ago`;
  }, [status, syncClock]);

  const title = useMemo(() => {
    if (!status) return undefined;
    if (status.apiCache.lastRefresh) return status.apiCache.lastRefresh;
    if (!status.activityCache) return "activity.joblib missing";
    return status.activityCache.mtime ?? "sync time unavailable";
  }, [status]);

  return { label, title };
}

export function useProjectTimeline() {
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [data, setData] = useState<ProjectTimelineData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const query = new URLSearchParams();
        if (refreshNonce) query.set("refresh", "true");
        const response = await fetch(`/api/dashboard/project-timeline?${query}`, { signal: controller.signal });
        const payload = await readJson<ProjectTimelineData>(response);
        if (!response.ok || payload.error) throw new Error(payload.error ?? `Failed to load timeline (${response.status})`);
        setData(payload);
      } catch (reason) {
        if (reason && typeof reason === "object" && "name" in reason && (reason as { name?: string }).name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "Failed to load project timeline");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    load();
    return () => controller.abort();
  }, [refreshNonce]);

  return {
    data,
    loading,
    error,
    refresh: () => setRefreshNonce((value) => value + 1)
  };
}
