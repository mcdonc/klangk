/**
 * Scene 6 — File Browser (~30s)
 *
 * CONTINUITY: the same hero `demo` workspace — clanker's app.py /
 * requirements.txt from Sc 5b are still here, AND the Pyramid PDF seeded right
 * after the demo container was created (Scene 2) by seed-demo-pdf.ts.
 *
 * The scene's unique payoff is the INLINE PDF RENDER — Klangk renders common
 * formats (PDFs, images, spreadsheets, video) in-browser, no download needed.
 * (Code-file preview is already shown in Sc 5b, where pi writes app.py.) So we
 * scroll the file list to the PDF and click it for the inline render.
 *
 * File-viewer layout gotcha (file_viewer_panel.dart): clicking a file REPLACES
 * the list with a full-width _FileViewer. To avoid the fragile back-arrow
 * close (a 16px icon — too small to click reliably by coordinate), we click
 * only ONE file (the PDF), keeping the flow to a single open.
 *
 * Geometry is MEASURED from recorded frames (recorder upscales 960×540 →
 * 1920×1080, so PNG-Y / 2 = Flutter-Y). The API sorts alphabetically, so
 * dotfiles fill the top; pyramid-docs.pdf (idx 9) is ~150px below the 540px
 * fold and must be scrolled into view. After scrolling ~200px the PDF sits at
 * screen-Y ≈379.
 *
 * Recording order: before Sc 8 (features) — this scene doesn't touch the
 * terminal. Deterministic.
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

  // 2. Open the workspace (holds on the list, waits for the terminal to mount).
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD, {
    waitForTerminal: true,
    holdOnListMs: 2000,
  });
  await pace(1500);

  // 3. Files tab (index 1) — the directory list roots at the workspace home.
  await openTab(page, 1); // Files
  await pace(2500);

  const { width } = vp(page);

  // 4. Scroll the file list down ~200px to reveal `pyramid-docs.pdf` (idx 9,
  //    below the fold). Move to the list center first so the wheel event lands
  //    on the ListView's scrollable.
  await page.mouse.move(width / 2, 300);
  await pace(500);
  await page.mouse.wheel(0, 200);
  await pace(2000); // viewer sees the list scroll

  // 5. Click the PDF (measured at screen-Y 395 after the scroll — the row
  //    spans ~373-417, so ±20px margin) → the viewer renders it INLINE.
  await mouseClick(page, width / 2, 395);
  await pace(6000); // viewer sees the PDF render inline
});
