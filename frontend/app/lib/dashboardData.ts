import type { PlotlyFigure } from "../components/PlotCard";
import type { InsightItem } from "../components/InsightCard";
import type { LeaderboardItem } from "../components/LeaderboardCard";
import type { ServiceStatus } from "../components/ServiceMonitor";

export type Health = { status: string; version?: string } | null;

export type Granularity = "W" | "ME" | "3ME";

type DashboardFigures = {
  weeklyCompletionTrend?: PlotlyFigure;
  taskCountTrend?: PlotlyFigure;
  taskLifespans?: PlotlyFigure;
  taskLifespansPeriod?: PlotlyFigure;
  completedTasksPeriodically?: PlotlyFigure;
  cumsumCompletedTasksPeriodically?: PlotlyFigure;
  heatmapEventsByDayHour?: PlotlyFigure;
  eventsOverTime?: PlotlyFigure;
  activeProjectHierarchy?: PlotlyFigure;
};

export type HistoryRange = { beg: string; end: string };

export type ActivitySync = {
  state: "idle" | "running" | "failed" | "succeeded";
  error: string | null;
  startedAt: string | null;
  newEvents: number | null;
  dashboardError?: string;
};

export type HistoryCharts = {
  range: HistoryRange & { mode: "custom" };
  rangeHasActivity: boolean;
  figures: Pick<DashboardFigures, "activeProjectHierarchy" | "eventsOverTime" | "heatmapEventsByDayHour" | "taskLifespansPeriod">;
  refreshedAt: string;
  timeZone: string;
};

type ConfigurableItem = {
  key: string;
  label: string;
  icon?: string;
  configPath?: string;
  anchor?: string;
  summary?: string;
};

type HabitTrackerItem = {
  taskId: string;
  name: string;
  projectId: string;
  projectName: string;
  color: string;
  weeklyCompleted: number;
  weeklyRescheduled: number;
  allTimeCompleted: number;
  allTimeRescheduled: number;
  reliability: number | null;
};

export type DashboardHome = {
  sync: ActivitySync;
  noData?: boolean;
  range: { beg: string; end: string; mode: "rolling" | "custom"; granularity: Granularity; weeks: number };
  metrics: {
    items: {
      name: string;
      value: number;
      deltaPercent: number | null;
      inverseDelta: boolean;
      currentPeriod?: string;
      previousPeriod?: string;
      currentLabel?: string;
      previousLabel?: string;
    }[];
    currentPeriod: string;
    previousPeriod: string;
  };
  badges: { p1: number; p2: number; p3: number; p4: number };
  habitTracker: {
    label: string;
    weekBeg: string;
    weekEnd: string;
    trackedCount: number;
    totals: {
      weeklyCompleted: number;
      weeklyRescheduled: number;
      allTimeCompleted: number;
      allTimeRescheduled: number;
    };
    items: HabitTrackerItem[];
    history: { label: string; completed: number; rescheduled: number }[];
    figure: PlotlyFigure;
  };
  insights?: { label?: string; items: InsightItem[] };
  leaderboards?: {
    lastCompletedWeek: {
      label: string;
      beg: string;
      end: string;
      parentProjects: { items: LeaderboardItem[]; totalCompleted: number; figure: PlotlyFigure };
      rootProjects: { items: LeaderboardItem[]; totalCompleted: number; figure: PlotlyFigure };
    };
  };
  figures: DashboardFigures;
  configurableItems?: ConfigurableItem[];
  refreshedAt: string;
  error?: string;
};

export type DashboardStatus = {
  services: ServiceStatus[];
  apiCache: { lastRefresh: string | null };
  activityCache: { path: string; mtime: string | null; size: number | null } | null;
};
