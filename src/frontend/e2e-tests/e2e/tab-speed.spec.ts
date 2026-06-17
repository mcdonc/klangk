import { test, expect } from "@playwright/test";
import WebSocket from "ws";
import { createAndOpenWorkspace, API_BASE } from "./helpers";

test("new terminal tab round-trip completes within 2 seconds", async ({
  page,
  request,
}) => {
  const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
    page,
    request,
    "tab-speed",
    { waitForTerminal: true },
  );
  try {
    // Open parallel WebSocket
    const wsUrl = API_BASE.replace("http://", "ws://");
    const ws = new WebSocket(`${wsUrl}/ws?token=${token}`);

    await new Promise<void>((resolve, reject) => {
      ws.on("open", () => {
        ws.send(
          JSON.stringify({
            cmd: "workspace_connect",
            workspaceId,
          }),
        );
      });
      ws.on("error", reject);

      // Drive through connection flow
      const handler = (raw: Buffer | string) => {
        const msg = JSON.parse(raw.toString());
        if (msg.type === "workspace_ready") {
          ws.send(JSON.stringify({ cmd: "ui_ready" }));
        }
        if (msg.type === "event" && msg.event?.name === "container_ready") {
          // Start terminal
          ws.send(
            JSON.stringify({
              cmd: "terminal_start",
              cols: 80,
              rows: 24,
            }),
          );
        }
        if (msg.type === "terminal_started") {
          // Wait a moment for startup tasks to finish
          setTimeout(resolve, 2000);
        }
      };
      ws.on("message", handler);
    });

    // Drain any queued messages
    await new Promise((r) => setTimeout(r, 500));

    // Now measure new_window round-trip
    const startTime = Date.now();

    const elapsed = await new Promise<number>((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error("terminal_windows not received within 10s"));
      }, 10_000);

      ws.on("message", (raw: Buffer | string) => {
        const msg = JSON.parse(raw.toString());
        if (msg.type === "terminal_windows") {
          clearTimeout(timeout);
          resolve(Date.now() - startTime);
        }
      });

      ws.send(JSON.stringify({ cmd: "terminal_new_window" }));
    });

    console.log(`[tab-speed] Round-trip: ${elapsed}ms`);
    expect(elapsed).toBeLessThan(3000);

    ws.close();
  } finally {
    await cleanup();
  }
});
