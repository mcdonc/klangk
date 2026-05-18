import { defineConfig } from "@playwright/test";

// E2E tests use non-default ports to avoid conflicts with a dev server
const BACKEND_PORT = process.env.BARK_E2E_PORT || "18997";
const BASE_URL =
  process.env.BARK_TEST_URL || `http://localhost:${BACKEND_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  workers: parseInt(process.env.BARK_E2E_WORKERS || "2"),
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
      use: {
        launchOptions: {
          executablePath:
            process.env.CHROME_PATH ||
            "/run/current-system/sw/bin/google-chrome",
          args: ["--enable-unsafe-swiftshader"],
        },
      },
    },
  ],
});
