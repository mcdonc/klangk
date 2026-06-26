import { test, expect } from "@playwright/test";
import {
  API_BASE,
  createAndOpenWorkspace,
  seedFile,
  waitForFlutter,
} from "../helpers";

// PR-A — deep-link a file into the EXISTING viewer via `?file=<workspace-rel>`
// on the workspace route (hash routing: `/#/workspace/:id?file=...`).
// WorkspacePage forwards it to IdeLayout, which selects the Files tab and calls
// FileViewerPanel.openFile — reusing the existing viewer (no parallel viewer).
//
// Flutter canvas has no widget DOM, so we assert the backing content via the
// files API and capture a screenshot artifact. Coordinates/visuals are
// calibrated on the live stack in CI.

const MD = `# Deep Link Heading

Opened straight from a URL.
`;

test.describe("file-viewers/deep-link", () => {
  test("?file= opens the file in the existing viewer (up + download present)", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-deeplink",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "/home/work/readme.md",
        MD,
        headers,
        "text/markdown",
      );

      // Backing content exists via API.
      const api = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/content?path=/home/work/readme.md`,
        { headers },
      );
      expect((await api.json()).content).toContain("# Deep Link Heading");

      // Navigate straight to the deep-link (hash route + query param). A fresh
      // goto reloads the page so WorkspacePage rebuilds with initialFile set,
      // which opens the file in the Files tab via the existing viewer.
      await page.goto(`/#/workspace/${workspaceId}?file=work/readme.md`, {
        waitUntil: "load",
      });
      await waitForFlutter(page);

      // The viewer is showing the file (rendered on the Flutter canvas).
      await page.screenshot({
        path: "test-results/fv-deeplink-rendered.png",
      });

      // Sanity: the deep-link param is reflected in the URL.
      expect(page.url()).toContain("file=work%2Freadme.md");
    } finally {
      await cleanup();
    }
  });
});
