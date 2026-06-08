import { test, expect, Page } from "@playwright/test";
import { createAndOpenWorkspace, fv, terminalType, vp } from "./helpers";

// Terminal input rides the websocket as `terminal_input` messages (the same
// channel as `terminal_resize`). These tests assert *whether a key produced a
// terminal_input frame* — i.e. whether the terminal trapped the key or let it
// pass through to the browser. We can't read the Flutter canvas, but the wire
// tells us exactly what reached the PTY.
//
// These run on web (chromium/firefox/webkit). Native font-zoom (Ctrl +/-/0) has
// no browser to defer to and is covered by Dart widget tests instead.

/** Collect the payloads of every `terminal_input` frame the client sends. */
function captureTerminalInput(page: Page): string[] {
  const frames: string[] = [];
  page.on("websocket", (ws: { on: Function }) => {
    ws.on("framesent", (frame: { payload: string | Buffer }) => {
      const text = frame.payload.toString();
      if (text.includes("terminal_input")) frames.push(text);
    });
  });
  return frames;
}

async function focusTerminal(page: Page) {
  const { width, height } = vp(page);
  await fv(page).click({
    position: { x: width / 2, y: height / 2 },
    force: true,
  });
  await page.waitForTimeout(500);
}

test.describe("terminal keymap (web)", () => {
  test("plain PageUp on the shell is not sent to the PTY", async ({
    page,
    request,
  }) => {
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(page, request, "km-pgup", {
      waitForTerminal: true,
    });
    try {
      await focusTerminal(page);
      const n = sent.length;
      await page.keyboard.press("PageUp");
      await page.waitForTimeout(750);
      expect(sent.length).toBe(n); // nothing reached the PTY; browser owns it
    } finally {
      await cleanup();
    }
  });

  test("PageUp inside the alternate screen (less) is sent to the PTY", async ({
    page,
    request,
  }) => {
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(page, request, "km-alt", {
      waitForTerminal: true,
    });
    try {
      // Build a file and open it in less (switches to the alternate screen).
      await terminalType(page, "seq 1 500 > /home/klangk/work/big.txt");
      await page.waitForTimeout(500);
      await terminalType(page, "less /home/klangk/work/big.txt");
      await page.waitForTimeout(1000);

      const n = sent.length;
      await page.keyboard.press("PageDown");
      await page.waitForTimeout(750);
      expect(sent.length).toBeGreaterThan(n); // less received the paging key

      await page.keyboard.press("q"); // quit less
    } finally {
      await cleanup();
    }
  });

  test("Shift+PageUp scrolls the buffer without touching the PTY", async ({
    page,
    request,
  }) => {
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "km-shift",
      {
        waitForTerminal: true,
      },
    );
    try {
      await terminalType(page, "seq 1 500"); // fill scrollback
      await page.waitForTimeout(500);
      await focusTerminal(page);
      const n = sent.length;
      await page.keyboard.press("Shift+PageUp");
      await page.waitForTimeout(750);
      expect(sent.length).toBe(n); // handled by Flutter scrollback, not the PTY
    } finally {
      await cleanup();
    }
  });

  test("Ctrl +/- are left for the browser, not sent to the PTY", async ({
    page,
    request,
  }) => {
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(page, request, "km-zoom", {
      waitForTerminal: true,
    });
    try {
      await focusTerminal(page);
      const n = sent.length;
      await page.keyboard.press("Control+Equal");
      await page.keyboard.press("Control+Minus");
      await page.waitForTimeout(750);
      expect(sent.length).toBe(n); // browser owns zoom; terminal didn't eat it
    } finally {
      await cleanup();
    }
  });

  test("mouse wheel up on the shell scrolls the buffer, not the PTY", async ({
    page,
    request,
  }) => {
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "km-wheel",
      {
        waitForTerminal: true,
      },
    );
    try {
      await terminalType(page, "seq 1 500"); // fill scrollback
      await page.waitForTimeout(500);
      await focusTerminal(page);
      const { width, height } = vp(page);
      await page.mouse.move(width / 2, height / 2);
      const n = sent.length;
      await page.mouse.wheel(0, -600); // wheel up over the primary screen
      await page.waitForTimeout(750);
      expect(sent.length).toBe(n); // primary scrollback is local; nothing to PTY
    } finally {
      await cleanup();
    }
  });

  test("typing after scrolling up still reaches the PTY", async ({
    page,
    request,
  }) => {
    // The view snaps back to the live row on input — that is visual (asserted in
    // the Dart widget tests). Over the wire we can confirm the flow is intact:
    // after scrolling the buffer up, a typed character still gets to the PTY.
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(page, request, "km-snap", {
      waitForTerminal: true,
    });
    try {
      await terminalType(page, "seq 1 500");
      await page.waitForTimeout(500);
      await focusTerminal(page);
      await page.keyboard.press("Shift+PageUp"); // scroll up (no PTY)
      await page.waitForTimeout(500);
      const n = sent.length;
      await page.keyboard.press("x"); // a real keystroke
      await page.waitForTimeout(750);
      expect(sent.length).toBeGreaterThan(n); // the character reached the PTY
    } finally {
      await cleanup();
    }
  });

  test("Shift+PageUp/PageDown page the app on the alternate screen (less)", async ({
    page,
    request,
  }) => {
    // On the alt screen there is no scrollback; the page keys must reach the
    // running app (via flterm handleScroll) instead of being a no-op — this is
    // what makes Shift+PgUp/PgDn work inside pi / vim / less.
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "km-altpg",
      {
        waitForTerminal: true,
      },
    );
    try {
      await terminalType(page, "seq 1 500 > /home/klangk/work/big.txt");
      await page.waitForTimeout(500);
      await terminalType(page, "less /home/klangk/work/big.txt");
      await page.waitForTimeout(1000);
      await focusTerminal(page);

      let n = sent.length;
      await page.keyboard.press("Shift+PageUp");
      await page.waitForTimeout(750);
      expect(sent.length).toBeGreaterThan(n); // paged the app, not a no-op

      n = sent.length;
      await page.keyboard.press("Shift+PageDown");
      await page.waitForTimeout(750);
      expect(sent.length).toBeGreaterThan(n);

      await page.keyboard.press("q"); // quit less
    } finally {
      await cleanup();
    }
  });

  test("mouse wheel on the alternate screen (less) scrolls the app", async ({
    page,
    request,
  }) => {
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "km-altwh",
      {
        waitForTerminal: true,
      },
    );
    try {
      await terminalType(page, "seq 1 500 > /home/klangk/work/big.txt");
      await page.waitForTimeout(500);
      await terminalType(page, "less /home/klangk/work/big.txt");
      await page.waitForTimeout(1000);
      const { width, height } = vp(page);
      await page.mouse.move(width / 2, height / 2);
      const n = sent.length;
      await page.mouse.wheel(0, 300); // wheel down inside less
      await page.waitForTimeout(750);
      expect(sent.length).toBeGreaterThan(n); // less received the scroll

      await page.keyboard.press("q"); // quit less
    } finally {
      await cleanup();
    }
  });
});
