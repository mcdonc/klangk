/**
 * Scene 05 — Web UI: Workspaces and Terminal (~1 min)
 *
 * Pure navigation, on camera, as a clean FORWARD narrative: land on the
 * Workspaces LIST (the home view after login) → open a workspace → show the
 * terminal is live → add a second terminal tab.
 *
 * NOTE on terminal typing: the terminal is a Flutter `xterm` widget whose
 * FocusNode gates keyboard input, and Playwright's canvas click doesn't
 * reliably trip its onTap→requestFocus in this build (the real e2e suite
 * drives it via `terminal_started` + a known-good container image). So this
 * scene shows the terminal *rendering* and the tab mechanics (which DO work
 * via semantics clicks) without attempting to type into it — the narration
 * covers "it's a real, persistent shell". Deterministic; no live agent.
 */
import { test } from "@playwright/test";
import {
  DEMO_PASSWORD,
  pace,
  ensureUser,
  ensureFreshWorkspace,
  openWorkspaceDemo,
  openTab,
  addTerminalTab,
  terminalType,
} from "../demo-helpers";

const SCENE_USER = "demo-webui@example.com";
const WORKSPACE_NAME = "webui-demo";

test("web ui tour", async ({ page, request }) => {
  test.setTimeout(240_000);

  // 1. Ensure user + a fresh workspace.
  const { headers } = await ensureUser(request, SCENE_USER, DEMO_PASSWORD);
  const ws = await ensureFreshWorkspace(request, headers, WORKSPACE_NAME);

  // 2. Open the workspace via the helper: it logs in once (lands on the
  //    Workspaces list) and holds there `holdOnListMs` so the viewer sees the
  //    workspace card, then opens the workspace and waits for the terminal to
  //    mount. Single login avoids the terminal_started WS race.
  await openWorkspaceDemo(page, SCENE_USER, ws.id, DEMO_PASSWORD, {
    waitForTerminal: true,
    holdOnListMs: 3000,
  });
  await pace(2500); // let the terminal settle on camera

  // 3. Type two commands into the live terminal — clean canvas (semantics
  //    off) lets the xterm FocusNode receive keyboard input.
  await terminalType(page, 'echo "Hello from the Klangk web terminal"');
  await pace(900);
  await terminalType(page, "ls -la ~/");
  await pace(2000);

  // 4. Tour the nav tabs — visible-mouse clicks at measured viewport
  //    fractions (resolution-safe). Pause on each so the viewer reads layout.
  await openTab(page, 1); // Files
  await pace(2000);
  await openTab(page, 2); // Chat
  await pace(2000);
  await openTab(page, 3); // Sharing
  await pace(2000);
  await openTab(page, 4); // Settings
  await pace(2000);

  // 5. Back to Terminal and add a second terminal tab via the "+" (visible
  //    mouse). Narrate: each tab is an independent session and they persist
  //    across reconnects.
  await openTab(page, 0); // Terminal
  await pace(1500);
  await addTerminalTab(page);
  await pace(2500);
});
