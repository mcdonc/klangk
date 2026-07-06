import { defineConfig } from "@playwright/test";

// E2E tests use non-default ports to avoid conflicts with a dev server
const BACKEND_PORT = process.env.KLANGK_E2E_PORT || "18997";
const BASE_URL =
  process.env.KLANGK_TEST_URL || `http://localhost:${BACKEND_PORT}`;
const BROWSERS = process.env.PLAYWRIGHT_BROWSERS_PATH || "";

const chromiumUse = {
  launchOptions: {
    executablePath:
      process.env.CHROME_PATH ||
      `${BROWSERS}/chromium-1223/chrome-linux64/chrome`,
    args: ["--enable-unsafe-swiftshader"],
  },
};

const firefoxUse = {
  browserName: "firefox" as const,
  launchOptions: {
    // CI (Linux) uses the default path; FIREFOX_PATH overrides it for local
    // runs (e.g. macOS, where the binary is firefox/Nightly.app/...), mirroring
    // CHROME_PATH above.
    executablePath:
      process.env.FIREFOX_PATH || `${BROWSERS}/firefox-1522/firefox/firefox`,
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
    // The nix playwright-driver bundles webkit build 2287, but @playwright/test
    // 1.59.1 looks for build 2272 by default ("Executable doesn't exist at
    // .../webkit-2272/pw_run.sh"). Like chromium/firefox above, pin the path to
    // the nix-provided build so Playwright uses it directly instead of its
    // npm-version-derived revision. WEBKIT_PATH mirrors CHROME/FIREFOX_PATH
    // for local overrides. See #1193.
    executablePath:
      process.env.WEBKIT_PATH || `${BROWSERS}/webkit-2287/pw_run.sh`,
  },
};

// Test projects:
// - chromium-api: API-only tests that don't need cross-browser (run once)
// - chromium, firefox, webkit: browser-specific tests
// CI runs chromium + chromium-api as the merge-gating job, and
// firefox + webkit as a separate non-blocking job.

export default defineConfig({
  testDir: "./e2e",
  timeout: 300_000,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.KLANGK_E2E_WORKERS
    ? /^\d+$/.test(process.env.KLANGK_E2E_WORKERS)
      ? parseInt(process.env.KLANGK_E2E_WORKERS, 10)
      : process.env.KLANGK_E2E_WORKERS
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
      testMatch: ["api.spec.ts", "token-expiry.spec.ts"],
      use: chromiumUse,
    },
    {
      name: "chromium",
      testMatch: [
        "klangk.spec.ts",
        "terminal-keymap.spec.ts",
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
        "docs-chat-screenshots.spec.ts",
        "docs-chat-screenshots-dev.spec.ts",
        "docs-invitations-screenshots-dev.spec.ts",
        "docs-files-screenshots-dev.spec.ts",
      ],
      use: chromiumUse,
    },
  ],
});
