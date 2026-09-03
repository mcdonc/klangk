import { defineConfig } from "@playwright/test";

// E2E tests use non-default ports to avoid conflicts with a dev server
const BACKEND_PORT = process.env.KLANGKBUILD_E2E_PORT || "18997";
const BASE_URL =
  process.env.KLANGKBUILD_TEST_URL || `http://localhost:${BACKEND_PORT}`;
// @playwright/test (package.json) is pinned to the version whose browser
// revisions match the nix playwright-driver.browsers exposed via
// PLAYWRIGHT_BROWSERS_PATH, so Playwright resolves each browser under PWP on
// its own — no hardcoded build pin (the chromium-1223/firefox-1522/webkit-2287
// pins this replaced broke on any nix driver bump, #2182). devenv.nix asserts
// the @playwright/test chromium revision exists under PWP and fails fast if the
// two drift. CHROME_PATH / FIREFOX_PATH / WEBKIT_PATH still override the path
// for non-devenv runs (e.g. macOS, or the dist-smoke which `npx playwright
// install`s its own browser with PWP unset).
const chromiumUse = {
  launchOptions: {
    executablePath: process.env.CHROME_PATH || undefined,
    args: ["--disable-gpu"],
  },
};

const firefoxUse = {
  browserName: "firefox" as const,
  launchOptions: {
    executablePath: process.env.FIREFOX_PATH || undefined,
    // Allow navigator.clipboard read/write in automation without a prompt, so
    // the paste e2e can seed the clipboard. (The fix's own read path uses the
    // native `paste` event and needs no permission.)
    firefoxUserPrefs: {
      "dom.events.asyncClipboard.readText": true,
      "dom.events.testing.asyncClipboard": true,
    },
  },
};

const webkitUse = {
  browserName: "webkit" as const,
  launchOptions: {
    executablePath: process.env.WEBKIT_PATH || undefined,
  },
};

// Test projects:
// - chromium-api: API-only tests that don't need cross-browser (run once)
// - chromium, firefox, webkit: browser-specific tests
// CI runs chromium + chromium-api as the merge-gating job, and
// firefox + webkit as a separate non-blocking job.

export default defineConfig({
  testDir: "./e2e",
  // #3065: on CI the test timeout must exceed the worst-case setup chain
  // (register + workspace-create retries + login) PLUS the 240s
  // container-readiness budget — otherwise a slow-but-succeeding setup
  // pushes the readiness wait into the hard 300s kill, which surfaces as
  // an opaque "Test timeout of 300000ms exceeded" instead of the
  // diagnostic readiness rejection (and the burn wastes the retry budget
  // on the same starved bring-up). Locally 300s stays plenty.
  timeout: process.env.CI ? 480_000 : 300_000,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.KLANGKBUILD_E2E_WORKERS
    ? /^\d+$/.test(process.env.KLANGKBUILD_E2E_WORKERS)
      ? parseInt(process.env.KLANGKBUILD_E2E_WORKERS, 10)
      : process.env.KLANGKBUILD_E2E_WORKERS
    : 4,
  fullyParallel: true,
  globalSetup: "./global-setup.ts",
  globalTeardown: "./global-teardown.ts",
  use: {
    baseURL: BASE_URL,
    headless: true,
    screenshot: "only-on-failure",
  },
  projects: [
    {
      // API-only and simple-UI tests — no cross-browser behavior, run once.
      name: "chromium-api",
      testMatch: [
        "api.spec.ts",
        "branding.spec.ts",
        "features.spec.ts",
        "password-history.spec.ts",
        "token-expiry.spec.ts",
      ],
      use: chromiumUse,
    },
    {
      name: "chromium",
      testMatch: [
        "klangk.spec.ts",
        "pending-redirect.spec.ts",
        "terminal-keymap.spec.ts",
        "terminal-tab-gate.spec.ts",
        "per-user-home.spec.ts",
        "terminal-tabs.spec.ts",
        "shared-terminals.spec.ts",
        "shared-workspace-name.spec.ts",
        "tab-speed.spec.ts",
        "sudo.spec.ts",
        "ws-connect-speed.spec.ts",
        "workspace-export.spec.ts",
      ],
      use: chromiumUse,
    },
    {
      name: "firefox",
      testMatch: [
        "klangk.spec.ts",
        "terminal-keymap.spec.ts",
        "ws-connect-speed.spec.ts",
      ],
      use: firefoxUse,
    },
    {
      name: "webkit",
      testMatch: ["klangk.spec.ts", "terminal-keymap.spec.ts"],
      use: webkitUse,
    },
    {
      // File Viewers specs run on chromium only (canvas rendering + download
      // round-trips don't need the cross-browser matrix). Run with
      // `--project=file-viewers`.
      name: "file-viewers",
      testMatch: "file-viewers/*.spec.ts",
      use: chromiumUse,
    },
    {
      // Documentation screenshot capture — not part of CI.
      // Run with: --project=docs-screenshots
      name: "docs-screenshots",
      testMatch: [
        "docs-screenshots.spec.ts",
        "docs-invitations-screenshots-dev.spec.ts",
        "docs-files-screenshots-dev.spec.ts",
      ],
      use: chromiumUse,
    },
    {
      // Dist smoke test (#1611) — release.yml's dist-smoke-test job runs
      // ONLY this project, against a klangkd started from an installed
      // wheel (KLANGKBUILD_TEST_URL points Playwright at it; global-setup
      // short-circuits its own server startup in that mode). One test,
      // one browser: proves the frontend shipped in the wheel boots and
      // renders the login page through nginx → UDS → uvicorn.
      name: "dist-smoke",
      testMatch: ["dist-smoke.spec.ts"],
      use: chromiumUse,
    },
  ],
});
