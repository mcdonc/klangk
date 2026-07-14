/**
 * Scene 4 — The Web UI — Workspaces, Terminal, and Hosted Apps (~1 min)
 *
 * CONTINUITY: the hero (admin@example.com) is the SAME account the CLI scenes
 * use, and `demo` is the SAME accumulating workspace (cloned repo + Pi session
 * from Sc 2 still in its terminal). We find-or-create both `demo` and
 * `openclaw` — never wipe — so a take works whether or not the CLI scenes ran.
 *
 * BEAT ORDER (per videoscript — nothing extra):
 *   1. Login → land on the Workspaces list (hold — viewer sees `openclaw`
 *      with its health icon + `demo` + the seeded fixtures).
 *   2. Open `openclaw` → click its **Service** sub-tab → the gateway's
 *      service-cmd terminal is shown (openclaw gateway running live).
 *   3. Back to the **bash** terminal sub-tab → type `klangk-hosted-url 8000`
 *      → viewer sees the hosted URL that openclaw listens on.
 *   4. Open openclaw's **hosted app** in a new browser tab — proxied through
 *      Klangk's single nginx port. THE key visual of the hosted-apps feature.
 *   5. Return to the workspace list → click the **demo** card (Owned by Me).
 *   6. Terminal continuity — just SHOW the existing CLI scrollback (the cloned
 *      repo + Pi session are already there). No new commands, no nav-tab tour:
 *      "files, chat, collaboration" is a hand-off to Sc 5/6/7, not a click-through.
 *   7. Add a second terminal tab via "+" → right-click → Rename → "scratch".
 *
 * Interaction rule (see videoscript "Interaction on camera"): every UI action
 * is a mouse click/movement; the keyboard types text only (commands, the name
 * "scratch"). The rename uses a triple-click to select-all (not Ctrl+A) and a
 * click on OK to submit (not Enter).
 *
 * Calibration notes (all empirically verified):
 *   - Service sub-tab: fracX 0.20 / fracY 0.20 (terminal tab strip).
 *   - bash terminal sub-tab (leftmost): fracX 0.065 / fracY 0.20.
 *   - demo workspace card (2nd in "Owned by Me"): fracX 0.50 / fracY 0.55
 *     (fracY 0.40 → openclaw, 0.55 → demo, 0.70 → Team Project).
 *   - Tab-strip positions are DERIVED, not calibrated: tabs are fixed 122px
 *     (see terminalTabCenterPx / addTerminalTab in demo-helpers). With the
 *     2 continuity tabs (bash + terminal2), "+" is at fracX ≈ 0.272 and the
 *     new 3rd tab it creates is at fracX ≈ 0.322.
 *   - Rename field (triple-click select-all): fracX 0.50 / fracY 0.48.
 *   - OK button (rightmost in the dialog's bottom-right actions): fracX 0.60
 *     / fracY 0.62.
 *   - Hosted URL: `<DEMO_URL>/hosted/<ws_id>/<host_port>/` where host_port =
 *     container port 8000 = status.ports[0].
 */
import { test } from "@playwright/test";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  SHARED_WORKSPACE,
  SHARED_OPENCLAW,
  DEMO_URL,
  pace,
  ensureSharedWorkspace,
  apiLogin,
  getWorkspaceStatus,
  demoLogin,
  waitForFlutter,
  waitForTerminal,
  clickBackToWorkspaces,
  addTerminalTab,
  terminalTabCenterPx,
  terminalType,
  mouseClick,
  mouseClickRight,
  tripleClick,
} from "../demo-helpers";

// Terminal tab strip (32px Row below the 40px nav-tab bar) — vertical center.
const TAB_STRIP_Y = 0.2;
// Service sub-tab (appears for workspaces with a running service-cmd).
const SERVICE_TAB_X = 0.2;
// bash sub-tab (leftmost): 4px margin + 61px (half the 122px tab).
const BASH_TAB_X = 0.065;
// The demo workspace may or may not still have scene 2's terminal2 window
// (idle-stop + restart loses it). Assume only bash (index 0) to be safe.
// The "+" lands after 1 tab (fracX ≈ 0.145), and the NEW tab it creates
// is index 1 (fracX ≈ 0.195).
const EXISTING_TABS = 1;
// Rename dialog field + OK button (see header calibration).
const FIELD_X = 0.5;
const FIELD_Y = 0.48;
const OK_X = 0.6;
const OK_Y = 0.62;

test("web ui tour", async ({ page, context, request }) => {
  test.setTimeout(240_000);

  // 1. Ensure the shared workspaces exist (continuity). Find-or-create, no wipe.
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const demo = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);
  const openclaw = await ensureSharedWorkspace(
    request,
    headers,
    SHARED_OPENCLAW,
  );
  // openclaw must be running + healthy for the Service tab + hosted app.
  // Discover its host port (container 8000 → status.ports[0]) for the URL.
  const status = await getWorkspaceStatus(request, headers, openclaw.id);
  const hostPort = (status.ports as number[])[0];
  const hostedUrl = `${DEMO_URL}/hosted/${openclaw.id}/${hostPort}/`;

  // 2. Login → land on the Workspaces list. Hold so the viewer reads it
  //    (openclaw + demo + the seeded fixtures).
  await demoLogin(page, DEMO_HERO_EMAIL, DEMO_HERO_PASSWORD);
  await pace(3500);

  // 3. Click the openclaw workspace card (1st in "Owned by Me", above demo)
  //    to open it — a visible mouse movement + click, not a hash navigation.
  //    Wait for the container + service-cmd to mount.
  await mouseClick(page, 0.5 * 960, 0.4 * 540);
  await waitForFlutter(page);
  await pace(6000); // service-cmd (gateway) needs a moment to register its tab

  // 4. Click the Service sub-tab → the gateway's service-cmd terminal shows.
  await mouseClick(page, SERVICE_TAB_X * 960, TAB_STRIP_Y * 540);
  await pace(5000); // viewer watches the gateway log (starting → ready)

  // 5. Back to the bash terminal sub-tab → type klangk-hosted-url 8000 →
  //    see the URL. (The Service sub-tab is selected; clicking the Terminal
  //    NAV tab would be a no-op — it's already active — so click the bash
  //    sub-tab explicitly.)
  await mouseClick(page, BASH_TAB_X * 960, TAB_STRIP_Y * 540);
  await waitForTerminal(page);
  await pace(1500);
  await terminalType(page, "klangk-hosted-url 8000");
  await pace(4500); // viewer reads the printed URL

  // 6. Open openclaw's hosted app in a new browser tab — its own web UI,
  //    proxied through Klangk's single port. The new tab becomes active so
  //    ffmpeg captures it loading. (openclaw's own gateway may show an
  //    "origin not allowed" error — that's its internal check, not Klangk's;
  //    the point is the hosted app responds through the single port.)
  const appTab = await context.newPage();
  await appTab.goto(hostedUrl, { waitUntil: "domcontentloaded" });
  await pace(5000);
  await appTab.close();
  await page.bringToFront();
  await pace(1000);

  // 7. Return to the workspace list → click the demo card (Owned by Me).
  await clickBackToWorkspaces(page);
  await pace(2500);
  // "Owned by Me" is the first TabBar tab; ensure it's active.
  await mouseClick(page, 0.25 * 960, 0.14 * 540);
  await pace(1500);
  // Click the demo workspace card (2nd card in "Owned by Me").
  await mouseClick(page, 0.5 * 960, 0.55 * 540);
  await waitForFlutter(page);
  await waitForTerminal(page);
  // 8. Terminal continuity — HOLD on the existing CLI scrollback (the cloned
  //    repo + Pi session are already in the tmux from Sc 2). Do NOT type new
  //    commands or tour the nav tabs — the narration hands those off to Sc 5+.
  await pace(5000);

  // 9. Add a NEW terminal tab via "+" (after the 2 continuity tabs), then
  //    right-click THAT new tab → Rename → "scratch". All mouse except
  //    typing the literal name.
  // Click the "+" button to add a new tab. The "+" sits just after the last
  // tab; with 1 existing tab (bash, 122px wide + 4px margin) the icon center
  // is at ~140px in 960-layout. Hardcoded rather than computed — the formula
  // drifted with Flutter layout changes.
  const { width, height } = page.viewportSize()!;
  await mouseClick(page, 140 * (width / 960), TAB_STRIP_Y * height);
  await pace(2000);

  // The new tab is index 1 (2nd tab: bash + <new>). Its center is at ~190px.
  const newTabX = 190 * (width / 960);

  // Right-click the new tab → context menu → "Rename".
  await mouseClickRight(page, newTabX, TAB_STRIP_Y * height);
  await pace(1500); // context menu appears
  // "Rename" is the first menu item, just below the cursor.
  await mouseClick(page, newTabX, (TAB_STRIP_Y + 0.04) * height);
  await pace(1500); // rename dialog appears

  // Triple-click the field → select-all (replaces the default name, not
  // appends), then type "scratch", then click OK to submit (no Ctrl+A / Enter).
  await tripleClick(page, FIELD_X * 960, FIELD_Y * 540);
  await page.keyboard.type("scratch");
  await pace(700);
  await mouseClick(page, OK_X * 960, OK_Y * 540);
  await pace(4000); // viewer sees the renamed "scratch" tab settle
});
