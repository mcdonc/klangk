/**
 * Scene 07 — File Browser (~30s)
 *
 * Shows the Files tab: open it with the mouse, browse, click a file row for a
 * highlighted preview, narrate drag-drop upload and right-click
 * download/rename/delete. The workspace home always has content (dotfiles,
 * default shell configs), so there's something to browse without seeding.
 * Deterministic.
 */
import { test } from "@playwright/test";
import {
  DEMO_PASSWORD,
  pace,
  vp,
  mouseClick,
  openTab,
  ensureUser,
  ensureFreshWorkspace,
  openWorkspaceDemo,
} from "../demo-helpers";

const SCENE_USER = "demo-files@example.com";
const WORKSPACE_NAME = "files-demo";

test("file browser", async ({ page, request }) => {
  test.setTimeout(240_000);

  // 1. Ensure user + workspace.
  const { headers } = await ensureUser(request, SCENE_USER, DEMO_PASSWORD);
  const ws = await ensureFreshWorkspace(request, headers, WORKSPACE_NAME);

  // 2. Open the workspace (single login, holds on the list, waits for the
  //    terminal to mount so the container is up before we touch the Files
  //    tab). holdOnListMs gives the viewer a beat on the workspace card.
  await openWorkspaceDemo(page, SCENE_USER, ws.id, DEMO_PASSWORD, {
    waitForTerminal: true,
    holdOnListMs: 2000,
  });
  await pace(1500);

  // 3. Files tab (index 1) — visible-mouse click at the measured viewport
  //    fraction.
  await openTab(page, 1); // Files
  await pace(2500);

  // 4. Browse: click the first file/dir row for a highlighted preview. File
  //    rows sit ~110px + index*48 below the path bar (measured in the e2e
  //    `clickFileRow` helper). Use the visible mouse for the demo.
  const { width } = vp(page);
  await mouseClick(page, width / 2, 110);
  await pace(2500);

  // 5. Open a second row to show varied content / navigation.
  await mouseClick(page, width / 2, 110 + 48);
  await pace(2500);
});
