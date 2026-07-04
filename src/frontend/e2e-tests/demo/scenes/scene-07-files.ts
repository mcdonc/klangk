/**
 * Scene 6 — File Browser (~30s)
 *
 * CONTINUITY: the same hero `demo` workspace — clanker's app.py /
 * requirements.txt from Sc 5b are still here, AND the Pyramid PDF seeded right
 * after the demo container was created (Scene 2) by seed-demo-pdf.ts. So the
 * file browser has code files AND a PDF to render inline. We find-or-create
 * `demo`, never wipe.
 *
 * Deterministic. The PDF seed lives in seed-demo-pdf.ts (called by
 * record-cli.sh after Scene 2 creates the container), NOT here — this scene is
 * a pure browse.
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

  // 3. Files tab (index 1) — visible-mouse click at the measured viewport
  //    fraction.
  await openTab(page, 1); // Files
  await pace(2500);

  // 4. Browse: click a code file row for a highlighted preview (app.py from
  //    Sc 5b). File rows sit ~110px + index*48 below the path bar. Use the
  //    visible mouse for the demo.
  const { width } = vp(page);
  await mouseClick(page, width / 2, 110);
  await pace(2500);

  // 5. Open a second row to show varied content / navigation (the seeded
  //    pyramid-docs.pdf is also in this list — clicking it would render inline,
  //    but the code-file browse is the deterministic, always-present beat).
  await mouseClick(page, width / 2, 110 + 48);
  await pace(2500);
});
