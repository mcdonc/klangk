/**
 * Scene 4 — The Web UI — Workspaces, Terminal, and Hosted Apps (~1 min)
 *
 * CONTINUITY: the hero (admin@example.com) is the SAME account the CLI scenes
 * use, and `demo` is the SAME accumulating workspace (cloned repo + Pi session
 * from Sc 2 still in its terminal). We find-or-create both `demo` and
 * `openclaw` — never wipe — so a take works whether or not the CLI scenes ran.
 *
 * BEAT ORDER (per videoscript):
 *   1. Login → land on the Workspaces list (hold — viewer sees `openclaw`
 *      with its health icon + `demo` + the seeded fixtures).
 *   2. Open `openclaw` → click its **Service** sub-tab → the gateway's
 *      service-cmd terminal is shown (openclaw gateway running live).
 *   3. Back to the **bash** terminal tab → type `klangk-hosted-url 8000`
 *      → viewer sees the hosted URL that openclaw listens on.
 *   4. Open openclaw's **hosted app** in a new browser tab — proxied through
 *      Klangk's single nginx port. THE key visual of the hosted-apps feature.
 *   5. Return to the workspace list → click the **demo** card (Owned by Me).
 *   6. Terminal continuity (same tmux session as CLI). Type two commands,
 *      tour the nav tabs.
 *   7. Add a second terminal tab via "+" → right-click → Rename → "scratch".
 *
 * Calibration notes:
 *   - Service sub-tab: fracX 0.20 / fracY 0.20 (terminal tab strip, below
 *     the 5 nav tabs). Only appears for workspaces with a running service-cmd.
 *   - The hosted URL is built from the workspace's allocated host ports:
 *     `<DEMO_URL>/hosted/<ws_id>/<host_port>/` where host_port = container
 *     port 8000 = status.ports[0].
 *   - demo workspace card: fracX 0.52 / fracY 0.87 (2nd card in Owned by Me,
 *     below openclaw). Measured via calibration screenshot.
 *   - Terminal tab strip: fracY 0.20. After addTerminalTab, the new (2nd)
 *     tab center is at fracX ≈ 0.195 (4px margin + 122px bash tab + 61px
 *     half-tab). Right-clicking it opens the context menu for Rename.
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
  openTab,
  addTerminalTab,
  terminalType,
  mouseClick,
  mouseClickRight,
} from "../demo-helpers";

// Service sub-tab coordinate (terminal-window strip) — see header calibration.
const SERVICE_TAB_X = 0.2;
const SERVICE_TAB_Y = 0.2;

// bash terminal sub-tab (leftmost in the terminal tab strip): fracX 0.065
// (4px margin + 61px half of the 122px tab). fracY = strip center.
const BASH_TAB_X = 0.065;

// demo workspace card (2nd in "Owned by Me"). Empirically verified:
// fracY 0.40 → openclaw, fracY 0.55 → demo, fracY 0.70 → Team Project.

// Terminal tab strip vertical center (see addTerminalTab calibration).
const TAB_STRIP_Y = 0.2;
// After adding a 2nd tab, it sits right of bash: 4 + 122 + 61 ≈ 187px.
const NEW_TAB_X = 0.195;

test("web ui tour", async ({ page, context, request }) => {
  test.setTimeout(300_000);

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

  // 3. Open openclaw (navigate by hash — reliable; the list-hold above is the
  //    visual beat). Wait for the container + service-cmd to mount.
  await page.goto(`/#/workspace/${openclaw.id}`, { waitUntil: "load" });
  await waitForFlutter(page);
  await pace(6000); // service-cmd (gateway) needs a moment to register its tab

  // 4. Click the Service sub-tab → the gateway's service-cmd terminal shows.
  await mouseClick(page, SERVICE_TAB_X * 960, SERVICE_TAB_Y * 540);
  await pace(5000); // viewer watches the gateway log (starting → ready)

  // 5. Back to the bash terminal sub-tab → type klangk-hosted-url 8000 →
  //    see the URL. (The Service sub-tab is selected; we must click the
  //    bash sub-tab explicitly — the Terminal NAV tab is already active,
  //    so openTab(0) would be a no-op and leave Service showing.)
  await mouseClick(page, BASH_TAB_X * 960, TAB_STRIP_Y * 540);
  await waitForTerminal(page);
  await pace(1500);
  await terminalType(page, "klangk-hosted-url 8000");
  await pace(4500); // viewer reads the printed URL

  // 6. Open openclaw's hosted app in a new browser tab — its own web UI,
  //    proxied through Klangk's single port. The new tab becomes active so
  //    ffmpeg captures it loading.
  const appTab = await context.newPage();
  await appTab.goto(hostedUrl, { waitUntil: "domcontentloaded" });
  await pace(5000); // viewer sees openclaw's web UI rendered
  await appTab.close();
  await page.bringToFront();
  await pace(1000);

  // 7. Return to the workspace list → click the demo card (Owned by Me).
  await clickBackToWorkspaces(page);
  await pace(2500);
  // Ensure "Owned by Me" tab is active (1st of 2 TabBar tabs).
  await mouseClick(page, 0.25 * 960, 0.14 * 540);
  await pace(1500);
  // Click the demo workspace card (2nd card in "Owned by Me").
  await mouseClick(page, 0.5 * 960, 0.55 * 540);
  await waitForFlutter(page);
  await waitForTerminal(page);
  await pace(2500);

  // 8. Type two commands into the live terminal — same container the CLI
  //    scenes used (the ls output shows the cloned repo + .pi + .containername
  //    from Sc 2, proving CLI/web share one container).
  await terminalType(page, 'echo "Hello from the Klangk web terminal"');
  await pace(900);
  await terminalType(page, "ls -la ~/");
  await pace(2000);

  // 9. Tour the nav tabs — visible-mouse clicks at measured viewport fractions.
  await openTab(page, 1); // Files
  await pace(2000);
  await openTab(page, 2); // Chat
  await pace(2000);
  await openTab(page, 3); // Sharing
  await pace(2000);
  await openTab(page, 4); // Settings
  await pace(2000);

  // 10. Back to Terminal → add a second tab → rename it to "scratch".
  await openTab(page, 0); // Terminal
  await pace(1500);
  await addTerminalTab(page);
  await pace(2000);

  // Right-click the new tab → context menu → click "Rename".
  await mouseClickRight(page, NEW_TAB_X * 960, TAB_STRIP_Y * 540);
  await pace(1500); // context menu appears
  // "Rename" is the first (and likely only) menu item — click it.
  // It appears just below the cursor: TAB_STRIP_Y + ~0.04 (≈24px / 540).
  await mouseClick(page, NEW_TAB_X * 960, (TAB_STRIP_Y + 0.04) * 540);
  await pace(1500); // rename dialog appears

  // The dialog's TextField is autofocused but text is NOT selected, so
  // select-all first to replace the default name rather than append to it.
  await page.keyboard.press("Control+a");
  await page.keyboard.type("scratch");
  await pace(500);
  // Press Enter to submit (the TextField's onSubmitted pops with the value).
  await page.keyboard.press("Enter");
  await pace(4000); // viewer sees the renamed "scratch" tab settle
});
