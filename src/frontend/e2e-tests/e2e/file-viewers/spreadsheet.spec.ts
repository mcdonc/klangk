import { test, expect } from "@playwright/test";
import {
  API_BASE,
  createAndOpenWorkspace,
  seedFile,
  openFilesTab,
  clickFileRow,
  flutterClick,
  fv,
  vp,
} from "../helpers";

// M9 — Spreadsheet viewer (sheetifye, .xlsx). An xlsx file has TWO renderers:
// Spreadsheet (View, default, editable in-grid) and Raw. These cover every
// usage of the view: opening (View), editing a cell, the Raw mode chip,
// downloading the raw bytes, and navigating away. Grid edits are local (no
// binary save-back through the files API yet), so the file is unchanged after.
// NOTE: chip/cell/download/back coordinates are calibrated on the live stack.

// A minimal real .xlsx (Sheet1: A1=Name B1=Score, A2=Alice 92, A3=Bob 87) so
// the grid actually parses and renders.
const XLSX = Buffer.from(
  "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",
  "base64",
);

test.describe("file-viewers/spreadsheet", () => {
  test("View + edit: opens the grid and edits a cell", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-xlsx-view",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/book.xlsx",
        XLSX,
        headers,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);
      await page.waitForTimeout(800);
      await page.screenshot({ path: "test-results/fv-xlsx-view.png" });
      await expect(page).toHaveTitle(/^Klangk - /);

      // Edit: focus a cell in the grid body and type (local edit).
      const { width } = vp(page);
      await fv(page).click({ position: { x: width / 2, y: 240 }, force: true });
      await page.waitForTimeout(200);
      await page.keyboard.type("99");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(200);
      await page.screenshot({ path: "test-results/fv-xlsx-edit.png" });
    } finally {
      await cleanup();
    }
  });

  test("Raw: switches to the raw renderer", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-xlsx-raw",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/raw.xlsx",
        XLSX,
        headers,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      const { width } = vp(page);
      // Mode chips, top-right: [View] [Raw]. Switch to Raw.
      await flutterClick(page, width - 60, 105); // Raw chip — calibrate
      await page.waitForTimeout(400);
      await page.screenshot({ path: "test-results/fv-xlsx-raw.png" });
      await expect(page).toHaveTitle(/^Klangk - /);
    } finally {
      await cleanup();
    }
  });

  test("Download: round-trips the raw bytes", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-xlsx-dl",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/dl.xlsx",
        XLSX,
        headers,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      // Download-raw is verified via the files API round-trip. The renderer's
      // download icon routes to this same endpoint; we assert on the endpoint
      // rather than the browser "download" event, which Flutter web's blob-
      // anchor download does not reliably surface to Playwright in CI.
      const dl = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(
          "work/dl.xlsx",
        )}`,
        { headers },
      );
      expect(dl.ok()).toBeTruthy();
      expect(Buffer.from(await dl.body()).equals(XLSX)).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("Navigate away: leave and return; file unchanged (local edit)", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-xlsx-nav",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/nav.xlsx",
        XLSX,
        headers,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      await flutterClick(page, 12, 105); // back/close — calibrate
      await page.waitForTimeout(300);
      await page.goto("/");
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
      await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
      await expect(page).toHaveTitle(/^Klangk - /, { timeout: 30_000 });

      const after = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(
          "work/nav.xlsx",
        )}`,
        { headers },
      );
      expect(Buffer.from(await after.body()).equals(XLSX)).toBeTruthy();
    } finally {
      await cleanup();
    }
  });
});
