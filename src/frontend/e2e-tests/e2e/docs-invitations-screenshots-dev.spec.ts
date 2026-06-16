/**
 * Captures invitation admin page screenshots against the running dev server.
 * Run with:
 *   KLANGK_TEST_URL=http://localhost:8995 npx playwright test \
 *     --project docs-screenshots -g "invitation" --no-deps
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
  "invitations",
);

mkdirSync(SCREENSHOT_DIR, { recursive: true });

const BASE_URL = process.env.KLANGK_TEST_URL || "http://localhost:8995";

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

test.describe("invitation screenshots", () => {
  test.setTimeout(300_000);

  test("admin invitations page", async ({ page }) => {
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

    // Navigate to admin page
    await page.goto(`${BASE_URL}/#/admin/users`, { waitUntil: "load" });
    await waitForFlutter(page);
    await dismissAccessibility(page);
    await page.waitForTimeout(3000);

    // Screenshot 1: Users tab (default)
    await page.screenshot({
      path: join(SCREENSHOT_DIR, "01-admin-users-tab.png"),
      clip: { x: 0, y: 56, width, height: height - 56 },
    });

    // Click Invitations tab (third of four tabs)
    // Tabs: Users | Groups | Invitations | Access Control
    // Each tab occupies width/4 of the screen
    const tabWidth = width / 4;
    const invitationsTabX = tabWidth * 2 + tabWidth / 2; // center of 3rd tab
    const tabY = 76; // tab bar Y position
    await page.mouse.click(invitationsTabX, tabY);
    await page.waitForTimeout(1000);

    // Screenshot 2: Invitations tab
    await page.screenshot({
      path: join(SCREENSHOT_DIR, "02-invitations-tab.png"),
      clip: { x: 0, y: 56, width, height: height - 56 },
    });

    // Click the FAB (+ button) to open invite dialog
    // FAB is typically at bottom-right
    await page.mouse.click(width - 40, height - 40);
    await page.waitForTimeout(1000);

    // Screenshot 3: Invite dialog
    await page.screenshot({
      path: join(SCREENSHOT_DIR, "03-send-invitation.png"),
      clip: { x: 0, y: 56, width, height: height - 56 },
    });

    // Type an email in the dialog
    await page.keyboard.type("newuser@example.com");
    await page.waitForTimeout(500);

    // Screenshot 4: Dialog with email filled
    await page.screenshot({
      path: join(SCREENSHOT_DIR, "04-invite-email-filled.png"),
      clip: { x: 0, y: 56, width, height: height - 56 },
    });
  });
});
