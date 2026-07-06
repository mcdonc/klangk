/**
 * Playwright config for the demo video scene scripts.
 *
 * This is SEPARATE from the real e2e suite (playwright.config.ts) — it is NOT
 * picked up by CI. It runs against an already-started demo server (the
 * dedicated video backend on :8996, started by run-demo-backend.sh), records
 * video for every scene, and uses a single worker so scenes run in a
 * deterministic order and don't fight over containers.
 *
 * Run:  devenv shell -- npx playwright test --config=src/frontend/e2e-tests/demo/playwright.demo.config.ts
 * One:  ... -g "clanker chat"          (grep a scene title)
 *
 * Pre-reqs: demo backend up on $KLANGK_TEST_URL (default :8996 via
 * run-demo-backend.sh), KLANGK_ALLOW_AUTOSTART=1, and the demo seed
 * (demo-seed.ts) run once. See README.md.
 */
import { defineConfig } from "@playwright/test";

const DEMO_URL = process.env.KLANGK_TEST_URL || "http://localhost:8996";
const BROWSERS = process.env.PLAYWRIGHT_BROWSERS_PATH || "";
// Logical viewport = the size Flutter lays out for = the Xvfb capture size in
// record-demo.sh. Default native 1920x1080 (crisp, desktop-sized widgets). For
// a 2x-bigger (but softer) render, set KLANGK_DEMO_VW=960 KLANGK_DEMO_VH=540
// and have record-demo.sh upscale the 960x540 capture to 1920x1080.
// (We cannot use devicePixelRatio>1 to get crisp-AND-2x: it breaks Flutter
// Web button hit-testing. See the viewport comment below.)
const VW = Number(process.env.KLANGK_DEMO_VW || 960);
const VH = Number(process.env.KLANGK_DEMO_VH || 540);

// Videos are written under test-results/ alongside each scene; the README maps
// scene name -> .webm file and how to convert/import to DaVinci.
export default defineConfig({
  testDir: "./scenes",
  // Playwright's default testMatch only grabs *.spec.ts / *.test.ts. Our
  // scenes are named scene-*.ts for readability, so match them explicitly.
  testMatch: /scene-.*\.ts$/,
  // Scene order is meaningful (the video is cut in this order), so serialize.
  workers: 1,
  fullyParallel: false,
  timeout: 300_000,
  // Demo runs are re-take-driven; never retry automatically (a flubbed take
  // should be visible, not papered over).
  retries: 0,
  // Playwright's built-in `video` writes a downscaled .webm fallback here.
  // record-demo.sh points this at a temp dir and discards it (the ffmpeg
  // capture is the keeper), so no stray artifacts land outside recordings/.
  // Defaults to `test-results` for a direct `playwright test` dry check.
  outputDir: process.env.KLANGK_DEMO_PW_OUTPUT || "test-results",
  // No global setup/teardown: we point at YOUR running server via KLANGK_TEST_URL.
  use: {
    baseURL: DEMO_URL,
    // Logical viewport (Flutter layout size) = capture size. Default 1920x1080
    // (crisp, 1:1 device px, working hit-testing). Env-tunable (KLANGK_DEMO_VW/
    // VH) so record-demo.sh can render at 960x540 + upscale to 1920x1080 for a
    // 2x-bigger-but-softer take.
    //
    // WHY NOT devicePixelRatio 2 for crisp-AND-2x? It BREAKS Flutter Web button
    // hit-testing: text fields still focus (the HTML renderer backs them with
    // real <input> elements) but tap recognizers on buttons/tabs silently never
    // fire — confirmed by test (login submit: no spinner, no submit) and by
    // Flutter's own issue tracker (gesture arena mis-handles pointer coords at
    // DPR>1). The only bypass is semantics, which needs a Dart rebuild
    // (SemanticsBinding.ensureSemantics, no runtime JS toggle) + DOM-locator
    // clicks + WebSocket terminal typing — a large refactor. So: pick two of
    // {crisp, 2x-bigger, working-clicks}. Native = crisp+clicks; 960x540-upscale
    // = 2x-bigger+clicks (softer).
    viewport: { width: VW, height: VH },
    // Always record Playwright's (downscaled) video as a fallback; the
    // full-res capture from record-demo.sh is the one you actually edit.
    video: "on",
    // Headed so you can SEE each click resolve as the scenes run (and watch me
    // dial in coordinates). Override with KLANGK_DEMO_HEADLESS=1 for a quick
    // background dry check.
    headless: process.env.KLANGK_DEMO_HEADLESS === "1",
    // --headed is recommended for recording (see README); run headless only for a
    // quick dry check. A modest slowMo makes clicks read as deliberate on camera.
    launchOptions: {
      slowMo: Number(process.env.KLANGK_DEMO_SLOWMO || 50),
      executablePath:
        process.env.CHROME_PATH ||
        `${BROWSERS}/chromium-1223/chrome-linux64/chrome`,
      // The recorder runs matchbox-window-manager under Xvfb, which force-
      // fullscreens the single app window to the exact screen size with no
      // decoration. --start-maximized hints matchbox to treat the browser as
      // the fullscreen app; the window then fills the canvas flush on all four
      // sides with zero manual positioning math (and no WM there to add chrome).
      // --enable-unsafe-swiftshader for WebGL in Xvfb. No --kiosk (Playwright
      // ignores it) and no DPR/zoom flags (DPR>1 breaks Flutter Web taps; see
      // the viewport comment above).
      args: ["--enable-unsafe-swiftshader", "--start-maximized"],
    },
  },
  // Bright window name so the .webm output dir is easy to find per scene.
  // Playwright names the artifact dir after the test path/title.
  reporter: [["list"]],
});
