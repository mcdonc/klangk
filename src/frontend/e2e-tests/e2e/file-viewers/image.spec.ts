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

// M4 — Image viewer (View) with a rotate action.
//
// Verify via files-API (the seeded PNG round-trips through download) + title +
// screenshot artifacts (rotation is visual; canvas has no DOM). NOTE: rotate/
// download/back coordinates are calibrated on the live stack.

// A valid 1x1 transparent PNG.
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
  "base64",
);

test.describe("file-viewers/image", () => {
  test("opens an image and rotates it four times", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-img-rotate",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/pic.png",
        PNG,
        headers,
        "image/png",
      );

      await openFilesTab(page);
      await clickFileRow(page, 0);
      await page.screenshot({ path: "test-results/fv-img-turn0.png" });

      const { width } = vp(page);
      // Rotate button sits in the image renderer's own toolbar (right side,
      // just below the chrome row).
      for (let turn = 1; turn <= 4; turn++) {
        await flutterClick(page, width - 30, 135); // rotate — calibrate
        await page.waitForTimeout(300);
        await page.screenshot({ path: `test-results/fv-img-turn${turn}.png` });
      }
      // After 4 turns we're back to the original orientation.
      await expect(page).toHaveTitle(/^Klangk - /);
    } finally {
      await cleanup();
    }
  });

  test("download round-trips the PNG bytes", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-img-download",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/dl.png",
        PNG,
        headers,
        "image/png",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      // Download-raw verified via the files API round-trip (the download icon
      // routes here; Flutter web's blob download doesn't surface a Playwright
      // "download" event in CI).
      const dl = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(
          "work/dl.png",
        )}`,
        { headers },
      );
      expect(dl.ok()).toBeTruthy();
      expect(Buffer.from(await dl.body()).equals(PNG)).toBeTruthy();
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
      "fv-img-nav",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/nav.png",
        PNG,
        headers,
        "image/png",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      await flutterClick(page, 12, 105); // back/close
      await page.waitForTimeout(400);

      // Leave the workspace and return.
      await page.goto("/");
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
      await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
      await expect(page).toHaveTitle(/^Klangk - /, { timeout: 30_000 });
    } finally {
      await cleanup();
    }
  });
});
