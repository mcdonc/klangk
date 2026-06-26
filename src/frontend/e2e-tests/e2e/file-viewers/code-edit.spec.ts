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

// M6 — Code editor (Edit) via code_forge_web, saving through /files/upload.
//
// A code file has THREE renderers (Code View, Edit, Raw). Default = View; the
// mode chips switch to Edit. The decisive verification is the files-API: after
// typing + Save, GET /files/content returns the edited bytes.
// NOTE: chip/editor/Save coordinates are calibrated on the live stack.

test.describe("file-viewers/code-edit", () => {
  // FIXME: this exercises the real type-into-editor + Save UI flow, which is
  // coordinate-calibrated against the live stack. The editor/Save click targets
  // aren't reliably hit in the headless CI Flutter-web build (same UI-
  // interaction limitation that affects the blob-download specs / PR #159), so
  // the save doesn't land and the files-API assertion fails. Re-enable once the
  // coordinates are calibrated for CI or the web-renderer issue is resolved.
  test.fixme("edit and save persists via the files API", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-edit-save",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "/home/work/edit.dart",
        "// original\n",
        headers,
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      const { width } = vp(page);
      // Switch to the Edit mode chip.
      await flutterClick(page, width - 110, 105); // "Edit" chip — calibrate
      await page.waitForTimeout(500);
      await page.screenshot({ path: "test-results/fv-edit-mode.png" });

      // Focus the editor body and type new content.
      await fv(page).click({
        position: { x: width / 2, y: 300 },
        force: true,
      });
      await page.waitForTimeout(300);
      await page.keyboard.type("// EDITED-BY-E2E\n");
      await page.waitForTimeout(300);

      // Click Save (top-right of the editor toolbar).
      await flutterClick(page, width - 40, 135); // Save — calibrate
      await page.waitForTimeout(800);

      // The decisive check: the file now contains the edit.
      const api = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/content?path=/home/work/edit.dart`,
        { headers },
      );
      expect(api.ok()).toBeTruthy();
      expect((await api.json()).content).toContain("EDITED-BY-E2E");
    } finally {
      await cleanup();
    }
  });

  test("switch View/Edit/Raw modes and navigate away cleanly", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "fv-edit-modes",
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "/home/work/m.dart",
        "void main() {}\n",
        headers,
      );
      await openFilesTab(page);
      await clickFileRow(page, 0);

      const { width } = vp(page);
      // View (default) → Edit → Raw → back to View via chips.
      await flutterClick(page, width - 110, 105); // Edit
      await page.waitForTimeout(400);
      await flutterClick(page, width - 60, 105); // Raw
      await page.waitForTimeout(400);
      await page.screenshot({ path: "test-results/fv-edit-modes.png" });

      // Close, then leave the workspace and return.
      await flutterClick(page, 12, 105);
      await page.waitForTimeout(300);
      await page.goto("/");
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
      await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
      await expect(page).toHaveTitle(/^Klangk - /, { timeout: 30_000 });

      // The unsaved edit-mode buffer was local — the file is unchanged.
      const api = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/content?path=/home/work/m.dart`,
        { headers },
      );
      expect((await api.json()).content).toContain("void main()");
    } finally {
      await cleanup();
    }
  });
});
