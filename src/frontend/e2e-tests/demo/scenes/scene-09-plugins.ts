/**
 * Scene 8 — Plugins (~45s)
 *
 * CONTINUITY: still in the hero's `demo` workspace (from Sc 2/4/5/5b/6/7). A
 * BROWSER scene. pi is launched here for the first time since Sc 5b — Sc 6
 * (files) and Sc 7 (collaboration) don't touch the terminal, so recording
 * order 6 → 7 → 8 keeps pi from being alive during them.
 *
 * The beat (see videoscript.md Scene 8): launch pi, ask it to trigger the
 * boingball plugin's bouncing-ball animation, hold on the animation. The
 * boingball plugin is ALWAYS baked into klangk (every workspace image ships
 * with /opt/klangk/plugins/boingball + its pi extension at
 * /opt/klangk/pi-agent/extensions/boingball.ts), so there's no image-rebuild
 * prep — it's available in any workspace, instantly, with no setup script.
 * That zero-startup-cost, baked-at-build-time property is the whole point of
 * the scene's VO (vs the klangkc sandbox setup scripts from Sc 3).
 *
 * How the animation works: pi calls the `boing` tool (registered by the
 * boingball extension). The tool POSTs `{action:"boing", browser_id}` to the
 * host bridge (KLANGK_BRIDGE_URL), which fans the action out over the
 * workspace WebSocket; the Flutter app renders a bouncing-ball overlay above
 * the canvas. So the browser tab MUST be open and WS-connected when pi fires
 * the tool, or the animation never renders. We keep the workspace open the
 * whole scene (the WS connection from openWorkspaceDemo is live), and detect
 * pi's "Boing!" tool result via the tmux side channel (window 0, the bash
 * pane pi lives in).
 *
 * Live/nondeterministic: re-run until pi obliges (it sometimes narrates
 * instead of calling the tool). Keep that take, trim dead air in DaVinci.
 *
 * PRE-ROLL: `demo` workspace present + running; boingball baked in (always).
 */
import { test } from "@playwright/test";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  SHARED_WORKSPACE,
  DEMO_URL,
  pace,
  slowType,
  vp,
  mouseClick,
  terminalType,
  terminalTabCenterPx,
  apiLogin,
  ensureSharedWorkspace,
  openWorkspaceDemo,
  getMeId,
  waitForPaneText,
  waitForPiIdle,
} from "../demo-helpers";

// Terminal tab strip geometry (shared with Sc 5b): vertical center, bash tab
// (index 0) horizontal center.
const TAB_STRIP_Y = 0.2;
const BASH_X = terminalTabCenterPx(0);
// pi's input box sits near the bottom of its TUI.
const PI_INPUT_Y = 0.92;

// The prompt explicitly asks for the bouncing ball so pi calls the `boing`
// tool (whose description says "Only use this when the user explicitly asks
// for it"). Plain "boingball!" alone is sometimes interpreted as small talk.
const PROMPT =
  "use the boingball plugin to show me a bouncing ball animation, please";

test("plugins", async ({ page, request }) => {
  test.setTimeout(300_000); // 5 min — one live pi turn + hold

  // --- prep: ensure demo workspace + resolve the hero's tmux session name ---
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);
  const userId = await getMeId(request, headers); // = container tmux session

  // --- 1. Open the demo workspace, land on the Terminal tab. The WS
  //      connection opened here is what the boingball bridge action travels
  //      over to render the overlay — keep the workspace open the whole scene. ---
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD, {
    waitForTerminal: true,
  });
  await pace(1500); // let the terminal settle on camera

  // Click the bash sub-tab (leftmost) and go home so pi's cwd is clean.
  await mouseClick(page, BASH_X, TAB_STRIP_Y * 540);
  await pace(1000);
  await terminalType(page, "cd ~");
  await pace(1500);

  // --- 2. Launch pi (visible), ask it to trigger the bouncing ball. ---
  await terminalType(page, "pi");
  await pace(2500); // pi's TUI boots (registers llm-proxy models + plugins)
  const { width, height } = vp(page);
  await mouseClick(page, width / 2, height * PI_INPUT_Y);
  await pace(500);
  await slowType(page, PROMPT, { cps: 16 });
  await pace(500);
  await page.keyboard.press("Enter");

  // --- 3. Wait for pi to call the `boing` tool. The tool returns the text
  //      "Boing!" on success, which lands in the bash pane (window 0). Detect
  //      it via the side channel. If pi narrates instead of calling the tool,
  //      this times out — re-run the scene (live/nondeterministic). ---
  await waitForPaneText(ws.name, userId, /Boing!|boing/i, {
    timeoutMs: 120_000,
  });
  await waitForPiIdle(ws.name, userId, { timeoutMs: 60_000 });

  // --- 4. Hold on the animation. The bouncing ball renders as a Flutter
  //      overlay above the canvas for the viewer to see. ~20s lets the VO land
  //      (the plugin-vs-sandbox trade-off). ---
  await pace(20_000);
});
