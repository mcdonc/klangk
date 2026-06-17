import { test, expect, Page } from "@playwright/test";
import { createAndOpenWorkspace, fv, terminalType, vp } from "./helpers";

/**
 * Capture all WebSocket frames received from the server.
 * Returns the array — it grows as frames arrive.
 */
function captureReceivedFrames(page: Page): string[] {
  const frames: string[] = [];
  page.on("websocket", (ws: { on: Function }) => {
    ws.on("framereceived", (frame: { payload: string | Buffer }) => {
      frames.push(frame.payload.toString());
    });
  });
  return frames;
}

test.describe("terminal copy via bridge", () => {
  test("mouse drag selection copies to system clipboard via bridge", async ({
    page,
    context,
    request,
  }) => {
    // Grant clipboard permissions so Clipboard.setData works without
    // user activation (the bridge round-trip may exceed the activation
    // window).
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);

    const received = captureReceivedFrames(page);
    const { cleanup } = await createAndOpenWorkspace(page, request, "copy", {
      waitForTerminal: true,
    });

    try {
      const { width, height } = vp(page);

      // Focus the terminal and generate visible output.
      await fv(page).click({
        position: { x: width / 2, y: height / 2 },
        force: true,
      });
      await page.waitForTimeout(500);

      await terminalType(
        page,
        "echo COPYTEST-START; seq 1 10; echo COPYTEST-END",
      );
      await page.waitForTimeout(2000);

      // Mouse drag across several lines in the terminal area.
      // With mouse-on, tmux enters copy mode on drag and selects text.
      // On mouse-up, copy-pipe sends the selection to klangk-copy-to-clipboard
      // which POSTs clipboard_write to the bridge → server relays to browser
      // → Flutter writes to system clipboard.
      const startX = 50;
      const startY = height / 2 - 40;
      const endX = width - 50;
      const endY = height / 2 + 40;

      await page.mouse.move(startX, startY);
      await page.waitForTimeout(100);
      await page.mouse.down();
      await page.waitForTimeout(100);

      // Drag slowly so tmux registers the selection
      const steps = 10;
      for (let i = 1; i <= steps; i++) {
        const x = startX + ((endX - startX) * i) / steps;
        const y = startY + ((endY - startY) * i) / steps;
        await page.mouse.move(x, y);
        await page.waitForTimeout(50);
      }
      await page.mouse.up();

      // Wait for the clipboard_write browser_request to arrive via WebSocket.
      const deadline = Date.now() + 15_000;
      let clipboardFrame: string | undefined;
      while (Date.now() < deadline) {
        clipboardFrame = received.find(
          (f) => f.includes("clipboard_write") && f.includes("browser_request"),
        );
        if (clipboardFrame) break;
        await page.waitForTimeout(200);
      }

      expect(clipboardFrame).toBeDefined();
      const msg = JSON.parse(clipboardFrame!);
      expect(msg.action).toBe("clipboard_write");
      expect(msg.text.length).toBeGreaterThan(0);

      // After the bridge round-trip, the system clipboard should have
      // the selected text.
      await page.waitForTimeout(500);
      const clipboard = await page.evaluate(() =>
        navigator.clipboard.readText(),
      );
      expect(clipboard.length).toBeGreaterThan(0);
    } finally {
      await cleanup();
    }
  });
});
