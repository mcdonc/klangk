/**
 * Captures Files tab screenshots against the running dev server.
 * Run with:
 *   KLANGKBUILD_TEST_URL=http://localhost:8995 npx playwright test \
 *     --project docs-screenshots -g "files screenshots" --no-deps
 */
import { test } from "@playwright/test";
import { join } from "path";
import { mkdirSync } from "fs";

const SCREENSHOT_DIR = join(
  __dirname,
  "..",
  "..",
  "..",
  "..",
  "docs",
  "assets",
  "files",
);

mkdirSync(SCREENSHOT_DIR, { recursive: true });

const BASE_URL = process.env.KLANGKBUILD_TEST_URL || "http://localhost:8995";

function fv(page: import("@playwright/test").Page) {
  return page.locator("flutter-view");
}

async function waitForFlutter(page: import("@playwright/test").Page) {
  await page.waitForFunction(
    () => !document.body.textContent?.includes("Loading, please wait"),
    { timeout: 90_000 },
  );
  await page.waitForSelector("flutter-view", { timeout: 10_000 });
  await page.waitForTimeout(500);
}

async function dismissAccessibility(page: import("@playwright/test").Page) {
  const btn = page.locator("button", { hasText: "Enable accessibility" });
  if (await btn.isVisible({ timeout: 500 }).catch(() => false)) {
    await btn.click();
    await page.waitForTimeout(300);
  }
}

test.describe("files screenshots", () => {
  test.setTimeout(300_000);

  test("files tab browsing and preview", async ({ page, request }) => {
    const { width, height } = page.viewportSize() || {
      width: 1280,
      height: 720,
    };
    const cx = width / 2;
    const f = fv(page);

    // Login via UI
    await page.goto(BASE_URL);
    await waitForFlutter(page);
    await dismissAccessibility(page);

    // Dismiss consent banner
    await page.waitForTimeout(1000);
    await page.mouse.click(690, 420);
    await page.waitForTimeout(2000);

    // Fill login form
    await f.click({
      position: { x: cx, y: height * 0.46 },
      force: true,
    });
    await page.waitForTimeout(200);
    await page.keyboard.type("admin@plope.com");
    await f.click({
      position: { x: cx, y: height * 0.56 },
      force: true,
    });
    await page.waitForTimeout(200);
    await page.keyboard.type("admin");
    await f.click({
      position: { x: cx, y: height * 0.64 },
      force: true,
    });
    await page.waitForTimeout(5000);

    // Find or create workspace via API
    const loginResp = await request.post(`${BASE_URL}/auth/login`, {
      data: { identifier: "admin@plope.com", password: "admin" },
    });
    const { access_token: token } = await loginResp.json();
    const headers = { Authorization: `Bearer ${token}` };

    const wsResp = await request.get(`${BASE_URL}/workspaces`, { headers });
    const workspaces = await wsResp.json();
    let workspace = workspaces[0];
    if (!workspace) {
      const createResp = await request.post(`${BASE_URL}/workspaces`, {
        headers,
        data: { name: "demo" },
      });
      workspace = await createResp.json();
    }

    // Navigate to workspace (starts the container)
    await page.goto(`${BASE_URL}/#/workspace/${workspace.id}`, {
      waitUntil: "load",
    });
    await waitForFlutter(page);
    await dismissAccessibility(page);
    await page.waitForTimeout(10000);

    // Seed some files for the screenshot (must happen after container starts)
    const seedFile = async (path: string, content: string) => {
      await request.post(
        `${BASE_URL}/workspaces/${workspace.id}/files/upload?path=${encodeURIComponent(path)}`,
        {
          headers,
          multipart: {
            file: {
              name: path.split("/").pop()!,
              mimeType: "text/plain",
              buffer: Buffer.from(content),
            },
          },
        },
      );
    };

    await seedFile(
      "/home/work/hello.py",
      'def greet(name):\n    return f"Hello, {name}!"\n\nif __name__ == "__main__":\n    print(greet("world"))\n',
    );
    await seedFile(
      "/home/work/README.md",
      "# My Project\n\nA demo workspace.\n",
    );
    await seedFile("/home/work/notes.txt", "TODO: add more features\n");

    // Click Files tab (2nd of 5 tabs)
    const tabWidth = width / 5;
    await f.click({
      position: { x: tabWidth + tabWidth / 2, y: 76 },
      force: true,
    });
    await page.waitForTimeout(2000);

    // Screenshot 1: Files tab with file listing
    await page.screenshot({
      path: join(SCREENSHOT_DIR, "01-file-browser.png"),
      clip: { x: 0, y: 56, width, height: height - 56 },
    });

    // Click on hello.py to preview it (first file in the list)
    // Files are listed below the path bar at ~y 140, each row ~48px
    await f.click({
      position: { x: cx, y: 190 },
      force: true,
    });
    await page.waitForTimeout(2000);

    // Screenshot 2: File preview
    await page.screenshot({
      path: join(SCREENSHOT_DIR, "02-file-preview.png"),
      clip: { x: 0, y: 56, width, height: height - 56 },
    });
  });
});
