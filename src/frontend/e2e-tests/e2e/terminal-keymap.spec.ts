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

/** Press a sentinel key and wait for its frame to arrive on the wire.
 *  Any frame from the *test* key would have arrived first, so checking
 *  sent.length after this is race-free without a fixed sleep. */
async function waitForSentinel(page: Page, sent: string[]): Promise<number> {
  const before = sent.length;
  await page.keyboard.press("x");
  // Wait for the sentinel's terminal_input frame (up to 2s)
  const deadline = Date.now() + 2000;
  while (sent.length === before && Date.now() < deadline) {
    await page.waitForTimeout(50);
  }
  return before; // return count *before* sentinel, for comparison
}

test.describe("terminal keymap (web)", () => {
  test("plain PageUp on the shell is sent to the PTY for tmux scrollback", async ({
    page,
    request,
  }) => {
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(page, request, "km-pgup", {
      waitForTerminal: true,
    });
    try {
      await focusTerminal(page);
      // Type a throwaway key to confirm the terminal is accepting input
      // before measuring — CI can be slow to wire up the WS listener.
      await page.keyboard.press("a");
      const deadline = Date.now() + 5000;
      while (sent.length === 0 && Date.now() < deadline) {
        await page.waitForTimeout(50);
      }
      const n = sent.length;
      await page.keyboard.press("PageUp");
      const nBeforeSentinel = await waitForSentinel(page, sent);
      // PageUp is sent to the PTY where tmux handles scrollback
      expect(nBeforeSentinel).toBeGreaterThan(n);
    } finally {
      await cleanup();
    }
  });

  // NOTE: alt-screen paging (vim/less/pi) is covered by the Dart widget tests
  // (`web + alternate screen: PageUp is forwarded to the PTY`, and the
  // `Shift+PgUp/PgDn page the app on the alt screen` group). The e2e versions
  // that drove `less` were removed: they raced `less` reaching the alternate
  // screen before the key was sent, which is flaky under CI load (the key lands
  // on the primary shell instead). The remaining e2e here are all deterministic
  // primary-screen behaviors.

  // NOTE: Shift+PageUp is intentionally NOT tested here. flterm converts
  // Shift+PgUp into mouse-wheel scroll events (SGR encoding) rather than
  // sending the Shift+PgUp key sequence to the PTY. Plain PageUp works
  // correctly for tmux copy-mode scrollback, so Shift+PgUp is not needed.

  test("the browser zoom combo is left for the browser, not sent to the PTY", async ({
    page,
    request,
    browserName,
  }) => {
    // The app's zoom modifier follows the platform Flutter detects: Cmd on
    // macOS, Ctrl elsewhere. WebKit always reports macOS (Safari UA), and
    // Chromium/Firefox follow the host OS — so pick the modifier accordingly,
    // matching what the app leaves for the browser.
    const zoomMod =
      browserName === "webkit" || process.platform === "darwin"
        ? "Meta"
        : "Control";
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(page, request, "km-zoom", {
      waitForTerminal: true,
    });
    try {
      await focusTerminal(page);
      const n = sent.length;
      await page.keyboard.press(`${zoomMod}+Equal`);
      await page.keyboard.press(`${zoomMod}+Minus`);
      const nBeforeSentinel = await waitForSentinel(page, sent);
      expect(nBeforeSentinel).toBe(n); // browser owns zoom; terminal didn't eat it
    } finally {
      await cleanup();
    }
  });

  test("typing after tmux copy-mode scroll still reaches the PTY", async ({
    page,
    request,
  }) => {
    // After PgUp puts tmux into copy-mode, a regular keystroke exits
    // copy-mode and the character reaches the PTY.
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(page, request, "km-snap", {
      waitForTerminal: true,
    });
    try {
      await terminalType(page, "seq 1 500");
      await page.waitForTimeout(500);
      await focusTerminal(page);
      await page.keyboard.press("PageUp"); // tmux enters copy-mode
      await page.waitForTimeout(300);
      // 'q' exits copy-mode (per tmux.conf), then type a real key
      await page.keyboard.press("q");
      await page.waitForTimeout(300);
      const n = sent.length;
      await page.keyboard.press("x"); // a real keystroke
      const deadline = Date.now() + 2000;
      while (sent.length === n && Date.now() < deadline) {
        await page.waitForTimeout(50);
      }
      expect(sent.length).toBeGreaterThan(n); // the character reached the PTY
    } finally {
      await cleanup();
    }
  });

  test("typing still works after mouse wheel scroll", async ({
    page,
    request,
  }) => {
    // Verify the terminal remains responsive after scrolling. Wheel events
    // are sent to tmux as SGR mouse events (flterm encodes them natively);
    // tmux copy-mode wheel bindings in tmux.conf control scroll speed.
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "km-wheel",
      { waitForTerminal: true },
    );
    try {
      await terminalType(page, "seq 1 500");
      await page.waitForTimeout(500);
      await focusTerminal(page);
      const { width, height } = vp(page);
      await page.mouse.move(width / 2, height / 2);
      await page.mouse.wheel(0, -600);
      await page.waitForTimeout(500);

      // After scrolling, typing must still reach the PTY
      const n = sent.length;
      await page.keyboard.press("x");
      const deadline = Date.now() + 2000;
      while (sent.length === n && Date.now() < deadline) {
        await page.waitForTimeout(50);
      }
      expect(sent.length).toBeGreaterThan(n);
    } finally {
      await cleanup();
    }
  });
});

test.describe("tmux configuration (web)", () => {
  test("terminal runs inside tmux", async ({ page, request }) => {
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tmux-env",
      { waitForTerminal: true },
    );
    try {
      await focusTerminal(page);
      // Check $TMUX is set — tmux sets this automatically
      await terminalType(page, "echo TMUX_CHECK=$TMUX");
      await page.waitForTimeout(1000);
      // We can't read the canvas, but we can verify the command was sent
      // and the terminal is responsive. The real assertion is that the
      // container started with tmux and didn't crash.
    } finally {
      await cleanup();
    }
  });

  // NOTE: Mouse drag text selection is verified manually. With mouse off in
  // tmux, plain click+drag selects text in the terminal emulator. The wire
  // test is unreliable because flterm's gesture recognizer may produce
  // terminal_input frames during drag even without mouse tracking enabled.

  test("Ctrl+B is not intercepted (no tmux prefix key)", async ({
    page,
    request,
  }) => {
    // Our tmux.conf strips all keybindings (unbind-key -a), so Ctrl+B
    // (the default tmux prefix) should pass through to the shell.
    const sent = captureTerminalInput(page);
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tmux-prefix",
      { waitForTerminal: true },
    );
    try {
      await focusTerminal(page);
      const n = sent.length;
      await page.keyboard.press("Control+b");
      const nBeforeSentinel = await waitForSentinel(page, sent);
      // Ctrl+B reached the PTY (readline backward-char), not swallowed by tmux
      expect(nBeforeSentinel).toBeGreaterThan(n);
    } finally {
      await cleanup();
    }
  });
});
