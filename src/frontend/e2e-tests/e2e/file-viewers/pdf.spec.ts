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

// M7 — PDF viewer (pdfrx, stretch). Verify via files-API (download round-trip)
// + title + screenshot artifacts. NOTE: page-nav/download/back coordinates are
// calibrated on the live stack.

// A minimal single-page PDF.
const PDF = Buffer.from(
  "%PDF-1.1\n" +
    "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n" +
    "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n" +
    "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n" +
    "trailer<</Root 1 0 R>>\n%%EOF\n",
  "latin1",
);

test.describe("file-viewers/pdf", () => {
  test("opens a pdf and exercises page navigation", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-pdf-view",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/doc.pdf",
        PDF,
        headers,
        "application/pdf",
      );

      await openFilesTab(page);
      await clickFileRow(page, 0);
      await page.screenshot({ path: "test-results/fv-pdf-page1.png" });

      const { width } = vp(page);
      // Next/previous page buttons in the renderer toolbar (left side).
      await flutterClick(page, 60, 135); // next — calibrate
      await page.waitForTimeout(400);
      await page.screenshot({ path: "test-results/fv-pdf-next.png" });
      await flutterClick(page, 20, 135); // previous — calibrate
      await page.waitForTimeout(400);

      await expect(page).toHaveTitle(/^Klangk - /);
    } finally {
      await cleanup();
    }
  });

  test("download round-trips the pdf bytes", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-pdf-download",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/dl.pdf",
        PDF,
        headers,
        "application/pdf",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      // Download-raw verified via the files API round-trip (the download icon
      // routes here; Flutter web's blob download doesn't surface a Playwright
      // "download" event in CI).
      const dl = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(
          "work/dl.pdf",
        )}`,
        { headers },
      );
      expect(dl.ok()).toBeTruthy();
      expect(Buffer.from(await dl.body()).equals(PDF)).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("close and navigate-away keep clean state", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-pdf-nav",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/nav.pdf",
        PDF,
        headers,
        "application/pdf",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      await flutterClick(page, 12, 105); // back/close
      await page.waitForTimeout(400);
      await page.goto("/");
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
      await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
      await expect(page).toHaveTitle(/^Klangk - /, { timeout: 30_000 });
    } finally {
      await cleanup();
    }
  });
});
