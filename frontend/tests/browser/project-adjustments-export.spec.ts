import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("initial desktop setup assigns a project to an archived group and can be edited later", async ({ page }) => {
  await page.route("**/api/**", (route) => route.fulfill({ json: {
    status: "ok", configured: true, valid: true, services: [],
    apiCache: { lastRefresh: null }, activityCache: null,
  } }));
  let connected = false;
  await page.route("**/api/admin/api_token", (route) => {
    if (route.request().method() === "POST") connected = true;
    return route.fulfill({ json: { configured: connected } });
  });
  await page.route("**/api/dashboard/home?**", (route) => route.fulfill({ json: {
    sync: { state: "idle", startedAt: null, newEvents: null, error: null },
    range: { beg: "2026-11-01", end: "2026-11-15", mode: "rolling", granularity: "W", weeks: 12 },
    metrics: { items: [], currentPeriod: "", previousPeriod: "" },
    badges: { p1: 0, p2: 0, p3: 0, p4: 0 }, figures: {}, insights: { items: [] },
    refreshedAt: "2026-11-15T12:00:00Z",
  } }));
  const projects = [
    { id: "archive-a", name: "Archive", path: "Archive", archived: true },
    { id: "archive-b", name: "Archive", path: "Archive", archived: true },
    { id: "work", name: "Work", path: "Work", archived: false },
    { id: "child", name: "Current", path: "Work / Current", archived: false },
  ];
  type Rule = { targetId: string; targetName: string };
  type Patch = { projectMappings: Record<string, Rule | null> };
  const work = { targetId: "work", targetName: "Work" };
  const unavailable = { targetId: "missing", targetName: "Saved group" };
  const attempts: Patch[] = [];
  const projectMappings: Record<string, Rule> = { "archive-a": work, unavailable };
  await page.route("**/api/admin/project_adjustments**", (route) => {
    if (route.request().method() === "PUT") {
      const file = new URL(route.request().url()).searchParams.get("file");
      expect(file).toBe("projects.py");
      const patch: Patch = route.request().postDataJSON();
      attempts.push(patch);
      if (attempts.length === 1) {
        return route.fulfill({ status: 503, json: { detail: "Could not save project adjustments." } });
      }
      if (attempts.length === 2) {
        return route.fulfill({ json: { saved: false, projectMappings } });
      }
      for (const [id, rule] of Object.entries(patch.projectMappings)) {
        if (rule) projectMappings[id] = rule;
        else delete projectMappings[id];
      }
      return route.fulfill({ json: { saved: true, file, projectMappings } });
    }
    return route.fulfill({ json: {
      files: ["projects.py"], selectedFile: "projects.py", mappings: { Archive: "Work" },
      archivedParentProjects: [], activeRootProjects: ["Work"],
      archivedProjects: ["Archive"], sourceProjects: ["Archive"],
      projects, projectMappings,
    } });
  });

  await page.goto("/");
  await page.getByLabel("API token", { exact: true }).fill("token-for-this-test-only");
  await page.getByRole("button", { name: "Validate & Save" }).click();
  const dialog = page.getByRole("dialog", { name: "Project mapping" });
  await expect(dialog.getByRole("heading", { name: "Choose main projects" })).toBeVisible();
  const search = dialog.getByRole("searchbox", { name: "Find a main project" });
  await search.fill("archive");
  await expect(dialog.getByRole("checkbox")).toHaveCount(2);
  await dialog.getByRole("checkbox", { name: "Archive · archived · ID archive-b" }).check();
  await search.fill("");
  await dialog.getByRole("checkbox", { name: "Work", exact: true }).check();
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(dialog.getByRole("heading", { name: "Archive · archived · ID archive-a" })).toBeVisible();
  await expect(dialog.getByLabel("Group under")).toHaveValue("work");
  await dialog.getByLabel("Group under").selectOption("archive-b");
  await dialog.getByRole("button", { name: "Skip project" }).click();
  await expect(dialog.getByRole("heading", { name: "Work / Current" })).toBeVisible();
  const group = { targetId: "archive-b", targetName: "Archive" };
  await dialog.getByLabel("Group under").selectOption(group.targetId);
  await dialog.getByRole("button", { name: "Save project mapping" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("Could not save project adjustments.");
  await expect(dialog.getByLabel("Group under")).toHaveValue(group.targetId);
  const unconfirmed = page.waitForResponse((response) =>
    response.request().method() === "PUT" && response.url().includes("/api/admin/project_adjustments")
  );
  await dialog.getByRole("button", { name: "Save project mapping" }).click();
  expect((await unconfirmed).ok()).toBe(true);
  await expect(dialog.getByRole("alert")).toHaveText("Could not confirm project mapping was saved.");
  await expect(dialog.getByLabel("Group under")).toHaveValue(group.targetId);
  await dialog.getByRole("button", { name: "Save project mapping" }).click();
  await expect(dialog).toHaveCount(0);
  const expected = { projectMappings: { "archive-b": group, child: group, work } };
  expect(attempts).toEqual([expected, expected, expected]);
  expect(projectMappings).toEqual({ "archive-a": work, unavailable, ...expected.projectMappings });

  await page.goto("/project-adjustments");
  await page.getByRole("button", { name: "Guided setup" }).click();
  const setup = page.getByRole("region", { name: "Project mapping setup" });
  await expect(setup.getByRole("checkbox", { name: "Archive · archived · ID archive-b" })).toBeChecked();
  await setup.getByRole("button", { name: "Continue" }).click();
  await expect(setup.getByLabel("Group under")).toHaveValue("work");
  await setup.getByLabel("Go to project").selectOption("child");
  await expect(setup.getByLabel("Group under")).toHaveValue(group.targetId);
  await setup.getByLabel("Group under").selectOption("work");
  await setup.getByRole("button", { name: "Assign and next" }).click();
  await setup.getByRole("button", { name: "Save project mapping" }).click();
  await expect(setup).toHaveCount(0);
  expect(attempts[3]).toEqual({ projectMappings: { child: work } });
});

test("exports saved manual and automatic project assignments for Android", async ({ page }) => {
  await page.route("**/api/**", (route) => route.fulfill({ json: { status: "ok", configured: true } }));
  await page.route("**/api/admin/project_adjustments**", (route) => route.fulfill({ json: {
    files: ["projects.py"], selectedFile: "projects.py",
    mappings: { Old: "Work", Shared: "Personal" },
    automaticMappings: { Child: "Work", Shared: "Work" },
    archivedParentProjects: ["Archive"], activeRootProjects: ["Work", "Personal"],
    archivedProjects: ["Old", "Child", "Archive"],
    sourceProjects: ["Old", "Child", "Archive"],
  } }));
  await page.goto("/project-adjustments");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export saved mappings for Android" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("todoist-project-adjustments.json");
  expect(JSON.parse(await readFile(await download.path(), "utf8"))).toEqual({
    format: "todoist-assistant-project-adjustments-v1",
    mappings: { Old: "Work", Child: "Work", Shared: "Personal" },
    archivedParents: ["Archive"],
  });
});
