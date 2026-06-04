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
      `${BROWSERS}/chromium-1217/chrome-linux64/chrome`,
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
      process.env.FIREFOX_PATH || `${BROWSERS}/firefox-1511/firefox/firefox`,
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
};

// Browsers run sequentially (chromium → firefox → webkit) to avoid
// overwhelming SQLite with concurrent writes from parallel tests.

export default defineConfig({
  testDir: "./e2e",
  timeout: 300_000,
  retries: 0,
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
      name: "chromium",
      testMatch: "klangk.spec.ts",
      use: chromiumUse,
    },
    {
      name: "firefox",
      testMatch: "klangk.spec.ts",
      dependencies: ["chromium"],
      use: firefoxUse,
    },
    {
      name: "webkit",
      testMatch: "klangk.spec.ts",
      dependencies: ["firefox"],
      use: webkitUse,
    },
  ],
});
