import { test, expect } from "@playwright/test";
import {
  API_BASE,
  createAndOpenWorkspace,
  seedFile,
  openFilesTab,
  clickFileRow,
  flutterClick,
} from "../helpers";

// M2 — registry-driven viewer + Raw fallback.
//
// In M2 the only registered renderer is RawTextRenderer (always matches), so
// every file opens in the Raw view. These specs verify the registry resolves a
// renderer for both a known text type and an unknown type, that the viewer
// opens, and that navigating away leaves clean state.
//
// Convention (matches klangk.spec.ts): verify via the files API + page title +
// coordinate interaction. Flutter renders to <canvas> (no widget DOM), so we
// capture page.screenshot() artifacts for manual eyeballing rather than gating
// on toHaveScreenshot baselines (which klangk itself does not use and which are
// cross-machine flaky for canvas). NOTE: row/button coordinates are calibrated
// against the live stack on first run.

test.describe("file-viewers/registry", () => {
  test("resolves Raw for a text file and opens it", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-registry-text",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/notes.txt",
        "registry-text-canary",
        headers,
        "text/plain",
      );

      // Backing data is correct via the API.
      const api = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/notes.txt`,
        { headers },
      );
      expect(api.ok()).toBeTruthy();
      expect((await api.json()).content).toBe("registry-text-canary");

      // Open the viewer through the UI.
      await openFilesTab(page);
      await clickFileRow(page, 0);
      await page.screenshot({
        path: "test-results/fv-registry-text-view.png",
      });

      // Still inside the workspace (viewer doesn't navigate away).
      await expect(page).toHaveTitle(/^Klangk - /);
    } finally {
      await cleanup();
    }
  });

  test("resolves Raw fallback for an unknown file type", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-registry-unknown",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/mystery.xyzzy",
        "unknown-type-canary",
        headers,
      );
      const api = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/mystery.xyzzy`,
        { headers },
      );
      expect(api.ok()).toBeTruthy();
      expect((await api.json()).content).toBe("unknown-type-canary");

      await openFilesTab(page);
      await clickFileRow(page, 0);
      await page.screenshot({
        path: "test-results/fv-registry-unknown-view.png",
      });
      await expect(page).toHaveTitle(/^Klangk - /);
    } finally {
      await cleanup();
    }
  });

  test("opening a different file swaps the viewer cleanly", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-registry-swap",
    );
    try {
      // Two files; the list is alphabetical, so a.txt is row 0, b.txt row 1.
      await seedFile(
        request,
        workspaceId,
        "work/a.txt",
        "AAA-content",
        headers,
      );
      await seedFile(
        request,
        workspaceId,
        "work/b.txt",
        "BBB-content",
        headers,
      );

      await openFilesTab(page);
      await clickFileRow(page, 0); // open a.txt
      await page.screenshot({ path: "test-results/fv-swap-a.png" });

      // Back to list, then open the other file.
      await flutterClick(page, 12, 105); // back/close (top-left of chrome)
      await page.waitForTimeout(400);
      await clickFileRow(page, 1); // open b.txt
      await page.screenshot({ path: "test-results/fv-swap-b.png" });

      await expect(page).toHaveTitle(/^Klangk - /);

      // Both files still intact via the API after UI churn.
      for (const [name, body] of [
        ["a.txt", "AAA-content"],
        ["b.txt", "BBB-content"],
      ]) {
        const r = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/${name}`,
          { headers },
        );
        expect((await r.json()).content).toBe(body);
      }
    } finally {
      await cleanup();
    }
  });
});
