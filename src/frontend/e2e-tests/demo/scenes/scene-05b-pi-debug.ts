/**
 * Scene 5b — Debugging with the Pi Harness (~2.5 min)
 *
 * CONTINUITY: still in the hero's `demo` workspace (from Sc 2/4/5), Terminal
 * tab. A BROWSER scene (Playwright drives the web Terminal tab; this is NOT a
 * klangkc-shell CLI scene — scenes 2/3/3b are the shell scenes, 4/5/5b are
 * browser).
 *
 * The arc:
 *   1. pi builds a Flask hello-world app on port 8000 (write-files-only, so
 *      no pip install → deterministic ModuleNotFoundError next).
 *   2. `python3 app.py` → ModuleNotFoundError: No module named 'flask'.
 *   3. pi debugs: reads the traceback, creates a venv, pip-installs Flask,
 *      runs the app, and (via its get_hosted_url tool) surfaces the hosted URL.
 *   4. Inspect pi's files (cat app.py / requirements.txt).
 *   5. Open the hosted URL in a new browser tab → "Hello from Klangk".
 *
 * The browser types the prompts visibly for the camera, but the scene does
 * NOT use fixed timeouts for pi's live turns: the container's shell IS a tmux
 * session (named <user-id>), so a side `klangkc exec` reads the VERY SAME pane
 * the browser renders. waitForPaneText / waitForPiIdle detect completion
 * deterministically. Off-camera; never appears in the recording.
 *
 * Live/nondeterministic: re-run until you like pi's steps, keep that take,
 * trim dead air in DaVinci (or narrate over it).
 *
 * PRE-ROLL: `demo` workspace present (Sc 2); pi functional (llm-proxy key
 * working); port 8000 free (scene cleans up a prior take's app + venv first).
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
// bash sub-tab (leftmost): 4px margin + 61px (half the 122px tab).
const BASH_TAB_X = 0.065;

const BUILD_PROMPT =
  'please build me a Flask hello world app on port 8000 that shows "Hello from Klangk". ' +
  "Write two files: app.py (the app) and requirements.txt (listing flask). " +
  "Do not install anything or run the app.";
const DEBUG_PROMPT =
  "The Flask app in app.py won't run — I get ModuleNotFoundError. Figure out why and fix it.";

test("pi debug", async ({ page, request }) => {
  test.setTimeout(420_000); // 7 min — two live pi turns + inspect + hosted URL

  // --- prep: ensure demo workspace + resolve the hero's tmux session name ---
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);
  const userId = await getMeId(request, headers); // = container tmux session

  // Off-camera cleanup: kill any stale app from a prior take (a leftover
  // .venv python holds port 8000 and derails pi's debug). Remove the files so
  // pi rebuilds fresh on camera. NB: fuser -k 8000/tcp kills whoever HOLDS
  // the port (the stale flask app), never the cleanup shell itself -- a
  // pkill/pgrep on "app.py" or "python" self-matches this very command line
  // and exits 137 (SIGKILL). Each stage || true; the trailing echo fixes 0.
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

  // The Terminal nav tab is active by default; click the bash sub-tab to be
  // sure the focus is in the bash pane (not the Service tab from Sc 4).
  await mouseClick(page, BASH_TAB_X * 960, TAB_STRIP_Y * 540);
  await waitForTerminal(page);
  await pace(1500);

  // Go home (Sc 4 may have left the terminal in ~/klangk). pi writes app.py to
  // its cwd, so this keeps the build + the off-camera cleanup (which also cds
  // home) aligned.
  await terminalType(page, "cd ~");
  await pace(1500);

  // --- 2. Launch pi (visible), ask it to build the app. ---
  // Type `pi` via the visible terminal, then the build prompt. The prompt is
  // constrained to write-files-only so the ModuleNotFoundError is deterministic.
  await terminalType(page, "pi");
  await pace(2500); // pi's TUI boots (registers llm-proxy models)
  // Focus pi's input box (bottom of the pi TUI, ~y 0.92) and type the prompt
  // slowly so the viewer reads it.
  const { height } = vp(page);
  await mouseClick(page, vp(page).width / 2, height * 0.92);
  await pace(500);
  await slowType(page, BUILD_PROMPT, { cps: 16 });
  await pace(500);
  await page.keyboard.press("Enter");

  // Wait for pi to finish: it writes app.py + requirements.txt (the prompt
  // names both explicitly) then reports done. Detect via the side channel
  // (the browser pane == the container tmux pane). Key on app.py (always
  // written); the explicit two-files prompt makes requirements.txt reliable
  // for the later ModuleNotFoundError + pip-install beats.
  await waitForPaneText(ws.name, userId, "app.py", {
    timeoutMs: 120_000,
  });
  await waitForPiIdle(ws.name, userId, { timeoutMs: 60_000 });
  await pace(3000); // let the viewer read pi's "files written" summary

  // --- 3. Exit pi (Ctrl+D), run the app → ModuleNotFoundError. ---
  // Send Ctrl+D via the browser terminal (visible) to drop back to the shell.
  await page.keyboard.press("Control+D");
  await pace(2000);
  await terminalType(page, "python3 app.py");
  await waitForPaneText(ws.name, userId, "ModuleNotFoundError", {
    timeoutMs: 20_000,
  });
  await pace(3500); // let the traceback sit on screen

  // --- 4. Re-launch pi (visible), ask it to debug. ---
  await terminalType(page, "pi");
  await pace(2500);
  await mouseClick(page, vp(page).width / 2, height * 0.92);
  await pace(500);
  await slowType(page, DEBUG_PROMPT, { cps: 16 });
  await pace(500);
  await page.keyboard.press("Enter");

  // pi reads the traceback, creates a venv, pip-installs Flask, runs the app,
  // and (via get_hosted_url) prints the hosted URL. Wait for the install.
  await waitForPaneText(
    ws.name,
    userId,
    /Successfully installed|Running on http/,
    {
      timeoutMs: 180_000,
    },
  );
  await waitForPiIdle(ws.name, userId, { timeoutMs: 90_000 });
  await pace(3000); // let the viewer read pi's fix + hosted URL

  // --- 5. Exit pi. Inspect its files in the plain bash shell. ---
  await page.keyboard.press("Control+D");
  await pace(2000);
  await terminalType(page, "cat app.py");
  await pace(3500);
  await terminalType(page, "cat requirements.txt");
  await pace(3000);

  // --- 6. Open the hosted URL in the browser → "Hello from Klangk". ---
  // Resolve container port 8000 → host port from the workspace status, then
  // build the hosted URL (the single-port proxy path). Navigate the MAIN page
  // to it (replacing the workspace view) so the render is guaranteed visible
  // to ffmpeg — a new tab (context.newPage) in the single Chromium window
  // isn't reliably foregrounded by matchbox, but a same-tab goto is.
  const status = await getWorkspaceStatus(request, headers, ws.id);
  const hostPort = (status.ports as number[])[0];
  const hostedUrl = `${DEMO_URL}/hosted/${ws.id}/${hostPort}/`;

  await page.goto(hostedUrl, { waitUntil: "domcontentloaded" });
  await pace(7000); // viewer sees the page render: "Hello from Klangk"
});
