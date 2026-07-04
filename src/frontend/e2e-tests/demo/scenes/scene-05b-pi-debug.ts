/**
 * Scene 5b — Debugging with the Pi Harness (~2.5 min)
 *
 * CONTINUITY: still in the hero's `demo` workspace (from Sc 2/4/5), Terminal
 * tab. A BROWSER scene (Playwright drives the web Terminal tab; this is NOT a
 * klangkc-shell CLI scene — scenes 2/3/3b are the shell scenes, 4/5/5b are
 * browser).
 *
 * THREE terminal tabs are already open from Sc 2 + Sc 4 (left → right):
 *   - bash      (tmux window 0) — pi lives here the WHOLE scene (never exited)
 *   - terminal2 (window 1)       — where the fixed app is run for the reveal
 *   - scratch   (window 2)       — first run attempt (ModuleNotFoundError)
 *                                  + file inspection (ls / cat)
 *
 * The arc (see videoscript.md Scene 5b):
 *   1. bash:  pi builds a Flask hello-world app on port 8000 (write-files-only,
 *      so no pip install → deterministic ModuleNotFoundError next).
 *   2. scratch: `python3 app.py` → ModuleNotFoundError: No module named 'flask'.
 *   3. bash:  ask pi to debug (pi still running — we never exited it). pi
 *      installs flask.
 *   4. scratch: inspect pi's files (`ls`, `cat app.py`, `cat requirements.txt`).
 *   5. terminal2: run the now-fixed app (`python3 app.py`) → serving on 8000.
 *   6. Open the hosted URL in a temporary browser tab → "Hello from Klangk",
 *      then return to the workspace.
 *
 * The browser types every prompt/command visibly for the camera, but the scene
 * does NOT use fixed timeouts for pi's LIVE turns: the container's shell IS a
 * tmux session (named <user-id>), so a side `klangkc exec` reads the bash pane
 * (window 0) — the VERY same pane the browser renders. waitForPaneText /
 * waitForPiIdle detect pi's completion deterministically. The scratch/terminal2
 * beats are deterministic shell commands (instant output), so they use fixed
 * paces. Off-camera detection only; it never appears in the recording.
 *
 * Live/nondeterministic: re-run until you like pi's steps, keep that take,
 * trim dead air in DaVinci (or narrate over it).
 *
 * PRE-ROLL: `demo` workspace present (Sc 2) with bash+terminal2 tabs (Sc 2) and
 * a scratch tab (Sc 4); pi functional (llm-proxy key working); port 8000 free
 * (scene cleans up a prior take's app first).
 */
import { test } from "@playwright/test";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  DEMO_URL,
  SHARED_WORKSPACE,
  pace,
  slowType,
  vp,
  mouseClick,
  terminalType,
  terminalTabCenterPx,
  apiLogin,
  ensureSharedWorkspace,
  getMeId,
  openWorkspaceDemo,
  waitForTerminal,
  klangkcExec,
  waitForPaneText,
  waitForPiIdle,
  getWorkspaceStatus,
} from "../demo-helpers";

// Terminal tab strip (32px Row below the 40px nav-tab bar) — vertical center.
const TAB_STRIP_Y = 0.2;
// Tab centers (fixed 122px tabs in the 960px layout): bash=0, terminal2=1,
// scratch=2. Created in Sc 2 (terminal2) + Sc 4 (scratch); left → right order
// matches tmux window index.
const BASH_X = terminalTabCenterPx(0);
const TERMINAL2_X = terminalTabCenterPx(1);
const SCRATCH_X = terminalTabCenterPx(2);
// pi's input box sits near the bottom of its TUI.
const PI_INPUT_Y = 0.92;

const BUILD_PROMPT =
  'please build me a Flask hello world app on port 8000 that shows "Hello from Klangk". ' +
  "Write two files: app.py (the app) and requirements.txt (listing flask). " +
  "Do not install anything or run the app.";
const DEBUG_PROMPT =
  "The Flask app in app.py fails with ModuleNotFoundError when I run " +
  "`python3 app.py`. Install flask system-wide (pip install flask, no " +
  "virtualenv) so the app runs. You don't need to run the app yourself.";

test("pi debug", async ({ page, context, request }) => {
  test.setTimeout(420_000); // 7 min — two live pi turns + inspect + reveal

  // --- prep: ensure demo workspace + resolve the hero's tmux session name ---
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);
  const userId = await getMeId(request, headers); // = container tmux session

  // Off-camera cleanup: kill any stale app from a prior take (a leftover
  // python holds port 8000 and derails the reveal). Remove the files so pi
  // rebuilds fresh on camera. NB: fuser -k 8000/tcp kills whoever HOLDS the
  // port (the stale flask app), never the cleanup shell itself — a pkill/pgrep
  // on "app.py" or "python" self-matches this very command line and exits 137
  // (SIGKILL). Each stage || true; the trailing echo fixes exit 0.
  klangkcExec(
    ws.name,
    "fuser -k 8000/tcp 2>/dev/null || true; " +
      "cd ~ && rm -f app.py requirements.txt && rm -rf .venv; " +
      "echo cleaned",
  );

  // --- 1. Open the demo workspace, land on the Terminal tab (continuity). ---
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD, {
    waitForTerminal: true,
  });
  await pace(1500); // let the terminal settle on camera

  // Click the bash sub-tab (leftmost) and go home so pi writes app.py to ~
  // (matching the off-camera cleanup). Sc 5 may have left scratch active.
  await mouseClick(page, BASH_X, TAB_STRIP_Y * 540);
  await pace(1000);
  await terminalType(page, "cd ~");
  await pace(1500);

  // --- 2. Launch pi (visible), ask it to build the app. ---
  await terminalType(page, "pi");
  await pace(2500); // pi's TUI boots (registers llm-proxy models)
  // Focus pi's input box (bottom of the pi TUI) and type the prompt slowly so
  // the viewer reads it. The prompt is write-files-only so the
  // ModuleNotFoundError next is deterministic.
  const { width, height } = vp(page);
  await mouseClick(page, width / 2, height * PI_INPUT_Y);
  await pace(500);
  await slowType(page, BUILD_PROMPT, { cps: 16 });
  await pace(500);
  await page.keyboard.press("Enter");

  // Wait for pi to finish: it writes app.py + requirements.txt then reports
  // done. Detect via the side channel (the bash pane == container window 0).
  await waitForPaneText(ws.name, userId, "app.py", {
    timeoutMs: 120_000,
  });
  await waitForPiIdle(ws.name, userId, { timeoutMs: 60_000 });
  await pace(3000); // let the viewer read pi's "files written" summary

  // --- 3. scratch tab: try to run the app → ModuleNotFoundError. ---
  // pi STAYS RUNNING in bash (we never exit it). Mouse to the scratch tab,
  // run python3 app.py there — it fails instantly (flask not installed).
  await mouseClick(page, SCRATCH_X, TAB_STRIP_Y * 540);
  await pace(1500);
  await terminalType(page, "cd ~");
  await pace(1000);
  await terminalType(page, "python3 app.py");
  await pace(4000); // ModuleNotFoundError traceback sits on screen

  // --- 4. bash tab: ask pi (still running) to debug. ---
  await mouseClick(page, BASH_X, TAB_STRIP_Y * 540);
  await pace(1500);
  await mouseClick(page, width / 2, height * PI_INPUT_Y);
  await pace(500);
  await slowType(page, DEBUG_PROMPT, { cps: 16 });
  await pace(500);
  await page.keyboard.press("Enter");

  // pi installs flask. The build prompt said "do not install", so the first
  // appearance of "install" in the bash pane signals the debug step. Then wait
  // for pi to go idle.
  await waitForPaneText(ws.name, userId, /install/i, {
    timeoutMs: 120_000,
  });
  await waitForPiIdle(ws.name, userId, { timeoutMs: 90_000 });
  await pace(3000); // let the viewer read pi's fix

  // --- 5. scratch tab: inspect pi's files. ---
  await mouseClick(page, SCRATCH_X, TAB_STRIP_Y * 540);
  await pace(1500);
  await terminalType(page, "ls");
  await pace(2500);
  await terminalType(page, "cat app.py");
  await pace(3000);
  await terminalType(page, "cat requirements.txt");
  await pace(3000);

  // --- 6. terminal2 tab: run the now-fixed app. ---
  // Off-camera safety net: free port 8000 (in case pi ran the app during its
  // debug) and ensure flask is installed system-wide (in case pi used a
  // virtualenv — terminal2's bare `python3 app.py` uses system python). This
  // guarantees terminal2's on-camera run serves on 8000 regardless of what pi
  // did. Invisible to the recording.
  klangkcExec(
    ws.name,
    "fuser -k 8000/tcp 2>/dev/null || true; " +
      "pip install flask >/dev/null 2>&1 || true; echo ready",
  );
  await mouseClick(page, TERMINAL2_X, TAB_STRIP_Y * 540);
  await pace(1500);
  await terminalType(page, "cd ~");
  await pace(1000);
  await terminalType(page, "python3 app.py");
  await pace(5000); // "Running on http://0.0.0.0:8000" — app is up

  // --- 7. Open the hosted URL in a temporary browser tab → "Hello from
  //      Klangk", then return to the workspace. ---
  // Resolve container port 8000 → host port from the workspace status, then
  // build the hosted URL (the single-port proxy path). Open it in a NEW
  // browser tab (foreground it so ffmpeg captures the render), hold, close,
  // and return focus to the workspace tab.
  const status = await getWorkspaceStatus(request, headers, ws.id);
  const hostPort = (status.ports as number[])[0];
  const hostedUrl = `${DEMO_URL}/hosted/${ws.id}/${hostPort}/`;

  const appTab = await context.newPage();
  await appTab.goto(hostedUrl, { waitUntil: "domcontentloaded" });
  await appTab.bringToFront();
  await pace(7000); // viewer sees the page render: "Hello from Klangk"
  await appTab.close();
  await page.bringToFront();
  await pace(2000);
});
