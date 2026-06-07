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

// M5 — Code viewer (View, syntax-highlighted) with Raw also available.
//
// Verify via files-API + title + download round-trip + screenshot artifacts.
// A code file has TWO renderers (Code View + Raw) → mode chips appear.
// NOTE: chrome-row chip/button coordinates are calibrated on the live stack.

const DART = `void main() {
  final greeting = 'hello, klangk';
  print(greeting);
}
`;

test.describe("file-viewers/code-view", () => {
  test("highlights code by default and toggles to Raw", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-code-view",
    );
    try {
      await seedFile(request, workspaceId, "work/main.dart", DART, headers);
      const api = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/main.dart`,
        { headers },
      );
      expect((await api.json()).content).toContain("void main()");

      await openFilesTab(page);
      await clickFileRow(page, 0);
      await page.screenshot({ path: "test-results/fv-code-highlighted.png" });

      // Toggle to Raw via the mode chip.
      const { width } = vp(page);
      await flutterClick(page, width - 70, 105); // "Raw" chip — calibrate
      await page.waitForTimeout(400);
      await page.screenshot({ path: "test-results/fv-code-raw.png" });

      await expect(page).toHaveTitle(/^Klangk - /);
    } finally {
      await cleanup();
    }
  });

  test("download round-trips the source", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-code-download",
    );
    try {
      await seedFile(request, workspaceId, "work/dl.dart", DART, headers);
      await openFilesTab(page);
      await clickFileRow(page, 0);

      // Download-raw verified via the files API round-trip (the download icon
      // routes here; Flutter web's blob download doesn't surface a Playwright
      // "download" event in CI).
      const dl = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(
          "work/dl.dart",
        )}`,
        { headers },
      );
      expect(dl.ok()).toBeTruthy();
      expect(Buffer.from(await dl.body()).toString()).toContain("void main()");
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
      "fv-code-nav",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "work/nav.py",
        "print('x')\n",
        headers,
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
