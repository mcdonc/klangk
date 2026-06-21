/**
 * Captures screenshots for terminal sharing documentation.
 * Run with: npx playwright test docs-screenshots --project chromium --no-deps
 *
 * Screenshots are saved to docs/assets/terminal-sharing/
 */
import { test } from "@playwright/test";
import { join } from "path";
import { mkdirSync } from "fs";
import {
  registerUser,
  createWorkspace,
  openWorkspace,
  API_BASE,
  TEST_PASSWORD,
  loginViaUI,
  waitForFlutter,
  fv,
  vp,
  terminalType,
} from "./helpers";
import WebSocket from "ws";

const SCREENSHOT_DIR = join(
  __dirname,
  "..",
  "..",
  "..",
  "..",
  "docs",
  "assets",
  "terminal-sharing",
);

// Ensure screenshot directory exists
mkdirSync(SCREENSHOT_DIR, { recursive: true });

async function screenshot(page: import("@playwright/test").Page, name: string) {
  await page.screenshot({ path: join(SCREENSHOT_DIR, `${name}.png`) });
}

// Clip to just the tab bar + terminal area (skip the top nav)
async function screenshotTerminalArea(
  page: import("@playwright/test").Page,
  name: string,
) {
  const { width, height } = vp(page);
  await page.screenshot({
    path: join(SCREENSHOT_DIR, `${name}.png`),
    clip: { x: 0, y: 56, width, height: height - 56 },
  });
}

interface WsMessage {
  type?: string;
  [key: string]: unknown;
}

class TestWsClient {
  private ws: WebSocket;
  private queue: WsMessage[] = [];
  private waiters: Array<(msg: WsMessage) => void> = [];

  constructor(ws: WebSocket) {
    this.ws = ws;
    ws.on("message", (raw: Buffer | string) => {
      const msg = JSON.parse(raw.toString());
      if (this.waiters.length > 0) {
        this.waiters.shift()!(msg);
      } else {
        this.queue.push(msg);
      }
    });
  }

  send(msg: Record<string, unknown>) {
    this.ws.send(JSON.stringify(msg));
  }

  async recv(timeout = 10_000): Promise<WsMessage> {
    if (this.queue.length > 0) return this.queue.shift()!;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error("recv timed out")),
        timeout,
      );
      this.waiters.push((msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
    });
  }

  async recvUntil(
    predicate: (msg: WsMessage) => boolean,
    timeout = 30_000,
  ): Promise<WsMessage> {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const msg = await this.recv(deadline - Date.now());
      if (predicate(msg)) return msg;
    }
    throw new Error("recvUntil timed out");
  }

  close() {
    this.ws.close();
  }
}

async function connectWs(
  token: string,
  workspaceId: string,
): Promise<TestWsClient> {
  const wsUrl = API_BASE.replace("http://", "ws://");
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${wsUrl}/ws?token=${token}`);
    const client = new TestWsClient(ws);
    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error("connect timed out"));
    }, 60_000);

    ws.on("open", () => {
      client.send({ cmd: "workspace_connect", workspaceId });
    });
    ws.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });

    (async () => {
      await client.recvUntil((m) => m.type === "workspace_ready");
      client.send({ cmd: "ui_ready" });
      await client.recvUntil(
        (m) =>
          m.type === "event" &&
          (m.event as Record<string, unknown>)?.name === "container_ready",
      );
      clearTimeout(timeout);
      resolve(client);
    })().catch((err) => {
      clearTimeout(timeout);
      reject(err);
    });
  });
}

test.describe("documentation screenshots", () => {
  test("terminal sharing workflow", async ({ page, browser, request }) => {
    // --- Setup: owner + collaborator ---
    const ownerEmail = `docs-owner-${Date.now()}@test.example.com`;
    const collabEmail = `docs-collab-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const collab = await registerUser(request, collabEmail);

    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "docs-demo",
    );

    try {
      // Add collaborator as owner (so they can type in shared terminals)
      await request.post(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/roles/owners`,
        {
          headers: owner.headers,
          data: { email: collabEmail },
        },
      );

      // --- Owner opens workspace ---
      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });
      await page.waitForTimeout(1000);

      // Screenshot 1: Initial terminal with single "bash" tab
      await screenshotTerminalArea(page, "01-initial-terminal");

      // Type something to show it's a real terminal
      const { width, height } = vp(page);
      const f = fv(page);
      await f.click({
        position: { x: width / 2, y: height / 2 },
        force: true,
      });
      await page.waitForTimeout(300);
      await page.keyboard.type("echo 'Hello from the terminal!'");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(500);

      // Screenshot 2: Terminal with typed command
      await screenshotTerminalArea(page, "02-terminal-with-command");

      // --- Share the terminal via right-click ---
      // Right-click on the "bash" tab (should be around x=60, y=80)
      // The tab bar is at y≈77 (56px nav + 21px into the 32px tab bar)
      await f.click({
        position: { x: 60, y: 21 },
        button: "right",
        force: true,
      });
      await page.waitForTimeout(500);

      // Screenshot 3: Right-click context menu on tab
      await screenshot(page, "03-tab-context-menu");

      // Click "Share" in the popup menu
      // The popup menu items are rendered by Flutter — find and click "Share"
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);

      // Use the WS client to share instead (more reliable than clicking the menu)
      const ownerWs = await connectWs(owner.token, workspaceId);
      ownerWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
      const windowsMsg = await ownerWs.recvUntil(
        (m) => m.type === "terminal_windows",
        60_000,
      );
      const firstWindow = (
        windowsMsg.windows as Array<Record<string, unknown>>
      )[0];
      ownerWs.send({
        cmd: "share_window",
        window_id: firstWindow.id as string,
      });
      await ownerWs.recvUntil((m) => m.type === "shared_terminals");
      await page.waitForTimeout(500);

      // Screenshot 4: Tab now shows broadcast icon (shared)
      await screenshotTerminalArea(page, "04-shared-tab-with-icon");

      // --- Rename the terminal ---
      ownerWs.send({
        cmd: "terminal_rename_window",
        index: firstWindow.index as number,
        name: "build",
      });
      await ownerWs.recvUntil((m) => m.type === "terminal_windows");
      await page.waitForTimeout(500);

      // Screenshot 5: Tab renamed to "build"
      await screenshotTerminalArea(page, "05-renamed-tab");

      // --- Create a second terminal ---
      ownerWs.send({ cmd: "terminal_new_window" });
      await ownerWs.recvUntil((m) => m.type === "terminal_windows");
      await page.waitForTimeout(500);

      // Screenshot 6: Two tabs — "build" (shared) and "1" (isolated)
      await screenshotTerminalArea(page, "06-two-tabs");

      // --- Collaborator joins ---
      const collabWs = await connectWs(collab.token, workspaceId);
      collabWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
      await collabWs.recvUntil((m) => m.type === "terminal_started");

      // Collab joins the shared terminal
      const sharedMsg = await collabWs.recvUntil(
        (m) =>
          m.type === "shared_terminals" &&
          (m.terminals as Array<Record<string, unknown>>).length > 0,
      );
      const sharedTerminal = (
        sharedMsg.terminals as Array<Record<string, unknown>>
      )[0];
      collabWs.send({
        cmd: "join_shared_terminal",
        user_id: sharedTerminal.user_id,
        window_id: sharedTerminal.window_id,
      });
      await collabWs.recvUntil((m) => m.type === "terminal_started");
      await page.waitForTimeout(1000);

      // Screenshot 7: Owner's view showing viewer count on shared tab
      await screenshotTerminalArea(page, "07-viewer-count");

      // --- Open collaborator's browser view ---
      const collabPage = await browser.newPage();
      await openWorkspace(collabPage, collabEmail, workspaceId, {
        waitForTerminal: true,
      });
      await collabPage.waitForTimeout(1000);

      // Screenshot 8: Collaborator's view showing the shared tab from owner
      await screenshotTerminalArea(collabPage, "08-collaborator-view");

      collabWs.close();
      ownerWs.close();
      await collabPage.close();
    } finally {
      await cleanup();
    }
  });
});
