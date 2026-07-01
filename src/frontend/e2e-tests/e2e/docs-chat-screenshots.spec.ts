/**
 * Captures screenshots for chat / AI agent documentation.
 * Run with: npx playwright test --project docs-screenshots -g "chat"
 *
 * Requires KLANGK_LLM_* env vars for agent responses.
 * Screenshots are saved to docs/assets/chat/
 */
import { test } from "@playwright/test";
import { join } from "path";
import { mkdirSync } from "fs";
import WebSocket from "ws";
import {
  registerUser,
  createWorkspace,
  openWorkspace,
  API_BASE,
  fv,
  vp,
  flutterClick,
} from "./helpers";

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

function openChatTab(page: import("@playwright/test").Page) {
  const { width } = vp(page);
  const tabWidth = width / 5;
  return flutterClick(page, tabWidth * 2 + tabWidth / 2, 76);
}

/** Send a chat message via raw WS and wait for it to be broadcast back. */
async function sendChatAndWait(
  token: string,
  workspaceId: string,
  message: string,
  waitForAgent = false,
): Promise<void> {
  const wsUrl = API_BASE.replace("http://", "ws://");
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${wsUrl}/ws?token=${token}`);
    const timeout = setTimeout(
      () => {
        ws.close();
        reject(new Error("Chat send timed out"));
      },
      waitForAgent ? 120_000 : 30_000,
    );

    ws.on("open", () => {
      ws.send(JSON.stringify({ cmd: "workspace_connect", workspaceId }));
    });

    let ready = false;
    ws.on("message", (raw: Buffer | string) => {
      const msg = JSON.parse(raw.toString());
      if (msg.type === "container_ready") {
        ws.send(JSON.stringify({ cmd: "ui_ready" }));
      }
      if (msg.type === "event" && msg.event?.name === "container_ready") {
        ready = true;
        ws.send(JSON.stringify({ cmd: "chat_send", message }));
      }
      // Wait for the message to be broadcast back (user message)
      if (ready && msg.type === "chat_message" && !waitForAgent) {
        if (msg.message === message) {
          clearTimeout(timeout);
          ws.close();
          resolve();
        }
      }
      // Wait for agent response
      if (ready && waitForAgent && msg.type === "chat_message") {
        if (msg.message_type === 1) {
          clearTimeout(timeout);
          ws.close();
          resolve();
        }
      }
    });

    ws.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });
  });
}

test.describe("chat documentation screenshots", () => {
  test("chat and agent interaction", async ({ page, request }) => {
    const email = `docs-chat-${Date.now()}@test.example.com`;
    const user = await registerUser(request, email);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      user.headers,
      "docs-chat",
    );

    try {
      await openWorkspace(page, email, workspaceId, {
        waitForTerminal: true,
      });
      await page.waitForTimeout(1000);

      // Navigate to Chat tab
      await openChatTab(page);
      await page.waitForTimeout(500);

      // Screenshot 1: Empty chat panel
      await screenshotChatArea(page, "01-empty-chat");

      // Send a regular chat message via WS
      await sendChatAndWait(
        user.token,
        workspaceId,
        "Hello everyone! Just setting up the project.",
      );
      await page.waitForTimeout(1000);

      // Screenshot 2: Message sent
      await screenshotChatArea(page, "02-message-sent");

      // Ask the agent a question
      try {
        await sendChatAndWait(
          user.token,
          workspaceId,
          "@clanker what files are in the home directory?",
          true, // wait for agent response
        );
      } catch {
        // Agent may not respond — wait longer and screenshot anyway
      }
      await page.waitForTimeout(5000);

      // Screenshot 3: Agent response (or just the user message if agent is unavailable)
      await screenshotChatArea(page, "03-agent-response");

      // Second agent interaction
      try {
        await sendChatAndWait(
          user.token,
          workspaceId,
          "@clanker create a file called hello.py that prints hello world",
          true,
        );
      } catch {
        // Agent may not respond
      }
      await page.waitForTimeout(5000);

      // Screenshot 4: Conversation with agent
      await screenshotChatArea(page, "04-agent-conversation");

      // Screenshot 5: Full page view
      await page.screenshot({
        path: join(SCREENSHOT_DIR, "05-chat-full-page.png"),
      });
    } finally {
      await cleanup();
    }
  });
});
