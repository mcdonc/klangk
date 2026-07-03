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
 *   3. Open openclaw's **hosted app** (its own "OpenClaw Control" web UI) in a
 *      new browser tab — proxied through Klangk's single nginx port, no
 *      separate port / no extra auth. THE key visual of the hosted-apps feature.
 *   4. Back to the list → open `demo` → the terminal is the same tmux session
 *      the CLI used (continuity proof). Type two commands, tour the nav tabs,
 *      add a second terminal tab.
 *
 * Calibration notes:
 *   - The Service sub-tab sits in the terminal-window strip (below the 5 nav
 *     tabs), at ~fracX 0.20 / fracY 0.20 (vision-measured + empirically
 *     verified: the click activates the gateway terminal). It only appears for
 *     workspaces with a running service-command (openclaw has `openclaw
 *     gateway`, auto-started).
 *   - The hosted URL is built from the workspace's allocated host ports:
 *     `<DEMO_URL>/hosted/<ws_id>/<host_port>/` where host_port = container
 *     port 8000 = status.ports[0]. Hosted apps are reached by URL (there is no
 *     in-app "Open hosted app" button); opening it in a new tab is the real,
 *     reliable way a user reaches one.
 *
 * NOTE on terminal typing: the terminal is a Flutter `xterm` widget whose
 * FocusNode gates keyboard input. We keep Flutter semantics OFF (it would
 * break later terminal typing) and type via the proven coordinate-click focus
 * path (`terminalType`). Deterministic except for the gateway log scroll.
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
} from "../demo-helpers";

// Service sub-tab coordinate (terminal-window strip) — see header calibration.
const SERVICE_TAB_X = 0.2;
const SERVICE_TAB_Y = 0.2;

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
  await pace(4000); // viewer watches the gateway log (starting → ready)

  // 5. Open openclaw's hosted app in a new browser tab — its own "OpenClaw
  //    Control" web UI, proxied through Klangk's single port. The new tab
  //    becomes active so ffmpeg captures it loading.
  const appTab = await context.newPage();
  await appTab.goto(hostedUrl, { waitUntil: "domcontentloaded" });
  await pace(5000); // viewer sees openclaw's web UI rendered
  await appTab.close();
  await page.bringToFront();
  await pace(1000);

  // 6. Back to the Workspaces list, then open demo (continuity).
  await clickBackToWorkspaces(page);
  await pace(2500);
  await page.goto(`/#/workspace/${demo.id}`, { waitUntil: "load" });
  await waitForFlutter(page);
  await waitForTerminal(page);
  await pace(2500);

  // 7. Type two commands into the live terminal — same container the CLI
  //    scenes used (the ls output shows the cloned repo + .pi + .containername
  //    from Sc 2, proving CLI/web share one container).
  await terminalType(page, 'echo "Hello from the Klangk web terminal"');
  await pace(900);
  await terminalType(page, "ls -la ~/");
  await pace(2000);

  // 8. Tour the nav tabs — visible-mouse clicks at measured viewport fractions.
  await openTab(page, 1); // Files
  await pace(2000);
  await openTab(page, 2); // Chat
  await pace(2000);
  await openTab(page, 3); // Sharing
  await pace(2000);
  await openTab(page, 4); // Settings
  await pace(2000);

  // 9. Back to Terminal and add a second terminal tab via the "+".
  await openTab(page, 0); // Terminal
  await pace(1500);
  await addTerminalTab(page);
  await pace(2500);
});
