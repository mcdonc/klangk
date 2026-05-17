import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: process.env.BARK_TEST_URL || "http://localhost:8997",
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
