import { expect, test } from "@playwright/test";

const idleSync = { state: "idle", startedAt: null, newEvents: null, error: null };
const runningSync = { ...idleSync, state: "running", startedAt: "2026-11-01T12:00:00Z" };
const finishedSync = { ...runningSync, state: "succeeded", newEvents: 1 };

const home = {
  sync: idleSync,
  range: { beg: "2026-10-26", end: "2026-11-01", mode: "rolling", granularity: "W", weeks: 12 },
  metrics: {
    items: [{ name: "Completed Tasks", value: 349, deltaPercent: null, inverseDelta: false }],
    currentPeriod: "This week", previousPeriod: "Last week"
  },
  badges: { p1: 0, p2: 0, p3: 0, p4: 0 },
  figures: {}, insights: { items: [] }, refreshedAt: "2026-11-01T12:00:00Z"
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) => route.fulfill({ json: {
    configured: true, valid: true, status: "ok", services: [],
    apiCache: { lastRefresh: null }, activityCache: null
  } }));
});

test("a failed background reload keeps the last loaded counts", async ({ page }) => {
  await page.route("**/api/dashboard/home?**", (route) => {
    if (new URL(route.request().url()).searchParams.get("cached_only") === "true") {
      return route.fulfill({ status: 500, contentType: "text/plain", body: "Internal Server Error" });
    }
    return route.fulfill({ json: {
      ...home, sync: { ...runningSync, dashboardError: "Activity history is from saved data." }
    } });
  });
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /multiplication/i })).toHaveCount(0);
  await expect(page.getByText("349", { exact: true })).toBeVisible();
  await expect(page.getByText("Activity history is from saved data. Showing saved data.", { exact: true })).toBeVisible();
  await expect(page.getByText("Could not load dashboard data. Showing the last loaded view.", { exact: true })).toBeVisible();
  await expect(page.getByText("Syncing in the background…", { exact: true })).toHaveCount(0);
  await expect(page.getByText("349", { exact: true })).toBeVisible();
});

test("Refresh data opens Activity History and a failed sync can be retried with saved data visible", async ({ page }) => {
  let dashboardRefreshes = 0;
  let syncRequests = 0;
  let fullStatusReads = 0;
  let progressReads = 0;
  let chartReads = 0;
  let complete = false;
  const lastSavedSync = "2026-11-21T10:20:00Z";
  const newSync = "2026-11-22T14:10:00Z";
  const sync = () => ({
    state: syncRequests === 0 ? "idle" : !complete ? "running" : syncRequests === 1 ? "failed" : "succeeded",
    startedAt: syncRequests === 1 ? "2026-11-22T14:00:00Z" : syncRequests > 1 ? newSync : null,
    newEvents: complete && syncRequests > 1 ? 3 : null,
    error: complete && syncRequests === 1 ? "Todoist is temporarily unavailable. Saved activity is available." : null
  });
  await page.route("**/api/dashboard/home?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    if (params.get("refresh") === "true") dashboardRefreshes++;
    expect(params.has("beg")).toBe(false);
    return route.fulfill({ json: home });
  });
  await page.route("**/api/dashboard/history?**", (route) => {
    chartReads++;
    const params = new URL(route.request().url()).searchParams;
    expect([...params.keys()].sort()).toEqual(["beg", "end"]);
    const fresh = complete && syncRequests > 1;
    return route.fulfill({ json: {
      range: { beg: params.get("beg"), end: params.get("end"), mode: "custom" },
      rangeHasActivity: fresh,
      eventCount: fresh ? 3 : 0,
      figures: {}, refreshedAt: fresh ? newSync : lastSavedSync, timeZone: "Europe/Warsaw"
    } });
  });
  await page.route(/\/api\/activity\/status(?:\?|$)/, (route) => {
    const progress = { sync: sync(), demoMode: false };
    if (new URL(route.request().url()).searchParams.has("sync_only")) {
      progressReads++;
      return route.fulfill({ json: progress });
    }
    fullStatusReads++;
    const fresh = complete && syncRequests > 1;
    return route.fulfill({ json: {
      ...progress,
      today: "2026-11-22",
      timeZone: "Europe/Warsaw",
      firstEventDate: "2026-11-21",
      latestEventDate: fresh ? "2026-11-22" : "2026-11-21",
      coverageStartDate: "2026-11-01",
      lastSyncedAt: fresh ? newSync : lastSavedSync,
      years: { "2026": { coverage: "partial", eventCount: fresh ? 4 : 1 } },
      months: { "2026-11": { coverage: "partial", eventCount: fresh ? 4 : 1 } },
      days: {
        "2026-11-21": { coverage: "covered", eventCount: 1 },
        "2026-11-22": { coverage: "partial", eventCount: fresh ? 3 : 0 }
      },
      weeks: {},
      summary: {
        days: { covered: 18, partial: 1, unverified: 3 },
        weeks: { covered: 2, partial: 2, unverified: 0 }
      }
    } });
  });
  await page.route("**/api/activity/sync", (route) => {
    expect(route.request().method()).toBe("POST");
    syncRequests++;
    complete = false;
    return route.fulfill({ status: 202, json: { sync: sync() } });
  });

  await page.goto("/");
  await expect(page.getByText("349", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Refresh data", exact: true }).click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(page.getByRole("heading", { name: "Activity History" })).toBeVisible();
  await expect(page.getByText("Fetching the latest Todoist activity…")).toBeVisible();
  const summary = page.getByRole("region", { name: "Saved activity" });
  await expect(summary.locator("time")).toHaveAttribute("datetime", lastSavedSync);
  await expect.poll(() => progressReads).toBeGreaterThan(0);
  expect(fullStatusReads).toBe(1);

  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /Overview/ }).click();
  await expect(page.getByText("349", { exact: true })).toBeVisible();
  const pollsBeforeReturn = progressReads;
  await page.getByRole("link", { name: "Refresh data", exact: true }).click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(summary.getByRole("button", { name: "Syncing…" })).toBeDisabled();
  await expect.poll(() => progressReads).toBeGreaterThan(pollsBeforeReturn);
  expect(fullStatusReads).toBe(2);
  expect(syncRequests).toBe(1);

  complete = true;
  await expect(summary.getByRole("alert")).toHaveText("Todoist is temporarily unavailable. Saved activity is available.");
  await expect(summary.locator("time")).toHaveAttribute("datetime", lastSavedSync);
  await expect(page.getByRole("button", { name: /^2026, 1 saved event/ })).toBeVisible();
  await page.getByText("Custom date range", { exact: true }).click();
  await page.getByLabel("From date").fill("2026-11-22");
  await page.getByLabel("To date").fill("2026-11-22");
  await page.getByRole("button", { name: "Load charts" }).click();
  await expect(page.getByText(/No recorded activity in this period/)).toBeVisible();
  expect(chartReads).toBe(1);
  const calendarReadsBeforeRetry = fullStatusReads;
  expect(calendarReadsBeforeRetry).toBe(2);
  await summary.getByRole("button", { name: "Retry sync" }).click();
  await expect.poll(() => syncRequests).toBe(2);
  complete = true;
  await expect(summary.getByText("Sync complete. 3 new events.")).toBeVisible();
  await expect(summary.locator("time")).toHaveAttribute("datetime", newSync);
  await expect(page.getByRole("button", { name: /^2026, 4 saved events/ })).toBeVisible();
  await expect(page.getByText(/No recorded activity in this period/)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "2026-11-22 → 2026-11-22" })).toBeVisible();
  expect(chartReads).toBe(2);
  expect(fullStatusReads).toBe(calendarReadsBeforeRetry + 1);
  await page.reload();
  await expect(summary.locator("time")).toHaveAttribute("datetime", newSync);
  expect(syncRequests).toBe(2);
  expect(dashboardRefreshes).toBe(0);
});

test("starting sync while the first status read is pending consumes the refresh intent", async ({ page }) => {
  let calendarReads = 0;
  let syncRequests = 0;
  let releaseInitialRead!: () => void;
  const pending = new Promise<void>((resolve) => { releaseInitialRead = resolve; });
  const startedAt = "2026-11-22T14:00:00Z";
  const finishedAt = "2026-11-22T14:00:01Z";
  const running = { state: "running", error: null, startedAt, newEvents: null };
  const completed = { ...running, state: "succeeded", newEvents: 0 };
  await page.route("**/api/activity/status**", async (route) => {
    if (new URL(route.request().url()).searchParams.has("sync_only")) {
      return route.fulfill({ json: { sync: completed, demoMode: false } });
    }
    if (++calendarReads === 1) {
      await pending;
      return route.abort().catch(() => {});
    }
    return route.fulfill({ json: {
      sync: completed, demoMode: false, today: "2026-11-22", timeZone: "Europe/Warsaw",
      firstEventDate: null, latestEventDate: null, coverageStartDate: null, lastSyncedAt: finishedAt,
      years: {}, months: {}, days: {}, weeks: {},
      summary: {
        days: { covered: 0, partial: 0, unverified: 0 },
        weeks: { covered: 0, partial: 0, unverified: 0 }
      }
    } });
  });
  await page.route("**/api/activity/sync", (route) => {
    syncRequests++;
    return route.fulfill({ status: 202, json: { sync: running } });
  });

  try {
    await page.goto("/history?sync=1&source=overview#history-browse-title");
    await expect.poll(() => calendarReads).toBe(1);
    await expect(page.getByText("Custom date range", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Sync now", exact: true }).click();
    await expect(page).toHaveURL(/\/history\?source=overview#history-browse-title$/);
    await expect(page.getByText("Sync complete. 0 new events.")).toBeVisible();
    expect(calendarReads).toBe(2);
    expect(syncRequests).toBe(1);
  } finally {
    releaseInitialRead();
  }
});

test("Overview links to the dedicated timeline from the sidebar", async ({ page }) => {
  await page.route("**/api/dashboard/home?**", (route) => route.fulfill({ json: home }));
  await page.route("**/api/dashboard/project-timeline?**", (route) => route.fulfill({ json: {
    range: { beg: "2026-11-01", end: "2026-11-22" }, parents: []
  } }));
  await page.goto("/");
  await expect(page.getByText("349", { exact: true })).toBeVisible();
  const jumps = page.getByRole("navigation", { name: "Jump to sections" });
  await expect(jumps.getByRole("link", { name: "Projects", exact: true })).toHaveCount(1);
  await expect(jumps.getByRole("link", { name: "Projects", exact: true })).toHaveAttribute("href", "#projects");
  await expect(page.getByRole("link", { name: /Open timeline/ })).toHaveCount(0);
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /Project Timeline/ }).click();
  await expect(page.getByRole("heading", { name: "Project Timeline", exact: true })).toBeVisible();
});

test("a background sync updates the visible counts when it finishes", async ({ page }) => {
  let reads = 0;
  let followupMode: string | null = null;
  await page.route("**/api/dashboard/home?**", (route) => {
    reads++;
    const updated = reads > 1;
    if (updated) followupMode = new URL(route.request().url()).searchParams.get("cached_only");
    return route.fulfill({ json: {
      ...home, sync: updated ? finishedSync : runningSync,
      metrics: { ...home.metrics, items: [{ ...home.metrics.items[0], value: updated ? 350 : 349 }] }
    } });
  });
  await page.goto("/");
  await expect(page.getByText("349", { exact: true })).toBeVisible();
  await expect(page.getByText("Refreshing dashboard data")).toHaveCount(0);
  await expect(page.getByText("350", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Syncing in the background…", { exact: true })).toHaveCount(0);
  expect(followupMode).toBe("true");
});

test("Autoscale survives a background sync and a new period resets the chart", async ({ page }) => {
  const initialRange = ["2026-10-01", "2026-11-01"];
  const syncedRange = ["2026-10-02", "2026-11-02"];
  const selectedRange = ["2026-05-04", "2026-11-02"];
  let finishSync!: () => void;
  const sync = new Promise<void>((resolve) => { finishSync = resolve; });

  await page.route("**/api/dashboard/home?**", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const selected = params.get("weeks") === "26";
    const refreshed = params.get("cached_only") === "true";
    if (refreshed) await sync;
    const range = selected ? selectedRange : refreshed ? syncedRange : initialRange;
    return route.fulfill({ json: {
      ...home,
      sync: refreshed ? finishedSync : runningSync,
      range: { ...home.range, weeks: selected ? 26 : 12, beg: range[0], end: range[1] },
      figures: {
        completedTasksPeriodically: {
          data: [{
            type: "scatter",
            mode: "lines",
            x: ["2024-06-01", "2026-06-01", "2026-10-26"],
            y: [1, 2, refreshed ? 4 : 3]
          }],
          layout: { xaxis: { type: "date", range } }
        }
      }
    } });
  });
  const chart = page.getByRole("region", { name: "Periodically Completed Tasks Per Project", exact: true });
  const plot = chart.locator(".js-plotly-plot");
  const chartState = () => plot.evaluateAll(([node]) => {
    const graph = node as (HTMLElement & {
      data?: { y: number[] }[];
      _fullLayout?: { xaxis: { autorange: boolean; range: string[] } };
    }) | undefined;
    const axis = graph?._fullLayout?.xaxis;
    if (!axis) return null;
    return {
      auto: axis.autorange,
      range: axis.range.map((date) => date.slice(0, 10)),
      lastCount: graph?.data?.[0]?.y.at(-1)
    };
  });

  try {
    const backgroundRead = page.waitForRequest((request) =>
      request.url().includes("/api/dashboard/home?") &&
      new URL(request.url()).searchParams.get("cached_only") === "true"
    );
    await page.goto("/");
    await backgroundRead;
    const autoscale = chart.locator('[data-title="Autoscale"]');
    await expect(autoscale).toBeAttached();
    await plot.hover();
    await expect.poll(chartState).toMatchObject({ auto: false, range: initialRange, lastCount: 3 });
    await autoscale.click();
    await expect.poll(chartState).toMatchObject({ auto: true, lastCount: 3 });
    const expandedRange = (await chartState())!.range;
    expect(Date.parse(expandedRange[0])).toBeLessThanOrEqual(Date.UTC(2024, 5, 1));

    finishSync();
    await expect.poll(chartState).toMatchObject({ auto: true, range: expandedRange, lastCount: 4 });

    await page.getByLabel("Range", { exact: true }).selectOption("26");
    await expect.poll(chartState).toMatchObject({ auto: false, range: selectedRange, lastCount: 4 });
  } finally {
    finishSync();
  }
});

test("idle Overview keeps its snapshot when time passes and the window regains focus", async ({ page }) => {
  await page.clock.install();
  let reads = 0;
  await page.route("**/api/dashboard/home?**", (route) => {
    reads++;
    return route.fulfill({ json: home });
  });
  await page.goto("/");
  await expect(page.getByText("349", { exact: true })).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.clock.fastForward(65_000);
  await expect(page.getByText("349", { exact: true })).toBeVisible();
  expect(reads).toBe(1);
});

test("hierarchy playback requests the range used by the displayed dashboard", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-11-22T12:00:00Z"));
  const figure = { data: [], layout: {} };
  const playbackRequests: URLSearchParams[] = [];
  const historyRequests: URLSearchParams[] = [];
  await page.route("**/api/dashboard/home?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    expect(params.has("beg")).toBe(false);
    return route.fulfill({ json: {
      ...home,
      figures: { activeProjectHierarchy: figure }
    } });
  });
  await page.route("**/api/dashboard/history?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    historyRequests.push(params);
    return route.fulfill({ json: {
      range: { beg: params.get("beg"), end: params.get("end"), mode: "custom" },
      figures: { activeProjectHierarchy: figure },
      rangeHasActivity: true, eventCount: 1, refreshedAt: home.refreshedAt, timeZone: "UTC"
    } });
  });
  await page.route("**/api/dashboard/hierarchy-playback?**", (route) => {
    playbackRequests.push(new URL(route.request().url()).searchParams);
    return route.fulfill({ json: { frames: [{ date: "2026-11-01", figure }] } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Play history" }).click();
  await expect.poll(() => playbackRequests.length).toBe(1);
  expect(playbackRequests[0].get("weeks")).toBe("12");
  expect(playbackRequests[0].has("beg")).toBe(false);

  await page.getByRole("link", { name: /Activity History/ }).first().click();
  await page.getByText("Custom date range", { exact: true }).click();
  await page.getByLabel("From date").fill("2026-10-01");
  await page.getByLabel("To date").fill("2026-10-15");
  expect(historyRequests).toHaveLength(0);
  await page.getByRole("button", { name: "Load charts" }).click();
  await expect(page.getByRole("heading", { name: "2026-10-01 → 2026-10-15" })).toBeVisible();
  expect(historyRequests).toHaveLength(1);
  expect(Object.fromEntries(historyRequests[0])).toEqual({ beg: "2026-10-01", end: "2026-10-15" });
  expect(playbackRequests).toHaveLength(1);
  await page.getByRole("button", { name: "Play history" }).click();
  await expect.poll(() => playbackRequests.length).toBe(2);
  expect(playbackRequests[1].get("beg")).toBe("2026-10-01");
  expect(playbackRequests[1].get("end")).toBe("2026-10-15");
  expect(playbackRequests[1].has("weeks")).toBe(false);
  expect(playbackRequests[1].get("cached_only")).toBe("true");
});
