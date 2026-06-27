import { test, expect } from "@playwright/test";
import {
  registerUser,
  createWorkspace,
  openWorkspace,
  waitForFlutter,
  loginViaUI,
  TEST_PASSWORD,
} from "./helpers";

// These tests verify that WebSocket connections complete quickly,
// guarding against Firefox's FailDelayManager throttling connections
// by up to 60s after an unclean close.

test.describe("WebSocket connect speed", () => {
  test("workspace opens within 10s of WebSocket connect", async ({
    page,
    request,
  }) => {
    const email = `ws-speed-${Date.now()}@test.example.com`;
    const { headers } = await registerUser(request, email);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      headers,
      "ws-speed",
    );

    try {
      // Set up the WebSocket listener BEFORE login: this PR hoists the WS
      // connect to login (WsClient.updateAuth connects on the logged-out ->
      // logged-in transition), so the socket is created during loginViaUI.
      // Registering after login would miss the websocket event and never
      // attach framereceived, causing container_ready to be missed.
      let containerReady = false;
      page.on("websocket", (ws: { on: Function }) => {
        ws.on("framereceived", (frame: { payload: string | Buffer }) => {
          const text = frame.payload.toString();
          if (text.includes("container_ready")) containerReady = true;
        });
      });

      await loginViaUI(page, email, TEST_PASSWORD);

      const start = Date.now();
      await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
      await waitForFlutter(page);

      await expect
        .poll(() => containerReady, {
          timeout: 10_000,
          message: "container_ready not received within 10s of navigation",
        })
        .toBeTruthy();

      const elapsed = Date.now() - start;
      expect(elapsed).toBeLessThan(10_000);
    } finally {
      await cleanup();
    }
  });

  test("workspace reopens within 10s after navigating away and back", async ({
    page,
    request,
  }) => {
    const email = `ws-reopen-${Date.now()}@test.example.com`;
    const { headers } = await registerUser(request, email);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      headers,
      "ws-reopen",
    );

    try {
      // First visit: register a global WS frame listener that persists
      // across SPA navigations. Track container_ready count.
      let containerReadyCount = 0;
      page.on("websocket", (ws: { on: Function }) => {
        ws.on("framereceived", (frame: { payload: string | Buffer }) => {
          const text = frame.payload.toString();
          if (text.includes("container_ready")) containerReadyCount++;
        });
      });

      await openWorkspace(page, email, workspaceId, {
        containerTimeout: 30_000,
      });
      // First container_ready received
      expect(containerReadyCount).toBeGreaterThanOrEqual(1);
      const countAfterFirst = containerReadyCount;

      // Navigate away via hash (SPA, no full reload — WS stays open)
      await page.evaluate(() => {
        window.location.hash = "#/workspaces";
      });
      await page.waitForTimeout(2000);

      // Second visit: navigate back to workspace
      const reopenStart = Date.now();
      await page.evaluate((id: string) => {
        window.location.hash = `#/workspace/${id}`;
      }, workspaceId);

      await expect
        .poll(() => containerReadyCount > countAfterFirst, {
          timeout: 10_000,
          message: "container_ready not received within 10s on reopen",
        })
        .toBeTruthy();

      const reopenTime = Date.now() - reopenStart;
      expect(reopenTime).toBeLessThan(10_000);
    } finally {
      await cleanup();
    }
  });
});
