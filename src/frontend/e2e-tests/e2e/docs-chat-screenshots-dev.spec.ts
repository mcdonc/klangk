/**
 * Captures chat screenshots against the running dev server.
 * Run with:
 *   KLANGK_TEST_URL=http://localhost:8995 npx playwright test \
 *     --project docs-screenshots -g "chat dev" --no-deps
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
  "chat",
);

mkdirSync(SCREENSHOT_DIR, { recursive: true });

const BASE_URL = process.env.KLANGK_TEST_URL || "http://localhost:8995";

function vp(page: import("@playwright/test").Page) {
  return page.viewportSize() || { width: 1280, height: 720 };
}

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

async function screenshotChatArea(
  page: import("@playwright/test").Page,
  name: string,
) {
  const { width, height } = vp(page);
  await page.screenshot({
    path: join(SCREENSHOT_DIR, `${name}.png`),
    clip: { x: 0, y: 56, width, height: height - 56 },
  });
}

test.describe("chat dev server screenshots", () => {
  test.setTimeout(600_000);

  test("chat and agent interaction", async ({ page, request }) => {
    const { width, height } = vp(page);
    const f = fv(page);

    // Set up WS frame listener BEFORE navigating
    let frameResolve: (() => void) | null = null;
    page.on("websocket", (ws) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        const text = frame.payload.toString();
        if (
          frameResolve &&
          text.includes("chat_message") &&
          text.includes('"message_type":1')
        ) {
          const r = frameResolve;
          frameResolve = null;
          r();
        }
      });
    });

    function waitForAgentResponse(timeoutMs = 120_000): Promise<void> {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          frameResolve = null;
          reject(new Error("Agent response timed out"));
        }, timeoutMs);
        frameResolve = () => {
          clearTimeout(timer);
          // Give 2s for rendering
          setTimeout(resolve, 2000);
        };
      });
    }

    // Login via UI
    await page.goto(BASE_URL);
    await waitForFlutter(page);
    await dismissAccessibility(page);

    const cx = width / 2;

    // Dismiss consent banner if present — click "I Accept" button
    // using page.mouse.click which bypasses locator coordinate mapping.
    await page.waitForTimeout(1000);
    await page.mouse.click(690, 420);
    await page.waitForTimeout(2000);

    // Fill login form (same coordinate pattern as helpers.ts loginViaUI)
    await f.click({ position: { x: cx, y: height * 0.46 }, force: true });
    await page.waitForTimeout(200);
    await page.keyboard.type("admin@plope.com");
    await f.click({ position: { x: cx, y: height * 0.56 }, force: true });
    await page.waitForTimeout(200);
    await page.keyboard.type("admin");
    await f.click({ position: { x: cx, y: height * 0.64 }, force: true });
    await page.waitForTimeout(5000);

    // Find or create workspace via API
    const loginResp = await request.post(`${BASE_URL}/auth/login`, {
      data: { identifier: "admin@plope.com", password: "admin" },
    });
    const { access_token: token } = await loginResp.json();
    const wsResp = await request.get(`${BASE_URL}/workspaces`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const workspaces = await wsResp.json();
    let workspace = workspaces.find(
      (w: Record<string, unknown>) => w.name === "my-project",
    );
    if (!workspace) {
      const createResp = await request.post(`${BASE_URL}/workspaces`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { name: "my-project" },
      });
      workspace = await createResp.json();
    }

    await page.goto(`${BASE_URL}/#/workspace/${workspace.id}`, {
      waitUntil: "load",
    });
    await waitForFlutter(page);
    await dismissAccessibility(page);
    // Wait for container to start and terminal to be ready
    await page.waitForTimeout(10000);

    // Click Chat tab (3rd of 5)
    const tabWidth = width / 5;
    await f.click({
      position: { x: tabWidth * 2 + tabWidth / 2, y: 76 },
      force: true,
    });
    await page.waitForTimeout(1000);

    // Screenshot 1: Chat panel
    await screenshotChatArea(page, "01-chat-panel");

    // Type and send a regular message
    await f.click({
      position: { x: width / 2, y: height - 40 },
      force: true,
    });
    await page.waitForTimeout(300);
    await page.keyboard.type("Hello team, ready to start coding!");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(1500);

    // Screenshot 2: Message sent
    await screenshotChatArea(page, "02-message-sent");

    // Ask the agent
    await f.click({
      position: { x: width / 2, y: height - 40 },
      force: true,
    });
    await page.waitForTimeout(300);
    await page.keyboard.type("@clanker what files are in the home directory?");
    await page.keyboard.press("Enter");

    // Wait for agent response
    try {
      await waitForAgentResponse();
    } catch {
      // Agent may not respond — screenshot whatever we have
      await page.waitForTimeout(5000);
    }

    // Screenshot 3: Agent response
    await screenshotChatArea(page, "03-agent-response");

    // Second agent interaction
    await f.click({
      position: { x: width / 2, y: height - 40 },
      force: true,
    });
    await page.waitForTimeout(300);
    await page.keyboard.type(
      "@clanker create a file called hello.py with a hello world program",
    );
    await page.keyboard.press("Enter");

    try {
      await waitForAgentResponse();
    } catch {
      await page.waitForTimeout(5000);
    }

    // Screenshot 4: Full conversation
    await screenshotChatArea(page, "04-agent-conversation");

    // Screenshot 5: Full page
    await page.screenshot({
      path: join(SCREENSHOT_DIR, "05-chat-full-page.png"),
    });
  });
});
