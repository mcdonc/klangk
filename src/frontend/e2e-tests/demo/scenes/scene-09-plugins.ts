/**
 * Scene 8 — Plugins (~45s)
 *
 * CONTINUITY: still in the hero's `demo` workspace. pi is STILL RUNNING in the
 * bash tab from Sc 5b — this scene REUSES it (we do NOT launch a new pi; doing
 * so would break the across-scene continuity of one live agent session). The
 * browser tab must be open and WS-connected when pi fires the tool, so the
 * workspace stays open the whole scene.
 *
 * The beat (see videoscript.md Scene 8): ask the already-running pi to trigger
 * the boingball plugin's bouncing-ball animation, hold on the animation. The
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
 * the canvas.
 *
 * Live/nondeterministic: re-run until pi obliges (it sometimes narrates
 * instead of calling the tool). Keep that take, trim dead air in DaVinci.
 *
 * PRE-ROLL: `demo` workspace present + running; pi alive in the bash tab (from
 * Sc 5b); boingball baked in (always).
 */
import { test } from "@playwright/test";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  SHARED_WORKSPACE,
  pace,
  slowType,
  vp,
  mouseClick,
  terminalTabCenterPx,
  apiLogin,
  ensureSharedWorkspace,
  openWorkspaceDemo,
  getMeId,
  waitForPaneText,
  waitForPiIdle,
} from "../demo-helpers";

// Terminal tab strip geometry (shared with Sc 5b): vertical center, bash tab
// (index 0) horizontal center. pi is ALREADY RUNNING in the bash tab — we just
// switch to it and focus its input.
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

  // --- 2. Switch to the bash sub-tab where pi is ALREADY RUNNING (continuity
  //      from Sc 5b — we do NOT launch a new pi). Focus pi's input box and
  //      type the prompt slowly so the viewer reads it. ---
  await mouseClick(page, BASH_X, TAB_STRIP_Y * 540);
  await pace(1500); // viewer sees the still-running pi
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
