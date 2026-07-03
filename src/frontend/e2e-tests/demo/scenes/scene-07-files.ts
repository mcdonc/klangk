/**
 * Scene 6 — File Browser (~30s)
 *
 * CONTINUITY: the same hero `demo` workspace — clanker's app.py /
 * requirements.txt from Sc 5 are still here for the viewer to browse. We
 * find-or-create `demo`, never wipe. The workspace home always has content
 * (dotfiles, default shell configs, plus whatever clanker wrote), so there's
 * something to browse without extra seeding.
 *
 * TODO: seed the Pyramid PDF (assets/pyramid-docs.pdf) into demo's home via
 * seedDemoFile (absolute container path /home/work/pyramid-docs.pdf) AFTER the
 * container boots, for the PDF-rendering beat the videoscript describes.
 *
 * Deterministic.
 */
import { test } from "@playwright/test";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  SHARED_WORKSPACE,
  pace,
  vp,
  mouseClick,
  openTab,
  apiLogin,
  ensureSharedWorkspace,
  openWorkspaceDemo,
} from "../demo-helpers";

test("file browser", async ({ page, request }) => {
  test.setTimeout(240_000);

  // 1. Ensure the shared `demo` workspace exists (continuity with Sc 5).
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);

  // 2. Open the workspace (single login, holds on the list, waits for the
  //    terminal to mount so the container is up before we touch the Files
  //    tab). holdOnListMs gives the viewer a beat on the workspace card.
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD, {
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
