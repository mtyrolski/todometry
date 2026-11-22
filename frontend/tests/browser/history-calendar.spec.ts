import { expect, test, type Page } from "@playwright/test";

const sync = { state: "idle", error: null, startedAt: null, newEvents: null };
const legacyStatus = {
  today: "2026-11-20",
  timeZone: "Europe/Warsaw",
  firstEventDate: "2024-02-26",
  latestEventDate: "2026-11-20",
  coverageStartDate: null,
  lastSyncedAt: null,
  years: {
    "2024": { coverage: "unverified", eventCount: 5 },
    "2025": { coverage: "unverified", eventCount: 0 },
    "2026": { coverage: "unverified", eventCount: 11 }
  },
  months: {
    "2024-02": { coverage: "unverified", eventCount: 4 },
    "2024-03": { coverage: "unverified", eventCount: 1 },
    "2024-12": { coverage: "unverified", eventCount: 0 },
    "2026-11": { coverage: "unverified", eventCount: 11 }
  },
  days: {},
  weeks: {},
  summary: {
    days: { covered: 0, partial: 0, unverified: 999 },
    weeks: { covered: 0, partial: 0, unverified: 143 }
  },
  sync,
  demoMode: false
};

async function mockSavedCharts(page: Page, beforeResponse?: (params: URLSearchParams) => Promise<void> | undefined) {
  const requests: URLSearchParams[] = [];
  await page.route("**/api/dashboard/history?**", async (route) => {
    expect(route.request().method()).toBe("GET");
    const params = new URL(route.request().url()).searchParams;
    requests.push(params);
    await beforeResponse?.(params);
    return route.fulfill({ json: {
      range: { beg: params.get("beg"), end: params.get("end"), mode: "custom" },
      rangeHasActivity: true,
      figures: {},
      refreshedAt: "2026-11-20T12:00:00Z",
      timeZone: legacyStatus.timeZone
    } });
  });
  return requests;
}

test("browse saved history by year, month, week and day without syncing", async ({ page }) => {
  const chartRequests = await mockSavedCharts(page);
  const unexpectedRequests: string[] = [];
  await page.route(/\/api\/(?:dashboard\/(?:home|progress|hierarchy-playback)|activity\/sync)(?:\?|$)/, (route) => {
    unexpectedRequests.push(new URL(route.request().url()).pathname);
    return route.abort();
  });
  await page.route("**/api/activity/status**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    if (params.has("sync_only")) return route.fulfill({ json: { sync, demoMode: false } });
    return route.fulfill({ json: {
      ...legacyStatus,
      days: params.get("month") === "2024-02" ? {
        "2024-02-26": { coverage: "unverified", eventCount: 3 },
        "2024-02-29": { coverage: "unverified", eventCount: 1 },
        "2024-03-01": { coverage: "unverified", eventCount: 1 }
      } : {
        "2026-11-20": { coverage: "unverified", eventCount: 11 }
      }
    } });
  });
  await page.goto("/history");
  const browser = page.getByRole("region", { name: "Browse history" });
  const charts = page.getByRole("region", { name: "History results" });

  let expectedChartReads = 0;
  const expectRange = async (beg: string, end: string) => {
    await expect(charts.getByRole("heading", { name: `${beg} → ${end}` })).toBeVisible();
    expect(chartRequests).toHaveLength(++expectedChartReads);
    expect(Object.fromEntries(chartRequests.at(-1) ?? [])).toEqual({ beg, end });
  };

  await expect(page.getByText(/saved before sync coverage was tracked/i)).toBeVisible();
  await expect(browser.getByRole("heading", { name: "Years" })).toBeVisible();
  await expect(browser.getByRole("button", { name: /^2026, 11 saved events/ })).toBeVisible();
  await expect(browser.getByRole("button", { name: /^2025, No saved events/i })).toBeVisible();
  expect(chartRequests).toHaveLength(0);

  await browser.getByRole("button", { name: /^2024, 5 saved events/ }).click();
  await expectRange("2024-01-01", "2024-12-31");
  await browser.getByRole("button", { name: /^February 2024, 4 saved events/ }).click();
  await expectRange("2024-02-01", "2024-02-29");
  await browser.getByRole("link", { name: "Go to charts" }).click();
  await expect(page).toHaveURL(/#history-results$/);

  const calendar = browser.getByRole("table", { name: "February 2024" });
  await calendar.getByRole("button", { name: /^Week of Feb 26, 2024/ }).click();
  await expectRange("2024-02-26", "2024-03-03");
  await expect(browser.getByRole("button", { name: /^Friday, March 1, 2024, 1 saved event/ })).toBeVisible();
  await browser.getByRole("button", { name: /^Thursday, February 29, 2024, 1 saved event/ }).click();
  await expectRange("2024-02-29", "2024-02-29");
  await browser.getByRole("button", { name: "Whole week", exact: true }).click();
  await expectRange("2024-02-26", "2024-03-03");

  const breadcrumbs = browser.getByRole("navigation", { name: "History levels" });
  await breadcrumbs.getByRole("button", { name: "February", exact: true }).click();
  await expectRange("2024-02-01", "2024-02-29");
  await breadcrumbs.getByRole("button", { name: "2024", exact: true }).click();
  await expectRange("2024-01-01", "2024-12-31");
  await breadcrumbs.getByRole("button", { name: "Years", exact: true }).click();
  await expect(charts).toHaveCount(0);

  await browser.getByRole("button", { name: /^2024, 5 saved events/ }).click();
  await expectRange("2024-01-01", "2024-12-31");
  await browser.getByRole("button", { name: /^December 2024,/ }).click();
  await expectRange("2024-12-01", "2024-12-31");
  const december = browser.getByRole("table", { name: "December 2024" });
  await december.getByRole("button", { name: /^Week of Dec 30, 2024/ }).click();
  await expectRange("2024-12-30", "2025-01-05");

  await browser.getByRole("button", { name: "This week", exact: true }).click();
  await expectRange("2026-11-16", "2026-11-20");
  expect(unexpectedRequests).toEqual([]);
});

test("a delayed week response leaves the newer selection visible", async ({ page }) => {
  const olderBeg = "2026-11-16";
  const newerBeg = "2026-11-09";
  let calendarReads = 0;
  let releaseOlder!: () => void;
  const holdOlder = new Promise<void>((resolve) => { releaseOlder = resolve; });
  const chartRequests = await mockSavedCharts(page, (params) => params.get("beg") === olderBeg ? holdOlder : undefined);
  await page.route("**/api/activity/status**", (route) => {
    if (new URL(route.request().url()).searchParams.has("sync_only")) {
      return route.fulfill({ json: { sync, demoMode: false } });
    }
    calendarReads++;
    return route.fulfill({ json: legacyStatus });
  });
  await page.addInitScript((beg) => {
    const fetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (typeof input === "string") {
        const url = new URL(input, window.location.href);
        if (url.pathname === "/api/dashboard/history" && url.searchParams.get("beg") === beg) {
          // Deliver the earlier response even after the chart read was canceled.
          return fetch(input, { ...init, signal: undefined });
        }
      }
      return fetch(input, init);
    };
  }, olderBeg);

  try {
    await page.goto("/history");
    const browser = page.getByRole("region", { name: "Browse history" });
    await browser.getByRole("button", { name: /^2026, 11 saved events/ }).waitFor();
    await browser.getByRole("button", { name: "This week", exact: true }).click();
    await expect.poll(() => chartRequests.map((params) => params.get("beg"))).toEqual([olderBeg]);
    await browser.getByRole("button", { name: "Previous week", exact: true }).click();

    const charts = page.getByRole("region", { name: "History results" });
    const currentWeek = charts.getByRole("heading", { name: `${newerBeg} → 2026-11-15` });
    await expect(currentWeek).toBeVisible();
    const olderResponse = page.waitForResponse((response) =>
      response.url().includes("/api/dashboard/history?") && new URL(response.url()).searchParams.get("beg") === olderBeg
    );
    releaseOlder();
    expect(await (await olderResponse).finished()).toBeNull();
    await expect(currentWeek).toBeVisible();
    await expect(charts.getByRole("heading", { name: `${olderBeg} → 2026-11-20` })).toHaveCount(0);
    expect(chartRequests.map((params) => Object.fromEntries(params))).toEqual([
      { beg: olderBeg, end: "2026-11-20" }, { beg: newerBeg, end: "2026-11-15" }
    ]);
    expect(calendarReads).toBe(1);
  } finally {
    releaseOlder();
  }
});

test("the calendar distinguishes saved activity, confirmed quiet days and gaps", async ({ page }) => {
  await mockSavedCharts(page);
  await page.route("**/api/activity/status**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    if (params.has("sync_only")) return route.fulfill({ json: { sync, demoMode: false } });
    return route.fulfill({ json: {
      ...legacyStatus,
      coverageStartDate: "2026-10-16",
      lastSyncedAt: "2026-11-20T04:10:00Z",
      days: {
        "2026-11-12": { coverage: "unverified", eventCount: 1 },
        "2026-11-13": { coverage: "covered", eventCount: 0 },
        "2026-11-14": { coverage: "unverified", eventCount: 0 },
        "2026-11-20": { coverage: "partial", eventCount: 2 }
      },
      weeks: {
        "2026-11-09": "unverified", "2026-11-16": "partial"
      },
      summary: {
        days: { covered: 41, partial: 2, unverified: 28 },
        weeks: { covered: 5, partial: 2, unverified: 4 }
      }
    } });
  });
  await page.goto("/history");
  const saved = page.getByRole("region", { name: "Saved activity" });
  await expect(saved.locator("time")).toHaveAttribute("datetime", "2026-11-20T04:10:00Z");
  await saved.getByText("Sync coverage", { exact: true }).click();
  await expect(saved.getByText("Oct 16, 2026", { exact: true })).toBeVisible();
  await expect(saved.getByText("41 of 71", { exact: true })).toBeVisible();
  await expect(saved.getByText("5 of 11", { exact: true })).toBeVisible();

  const browser = page.getByRole("region", { name: "Browse history" });
  await browser.getByRole("button", { name: /^2026,/ }).click();
  await browser.getByRole("button", { name: /^November 2026,/ }).click();
  const calendar = browser.getByRole("table", { name: "November 2026" });
  await expect(calendar.getByRole("button", { name: /^Thursday, November 12, 2026, 1 saved event/ })).toBeVisible();
  await expect(calendar.getByRole("button", { name: /^Friday, November 13, 2026, No activity, checked/ })).toBeVisible();
  await expect(calendar.getByRole("button", { name: /^Saturday, November 14, 2026, No saved events/ })).toBeVisible();
  await expect(calendar.getByRole("button", { name: /^Week of Nov 16, 2026, 2 saved events, partly checked/ })).toBeVisible();
  await expect(calendar.getByRole("button", { name: /unverified/i })).toHaveCount(0);
  await expect(calendar.getByText("?", { exact: true })).toHaveCount(0);
});

test("a custom date range still loads saved charts when the sync status is unavailable", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-11-20T12:00:00Z"));
  await page.route("**/api/activity/status**", (route) => route.abort());
  const chartRequests = await mockSavedCharts(page);
  await page.goto("/history");
  const from = page.getByLabel("From date");
  const to = page.getByLabel("To date");
  const load = page.getByRole("button", { name: "Load charts" });

  await expect(from).toBeHidden();
  await page.getByText("Custom date range", { exact: true }).click();
  await expect(load).toBeDisabled();
  await from.fill("2010-01-01");
  await to.fill("2024-03-03");
  await expect(load).toBeDisabled();
  await expect(page.getByText("Choose a range of up to 260 weeks.")).toBeVisible();

  await from.fill("2024-02-29");
  await load.click();
  await expect(page.getByRole("heading", { name: "2024-02-29 → 2024-03-03" })).toBeVisible();
  expect(chartRequests).toHaveLength(1);
  expect(Object.fromEntries(chartRequests[0])).toEqual({ beg: "2024-02-29", end: "2024-03-03" });
});
