/**
 * Scene 4 — The Web UI — Workspaces, Terminal, and Hosted Apps (~1 min)
 *
 * CONTINUITY: this is the bridge from the CLI scenes. The hero is
 * admin@example.com (same account CLI Sc 2 uses), and the workspace is `demo`
 * (created on-camera in Sc 2). We find-or-create both — never wipe — so a take
 * works whether or not the CLI scenes ran first. The workspace list thus shows
 * `demo` + the seeded Potemkin fixtures; if the CLI scenes ran, `openclaw`
 * (Sc 3/3b) appears too with its health icon.
 *
 * The video's hosted-app beat opens `openclaw`'s Service tab. That requires
 * the CLI scenes (3/3b) to have actually run (openclaw + its gateway). It's a
 * TODO below — calibrated when recording the full arc in order. The terminal /
 * tab-navigation beats here are the calibrated, deterministic core.
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
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  SHARED_WORKSPACE,
  SHARED_OPENCLAW,
  pace,
  ensureSharedWorkspace,
  apiLogin,
  openWorkspaceDemo,
  openTab,
  addTerminalTab,
  terminalType,
} from "../demo-helpers";

test("web ui tour", async ({ page, request }) => {
  test.setTimeout(240_000);

  // 1. Ensure the shared `demo` workspace exists (continuity with CLI Sc 2).
  //    Find-or-create, no wipe — state from prior scenes survives.
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);
  // Also ensure openclaw exists so it shows in the list for the hosted-app
  // beat (the gateway/service itself comes from CLI Sc 3/3b running first).
  await ensureSharedWorkspace(request, headers, SHARED_OPENCLAW);

  // 2. Open the workspace via the helper: it logs in once (lands on the
  //    Workspaces list) and holds there `holdOnListMs` so the viewer sees the
  //    workspace card, then opens the workspace and waits for the terminal to
  //    mount. Single login avoids the terminal_started WS race.
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD, {
    waitForTerminal: true,
    holdOnListMs: 3000,
  });
  await pace(2500); // let the terminal settle on camera

  // TODO (hosted-app beat): open openclaw from the list → Service tab →
  //   "Open hosted app" button → its proxied web UI loads. Requires the CLI
  //   scenes (3/3b) to have run first so openclaw's gateway is live. Calibrate
  //   the Service-tab + hosted-app-button coordinates when recording the arc
  //   in order. For now the list still shows openclaw (if present).

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
