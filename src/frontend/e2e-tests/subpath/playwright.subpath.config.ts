import { defineConfig } from "@playwright/test";

// The subpath × web × DPoP suite (#3287) — a SEPARATE config from
// playwright.config.ts: the browser base URL lives under /klangk/, the
// API helpers talk to the stack's own klangkd (port 18999, direct —
// browser traffic goes through the outer caddy, test-setup API traffic
// does not), and globalSetup boots that whole stack (second klangkd
// serving a /klangk base-href build + prefix-stripping outer caddy).
// Run via the `test-frontend-e2e` devenv task, which chains this config
// after the main one, or directly:
//   npx playwright test -c subpath/playwright.subpath.config.ts

// Set before importing anything that reads it: helpers.ts resolves
// API_BASE from this at module load, and worker processes inherit the
// runner's env (config modules load pre-fork).
process.env.KLANGKBUILD_E2E_PORT = process.env.KLANGKBUILD_E2E_PORT || "18999";

const BASE_URL =
  process.env.KLANGKBUILD_SUBPATH_URL || "http://localhost:18998/klangk/";

export default defineConfig({
  testDir: ".",
  timeout: process.env.CI ? 600_000 : 300_000,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  fullyParallel: false,
  globalSetup: "./global-setup.ts",
  globalTeardown: "./global-teardown.ts",
  use: {
    baseURL: BASE_URL,
    headless: true,
    screenshot: "only-on-failure",
    launchOptions: {
      executablePath: process.env.CHROME_PATH || undefined,
      args: ["--disable-gpu"],
    },
  },
});
