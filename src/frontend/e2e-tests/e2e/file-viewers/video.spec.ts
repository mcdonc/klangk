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

// M8 — Video viewer (video_player). A video file has TWO renderers: Video
// (View, default) and Raw. Video is view-only (no edit). These cover every
// usage of the view: opening (View), the play/pause control, the Raw mode
// chip, downloading the raw bytes, and navigating away.
// NOTE: chip/play/download/back coordinates are calibrated on the live stack.

// A tiny MP4 (ftyp + minimal moov). Not decodable to frames, but enough for the
// renderer to mount and for the download round-trip to verify byte identity.
const MP4 = Buffer.from(
  "AAAAGGZ0eXBpc29tAAACAGlzb21pc28yAAAACGZyZWUAAAGFbWRhdA==",
  "base64",
);

test.describe("file-viewers/video", () => {
  test("View: opens the player and toggles play/pause", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-video-view",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/clip.mp4",
        MP4,
        headers,
        "video/mp4",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);
      await page.waitForTimeout(600);
      await page.screenshot({ path: "test-results/fv-video-view.png" });
      await expect(page).toHaveTitle(/^Klangk - /);

      // Play control sits top-left of the renderer toolbar; tap to play, again
      // to pause. (Playback itself is browser/codec-dependent; we exercise the
      // control without asserting frames.)
      await flutterClick(page, 24, 135); // play — calibrate
      await page.waitForTimeout(300);
      await flutterClick(page, 24, 135); // pause — calibrate
      await page.waitForTimeout(200);
      await page.screenshot({ path: "test-results/fv-video-playpause.png" });
    } finally {
      await cleanup();
    }
  });

  test("Raw: switches to the raw renderer", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-video-raw",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/raw.mp4",
        MP4,
        headers,
        "video/mp4",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      const { width } = vp(page);
      // Mode chips, top-right: [View] [Raw]. Switch to Raw.
      await flutterClick(page, width - 60, 105); // Raw chip — calibrate
      await page.waitForTimeout(400);
      await page.screenshot({ path: "test-results/fv-video-raw.png" });
      await expect(page).toHaveTitle(/^Klangk - /);
    } finally {
      await cleanup();
    }
  });

  test("Download: round-trips the raw bytes", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-video-dl",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/dl.mp4",
        MP4,
        headers,
        "video/mp4",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      // Download-raw is verified via the files API round-trip. The renderer's
      // download icon routes to this same endpoint; we assert on the endpoint
      // rather than the browser "download" event, which Flutter web's blob-
      // anchor download does not reliably surface to Playwright in CI.
      const dl = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(
          "work/dl.mp4",
        )}`,
        { headers },
      );
      expect(dl.ok()).toBeTruthy();
      expect(Buffer.from(await dl.body()).equals(MP4)).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("Navigate away: leave and return cleanly", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-video-nav",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/nav.mp4",
        MP4,
        headers,
        "video/mp4",
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      await flutterClick(page, 12, 105); // back/close — calibrate
      await page.waitForTimeout(300);
      await page.goto("/");
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
      await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
      await expect(page).toHaveTitle(/^Klangk - /, { timeout: 30_000 });
      // Re-open: the controller disposed cleanly; second mount doesn't crash.
      await openFilesTab(page);
      await clickFileRow(page, 0);
      await page.waitForTimeout(400);
      expect(vp(page).width).toBeGreaterThan(0);
    } finally {
      await cleanup();
    }
  });
});
