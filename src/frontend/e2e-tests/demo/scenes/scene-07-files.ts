/**
 * Scene 6 — File Browser (~30s)
 *
 * CONTINUITY: the same hero `demo` workspace — clanker's app.py /
 * requirements.txt from Sc 5b are still here, AND the Pyramid PDF seeded right
 * after the demo container was created (Scene 2) by seed-demo-pdf.ts. So the
 * file browser has code files AND a PDF to render inline. We find-or-create
 * `demo`, never wipe.
 *
 * Recording order note: record this BEFORE Sc 8 (plugins) — Sc 8 launches pi
 * in the bash tab and leaves it alive, which would interfere with this scene's
 * file clicks if recorded after. This scene (Sc 6) doesn't touch the terminal.
 *
 * Deterministic. The PDF seed lives in seed-demo-pdf.ts (called by
 * record-cli.sh after Scene 2 creates the container), NOT here — this scene is
 * a pure browse.
 *
 * File-list geometry: the API sorts entries alphabetically, so dotfiles
 * (.bash_logout, .bashrc, ...) come first. At the 960px layout the list shows
 * ~9 rows before the fold, so `app.py` (index 6) is visible but
 * `pyramid-docs.pdf` (index 9) sits at/below the bottom edge and must be
 * scrolled into view before clicking. Rows are dense ListTiles (~48px)
 * starting at y≈110 (below the path bar).
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

// File-list row geometry (dense ListTiles at the 960px layout).
const ROW_TOP_Y = 110;
const ROW_H = 48;

test("file browser", async ({ page, request }) => {
  test.setTimeout(240_000);

  // 1. Ensure the shared `demo` workspace exists (continuity with Sc 5b).
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);

  // 2. Open the workspace (holds on the list, waits for the terminal to mount
  //    so the container is up before we browse). holdOnListMs gives the viewer
  //    a beat on the workspace card.
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD, {
    waitForTerminal: true,
    holdOnListMs: 2000,
  });
  await pace(1500);

  // 3. Files tab (index 1).
  await openTab(page, 1); // Files
  await pace(2500);

  // 4. Click `app.py` (index 6, visible) for a syntax-highlighted code
  //    preview — the file clanker/pi wrote in Sc 5b.
  const { width, height } = vp(page);
  const appPyY = ROW_TOP_Y + 6 * ROW_H;
  await mouseClick(page, width / 2, appPyY);
  await pace(3500);

  // 5. Scroll the file list down to reveal `pyramid-docs.pdf` (index 9, below
  //    the fold). Hover the list center and wheel down ~3 row-heights, then
  //    click the PDF row. The PDF renders inline in the viewer pane (the
  //    scene's payoff — Klangk renders common formats in-browser).
  const listCenterY = height * 0.55;
  await page.mouse.move(width / 2, listCenterY);
  await page.mouse.wheel(0, 3 * ROW_H);
  await pace(1500);
  // After scrolling down ~3 rows, app.py moves off-top; pyramid-docs.pdf
  // (originally index 9) lands near the top of the visible area.
  await mouseClick(page, width / 2, ROW_TOP_Y + 1.5 * ROW_H);
  await pace(5000); // viewer sees the PDF render inline
});
