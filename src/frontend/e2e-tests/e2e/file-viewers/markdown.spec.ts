import { test, expect } from "@playwright/test";
import {
  API_BASE,
  createAndOpenWorkspace,
  seedFile,
  openFilesTab,
  clickFileRow,
  flutterClick,
  vp,
} from "../helpers";

// M3 — Markdown viewer (View) with Raw also available.
//
// Conventions: files-API + title + download round-trip + screenshot artifacts
// (Flutter canvas has no widget DOM). A .md file now has TWO renderers
// (Markdown View + Raw), so the mode switcher chips are shown.
// NOTE: chrome-row chip/button coordinates are calibrated on the live stack.

const MD = `# Heading One

Some **bold** and a [link](https://example.test).

- alpha
- beta
`;

test.describe("file-viewers/markdown", () => {
  test("renders markdown by default and toggles to Raw", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-md-view",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/readme.md",
        MD,
        headers,
        "text/markdown",
      );
      // Backing content via API.
      const api = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/readme.md`,
        { headers },
      );
      expect((await api.json()).content).toContain("# Heading One");

      await openFilesTab(page);
      await clickFileRow(page, 0);
      // Default = rendered Markdown.
      await page.screenshot({ path: "test-results/fv-md-rendered.png" });

      // Toggle to Raw via the mode chip (right side of the chrome row).
      const { width } = vp(page);
      await flutterClick(page, width - 70, 105); // "Raw" chip — calibrate
      await page.waitForTimeout(400);
      await page.screenshot({ path: "test-results/fv-md-raw.png" });

      await expect(page).toHaveTitle(/^Klangk - /);
    } finally {
      await cleanup();
    }
  });

  test("download round-trips the markdown source", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-md-download",
    );
    try {
      await seedFile(request, workspaceId, "work/dl.md", MD, headers);
      await openFilesTab(page);
      await clickFileRow(page, 0);

      // Download-raw verified via the files API round-trip (the download icon
      // routes here; Flutter web's blob download doesn't surface a Playwright
      // "download" event in CI).
      const dl = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(
          "work/dl.md",
        )}`,
        { headers },
      );
      expect(dl.ok()).toBeTruthy();
      expect(Buffer.from(await dl.body()).toString()).toContain("# Heading One");
    } finally {
      await cleanup();
    }
  });

  test("close, tab-switch, and leave keep clean state", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-md-nav",
    );
    try {
      await seedFile(request, workspaceId, "work/nav.md", MD, headers);
      await openFilesTab(page);
      await clickFileRow(page, 0);

      // Close → back to list.
      await flutterClick(page, 12, 105);
      await page.waitForTimeout(400);

      // Re-open, switch to Terminal tab and back to Files.
      await clickFileRow(page, 0);
      const { width } = vp(page);
      const tabWidth = width / 5;
      await flutterClick(page, tabWidth / 2, 76);
      await page.waitForTimeout(400);
      await openFilesTab(page);
      await page.screenshot({ path: "test-results/fv-md-after-tabs.png" });

      // Leave the workspace entirely, then return.
      await page.goto("/");
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
      await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
      await expect(page).toHaveTitle(/^Klangk - /, { timeout: 30_000 });
    } finally {
      await cleanup();
    }
  });
});
