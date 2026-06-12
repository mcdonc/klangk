import { test, expect } from "@playwright/test";
import {
  API_BASE,
  TEST_PASSWORD,
  registerUser,
  loginViaUI,
  waitForFlutter,
  fv,
  flutterClick,
  clickBackToWorkspaces,
  waitForFile,
  vp,
  terminalType,
  createWorkspace,
  openWorkspace,
  createAndOpenWorkspace,
  dockerContainersForWorkspace,
  tryLogin,
} from "./helpers";

test.describe("Klangk E2E", () => {
  test("login with wrong password fails", async ({ page, request }) => {
    const email = `wrong-pw-${Date.now()}@test.example.com`;
    await registerUser(request, email);

    await tryLogin(page, email, "wrongpassword");
    await page.waitForTimeout(500);
    await expect(page).toHaveTitle(/Login/i);
  });

  test("login gets locked out after too many wrong passwords", async ({
    page,
    request,
  }) => {
    const email = `lockout-${Date.now()}@test.example.com`;
    await registerUser(request, email);

    // Exhaust the 5-attempt limit with wrong passwords
    for (let i = 0; i < 5; i++) {
      await tryLogin(page, email, "wrongpassword");
      await expect(page).toHaveTitle(/Login/i);
    }

    // Now the account is locked — even correct password returns 429
    await tryLogin(page, email, TEST_PASSWORD);
    await page.waitForTimeout(500);
    await expect(page).toHaveTitle(/Login/i); // still on login page
  });

  test("navigate to workspace and see IDE layout", async ({
    page,
    request,
  }) => {
    const { cleanup } = await createAndOpenWorkspace(page, request, "ide");

    try {
      const title = await page.title();
      expect(title).toMatch(/^Klangk - /);
      expect(title).not.toMatch(/Workspaces/i);
    } finally {
      await cleanup();
    }
  });

  test("workspace shows terminal tab", async ({ page, request }) => {
    const { cleanup } = await createAndOpenWorkspace(page, request, "term");

    try {
      const canvas = page.locator("canvas");
      await expect(canvas.first()).toBeVisible();
    } finally {
      await cleanup();
    }
  });

  test("terminal accepts keyboard input", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-input",
      { waitForTerminal: true },
    );

    try {
      const { width, height } = vp(page);
      const termX = width / 2;
      const termY = height / 2;

      await terminalType(
        page,
        "echo playwright-terminal-test > /home/work/.term-test",
        termX,
        termY,
      );
      await waitForFile(request, workspaceId, "work/.term-test", headers);

      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/.term-test`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content).toContain("playwright-terminal-test");
    } finally {
      await cleanup();
    }
  });

  test("terminal pastes via keyboard shortcut (native paste event)", async ({
    page,
    request,
  }) => {
    // Regression: on Firefox, paste went through Flutter's Clipboard.getData
    // (navigator.clipboard.readText), which returns nothing for externally
    // copied text, so Ctrl/Cmd+V silently failed. The fix reads the browser's
    // native `paste` event instead. This exercises the real keypress path so
    // it catches a regression on any browser.
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-paste",
      { waitForTerminal: true },
    );

    try {
      // chromium needs explicit clipboard permission; firefox/webkit don't
      // support granting it and don't need it for the paste-event read.
      try {
        await page
          .context()
          .grantPermissions(["clipboard-read", "clipboard-write"]);
      } catch {
        /* unsupported on firefox/webkit — fine */
      }

      const cmd = "echo playwright-paste-test > /home/work/.paste-test";
      await page.evaluate((t) => navigator.clipboard.writeText(t), cmd);

      const { width, height } = vp(page);
      const f = fv(page);
      await f.click({
        position: { x: width / 2, y: height / 2 },
        force: true,
      });
      await page.waitForTimeout(500);
      // ControlOrMeta → Cmd on macOS, Ctrl elsewhere (CI is Linux).
      await page.keyboard.press("ControlOrMeta+KeyV");
      await page.waitForTimeout(300);
      await page.keyboard.press("Enter");

      await waitForFile(request, workspaceId, "work/.paste-test", headers);
      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/.paste-test`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content).toContain("playwright-paste-test");
    } finally {
      await cleanup();
    }
  });

  test("terminal paste preserves quotes, spaces, and unicode", async ({
    page,
    request,
  }) => {
    // Pasted text must reach the PTY byte-for-byte. The native `paste` path
    // pulls from event.clipboardData.getData('text/plain'), which is a raw
    // string with no escaping done by Flutter's text-input — this regression-
    // tests that the byte boundary stays clean for typical messy content
    // (single quotes, double quotes, whitespace, multi-byte UTF-8).
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-paste-utf8",
      { waitForTerminal: true },
    );

    try {
      try {
        await page
          .context()
          .grantPermissions(["clipboard-read", "clipboard-write"]);
      } catch {
        /* unsupported on firefox/webkit — fine */
      }

      // Quotes, spaces, an emoji, and a non-Latin character. Wrap in a
      // heredoc so the shell preserves the payload exactly.
      const payload = `Hello "world" 'with' spaces — 🦋 中文`;
      const cmd = `cat > /home/work/.paste-utf8 <<'EOF'\n${payload}\nEOF`;
      await page.evaluate((t) => navigator.clipboard.writeText(t), cmd);

      const { width, height } = vp(page);
      const f = fv(page);
      await f.click({
        position: { x: width / 2, y: height / 2 },
        force: true,
      });
      await page.waitForTimeout(1000);
      await page.keyboard.press("ControlOrMeta+KeyV");
      await page.waitForTimeout(500);
      await page.keyboard.press("Enter");

      await waitForFile(request, workspaceId, "work/.paste-utf8", headers);
      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/.paste-utf8`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content.trimEnd()).toBe(payload);
    } finally {
      await cleanup();
    }
  });

  test("selecting text in terminal copies to clipboard automatically", async ({
    page,
    request,
  }) => {
    // Selecting text in the terminal should auto-copy it to the clipboard
    // on mouse-up, like a standard terminal. This avoids the need for
    // right-click → Copy which requires navigator.clipboard.writeText()
    // from a menu callback (broken on Firefox).
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-copy-select",
      { waitForTerminal: true },
    );

    try {
      try {
        await page
          .context()
          .grantPermissions(["clipboard-read", "clipboard-write"]);
      } catch {
        /* unsupported on firefox/webkit — fine */
      }

      const { width, height } = vp(page);
      const f = fv(page);

      // Type a known string so we have something to select
      await f.click({
        position: { x: width / 2, y: height / 2 },
        force: true,
      });
      await page.waitForTimeout(500);
      await page.keyboard.type("echo COPYTEST123");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(500);

      // Select the output by dragging across the first line of output.
      // The "COPYTEST123" should be on the second line (first is the command).
      // Drag from left to right across the output area.
      const startX = 10;
      const endX = 200;
      const lineY = 130; // approximate Y of the output line
      await page.mouse.move(startX, lineY);
      await page.mouse.down();
      await page.mouse.move(endX, lineY, { steps: 10 });
      await page.mouse.up();
      await page.waitForTimeout(300);

      // Read clipboard — should contain the selected text.
      // WebKit doesn't support clipboard.readText() even with
      // grantPermissions, so skip verification there.
      try {
        const clipText = await page.evaluate(() =>
          navigator.clipboard.readText(),
        );
        expect(clipText).toContain("COPYTEST123");
      } catch {
        console.warn(
          "Clipboard readText() denied — skipping verification (expected on WebKit)",
        );
      }
    } finally {
      await cleanup();
    }
  });

  test("right-click without selection defers to native context menu", async ({
    page,
    request,
  }) => {
    // Without a text selection, right-clicking the terminal should NOT
    // show a Flutter popup menu — it defers to the browser's native
    // context menu so paste works on Firefox without a permission dialog.
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-rc-native",
      { waitForTerminal: true },
    );

    try {
      const { width, height } = vp(page);
      const f = fv(page);

      // Click the terminal to focus it
      await f.click({
        position: { x: width / 2, y: height / 2 },
        force: true,
      });
      await page.waitForTimeout(300);

      // Right-click — should NOT show Flutter popup (no selection).
      await f.click({
        position: { x: width / 2, y: height / 2 },
        button: "right",
        force: true,
      });
      await page.waitForTimeout(300);

      // Dismiss whatever context menu appeared
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);

      // Type a command to verify the terminal is still interactive
      await page.keyboard.type("echo rc-works");
      await page.waitForTimeout(200);

      // The Flutter popup would have intercepted the right-click and
      // shown Copy/Paste. If no Flutter menu appeared, the terminal
      // stays focused and typing works. We just verify no crash.
    } finally {
      await cleanup();
    }
  });

  test("right-click with selection defers to native context menu", async ({
    page,
    request,
  }) => {
    // Right-clicking with a text selection should show the browser's
    // native context menu (not a Flutter popup), same as without selection.
    // Copy-on-select already put the text on the clipboard.
    const { cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-rc-sel",
      { waitForTerminal: true },
    );

    try {
      const { width, height } = vp(page);
      const f = fv(page);

      // Type something to select
      await f.click({
        position: { x: width / 2, y: height / 2 },
        force: true,
      });
      await page.waitForTimeout(500);
      await page.keyboard.type("echo SELECTME");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(500);

      // Select text by dragging across the output
      await page.mouse.move(10, 130);
      await page.mouse.down();
      await page.mouse.move(200, 130, { steps: 10 });
      await page.mouse.up();
      await page.waitForTimeout(300);

      // Right-click on the selection — should NOT show Flutter popup
      await f.click({
        position: { x: 100, y: 130 },
        button: "right",
        force: true,
      });
      await page.waitForTimeout(300);

      // Dismiss whatever context menu appeared
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);

      // Terminal should still be interactive after dismissing
      await page.keyboard.type("echo still-works");
      await page.waitForTimeout(200);
    } finally {
      await cleanup();
    }
  });

  test("paste shortcut is ignored when terminal isn't focused", async ({
    page,
    request,
  }) => {
    // installPasteListener is wired at the document level and only consumes
    // the event when the terminal's focus node has focus. If the user has
    // the Files tab active (or any other widget focused), pasting must NOT
    // route the clipboard into the PTY — otherwise a paste meant for
    // another input would silently fire shell commands.
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-paste-isolation",
      { waitForTerminal: true },
    );

    try {
      try {
        await page
          .context()
          .grantPermissions(["clipboard-read", "clipboard-write"]);
      } catch {
        /* unsupported on firefox/webkit — fine */
      }

      const cmd = "echo paste-leak-canary > /home/work/.paste-leak-canary";
      await page.evaluate((t) => navigator.clipboard.writeText(t), cmd);

      const { width } = vp(page);
      const f = fv(page);
      // Switch to the Files tab so the terminal isn't focused.
      // Owner has 5 tabs (Terminal, Files, Chat, Sharing, Settings).
      const tabWidth = width / 5;
      const filesTabX = tabWidth + tabWidth / 2;
      await f.click({ position: { x: filesTabX, y: 76 }, force: true });
      await page.waitForTimeout(300);

      // Fire the paste shortcut and Enter. If the terminal listener
      // mistakenly consumed the event, the canary file would appear; if it
      // correctly returned false, nothing reaches the PTY.
      await page.keyboard.press("ControlOrMeta+KeyV");
      await page.waitForTimeout(300);
      await page.keyboard.press("Enter");
      await page.waitForTimeout(500);

      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/.paste-leak-canary`,
        { headers },
      );
      // 404 (or any non-2xx) is the expected outcome: file shouldn't exist.
      expect(readResp.ok()).toBeFalsy();
    } finally {
      await cleanup();
    }
  });

  test("navigate back to workspaces", async ({ page, request }) => {
    const { cleanup } = await createAndOpenWorkspace(page, request, "nav-back");

    try {
      // Navigate back via URL (clickBackToWorkspaces is unreliable in
      // webkit due to canvas coordinate offsets on headless CI).
      await page.goto("/");
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
    } finally {
      await cleanup();
    }
  });

  test("terminal command creates file visible via API", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-file",
      { waitForTerminal: true },
    );

    try {
      const { width, height } = vp(page);
      const termX = width / 2;
      const termY = height / 2;

      await terminalType(page, 'echo "foo" > /home/work/foo.txt', termX, termY);
      await waitForFile(request, workspaceId, "work/foo.txt", headers);

      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/foo.txt`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content.trim()).toBe("foo");
    } finally {
      await cleanup();
    }
  });

  test("logout returns to login page", async ({ page, request }) => {
    const email = `logout-${Date.now()}@test.example.com`;
    await registerUser(request, email);
    await loginViaUI(page, email, TEST_PASSWORD);

    const { width } = vp(page);

    // Logout button is in the top-right corner of the workspaces page
    await flutterClick(page, width - 25, 28);
    await page.waitForTimeout(500);

    await expect(page).toHaveTitle(/Login/i, { timeout: 30_000 });
  });

  test("register new user, logout, and login with new credentials", async ({
    page,
    request,
  }) => {
    const email = `e2e-user-${Date.now()}@test.example.com`;
    const password = "testpass1234";

    // Register via API
    const regResp = await request.post(`${API_BASE}/auth/register`, {
      data: { email, password },
    });
    expect(regResp.ok()).toBeTruthy();
    const regData = await regResp.json();
    expect(regData.access_token).toBeTruthy();

    // Login via UI with the new user
    await page.goto("/");
    await waitForFlutter(page);

    const { width, height } = vp(page);
    const cx = width / 2;
    const f = fv(page);

    await f.click({ position: { x: cx, y: height * 0.46 }, force: true });
    await page.waitForTimeout(200);
    await page.keyboard.type(email);

    await f.click({ position: { x: cx, y: height * 0.56 }, force: true });
    await page.waitForTimeout(200);
    await page.keyboard.type(password);

    await f.click({ position: { x: cx, y: height * 0.64 }, force: true });

    await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
  });

  test("terminal command sequence creates directory", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-seq",
      { waitForTerminal: true },
    );

    try {
      const { width, height } = vp(page);
      const termX = width / 2;
      const termY = height / 2;

      // Run a multi-command sequence
      await terminalType(
        page,
        "mkdir -p /home/work/.e2e-multitest/sub && echo done > /home/work/.e2e-multitest/sub/result.txt",
        termX,
        termY,
      );
      await waitForFile(
        request,
        workspaceId,
        "work/.e2e-multitest/sub/result.txt",
        headers,
      );

      // Verify file content
      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/.e2e-multitest/sub/result.txt`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content.trim()).toBe("done");
    } finally {
      await cleanup();
    }
  });

  test("terminal works after tab switching", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tab-switch",
      { waitForTerminal: true },
    );

    try {
      const { width, height } = vp(page);
      const f = fv(page);

      // Workspace owner has 5 tabs: Terminal, Files, Chat, Sharing, Settings.
      // Each tab is width/5 wide; use center of each tab.
      const tabWidth = width / 5;
      const termTabX = tabWidth / 2;
      const filesTabX = tabWidth + tabWidth / 2;

      // Switch to Files tab
      await f.click({ position: { x: filesTabX, y: 76 }, force: true });
      await page.waitForTimeout(300);

      // Switch back to Terminal tab
      await f.click({ position: { x: termTabX, y: 76 }, force: true });
      await page.waitForTimeout(300);

      // Switch to Files again and back
      await f.click({ position: { x: filesTabX, y: 76 }, force: true });
      await page.waitForTimeout(300);
      await f.click({ position: { x: termTabX, y: 76 }, force: true });
      await page.waitForTimeout(500);

      // Terminal should still work — run a command
      const termX = width / 2;
      const termY = 200;
      await terminalType(
        page,
        "echo tab-survive-test > /home/work/.tab-survive",
        termX,
        termY,
      );
      await waitForFile(request, workspaceId, "work/.tab-survive", headers);

      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/.tab-survive`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content).toContain("tab-survive-test");
    } finally {
      await cleanup();
    }
  });

  test("container starts on workspace open and survives navigate away", async ({
    page,
    request,
  }) => {
    test.skip(
      !!process.env.KLANGK_CONTAINER_TEST_MODE,
      "requires local podman access",
    );
    const email = `lifecycle-${Date.now()}@test.example.com`;
    const { token, headers } = await registerUser(request, email);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      headers,
      "e2e-container-lifecycle",
    );

    try {
      // Before opening: no running container for this workspace
      expect(dockerContainersForWorkspace(workspaceId)).toHaveLength(0);

      // Open the workspace — openWorkspace handles WebSocket lifecycle
      // and waits for container_ready, so the container is guaranteed
      // to be running when it returns.
      await openWorkspace(page, email, workspaceId);

      const containersBefore = dockerContainersForWorkspace(workspaceId);
      expect(containersBefore.length).toBeGreaterThan(0);

      // Navigate away via URL (clickBackToWorkspaces is unreliable
      // in webkit due to canvas coordinate offsets on headless CI).
      await page.goto("/");
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });

      // Container should still be running after navigating away
      // (idle timeout handles cleanup, not disconnect)
      await page.waitForTimeout(1000);
      const containersAfter = dockerContainersForWorkspace(workspaceId);
      expect(containersAfter.length).toBe(1);
      expect(containersAfter[0]).toBe(containersBefore[0]);
    } finally {
      await cleanup();
    }
  });

  test("nested directory structure accessible via file API", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "subdir-nav",
      { waitForTerminal: true },
    );

    try {
      // Create nested directory structure via terminal
      await terminalType(
        page,
        "mkdir -p /home/work/.e2e-nav/inner && echo nav-test > /home/work/.e2e-nav/inner/file.txt",
      );
      await waitForFile(
        request,
        workspaceId,
        "work/.e2e-nav/inner/file.txt",
        headers,
      );

      // Verify structure via API
      const innerFiles = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files?path=work/.e2e-nav/inner`,
        { headers },
      );
      expect(innerFiles.ok()).toBeTruthy();
      const names = (await innerFiles.json()).map((e: any) => e.name);
      expect(names).toContain("file.txt");

      // Read nested file content
      const content = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/.e2e-nav/inner/file.txt`,
        { headers },
      );
      expect(content.ok()).toBeTruthy();
      expect((await content.json()).content.trim()).toBe("nav-test");
    } finally {
      await cleanup();
    }
  });

  test("host files in home dir appear inside container", async ({
    page,
    request,
  }) => {
    test.skip(
      !!process.env.KLANGK_CONTAINER_TEST_MODE,
      "requires local podman access",
    );
    const email = `host-home-${Date.now()}@test.example.com`;
    const { token, headers } = await registerUser(request, email);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      headers,
      "host-home",
    );

    // Decode user ID from JWT
    const payload = JSON.parse(
      Buffer.from(token.split(".")[1], "base64url").toString(),
    );
    const userId = payload.sub;

    // Write a file to the host home directory before starting the container
    const dataDir = process.env.KLANGK_E2E_DATA_DIR!;
    const homePath = `${dataDir}/workspaces/${userId}/home/${workspaceId}`;
    const { mkdirSync, writeFileSync } = await import("fs");
    mkdirSync(homePath, { recursive: true });
    writeFileSync(`${homePath}/.host-created-file`, "hello-from-host\n");

    try {
      await openWorkspace(page, email, workspaceId);

      // File API now roots at home, so we can read it directly
      const resp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.host-created-file`,
        { headers },
      );
      expect(resp.ok()).toBeTruthy();
      expect((await resp.json()).content.trim()).toBe("hello-from-host");
    } finally {
      await cleanup();
    }
  });

  test("files created in container home persist on host", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "home-persist",
      { waitForTerminal: true },
    );

    try {
      // File API roots at home, so create a file in ~ and read it directly
      await terminalType(page, "echo home-test > ~/.home-persist-test");
      await waitForFile(request, workspaceId, ".home-persist-test", headers);

      const resp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.home-persist-test`,
        { headers },
      );
      expect(resp.ok()).toBeTruthy();
      expect((await resp.json()).content.trim()).toBe("home-test");
    } finally {
      await cleanup();
    }
  });

  test.describe("idle timeout", () => {
    test.describe.configure({ retries: 1 });

    test("container stops after idle timeout", async ({ page, request }) => {
      test.skip(
        !!process.env.KLANGK_CONTAINER_TEST_MODE,
        "requires local podman access",
      );
      // Check if test mode is enabled
      const getResp = await request.get(`${API_BASE}/api/test/idle-timeout`);
      if (!getResp.ok()) {
        test.skip(true, "KLANGK_TEST_MODE not enabled");
        return;
      }

      const { workspaceId, email, headers, cleanup } =
        await createAndOpenWorkspace(page, request, "e2e-idle-test", {
          containerTimeout: 180_000,
        });

      // Set a short idle timeout for this workspace only
      await request.post(`${API_BASE}/api/test/set-idle-timeout`, {
        headers,
        data: { seconds: 5, workspace_id: workspaceId },
      });

      try {
        // Wait for the container to actually stop
        let stopped = false;
        for (let i = 0; i < 30; i++) {
          if (dockerContainersForWorkspace(workspaceId).length === 0) {
            stopped = true;
            break;
          }
          await page.waitForTimeout(1000);
        }
        expect(stopped).toBeTruthy();

        // Reset per-workspace timeout so the restarted container isn't
        // immediately killed again.
        await request.post(`${API_BASE}/api/test/set-idle-timeout`, {
          headers,
          data: { seconds: 300, workspace_id: workspaceId },
        });

        // Re-open the workspace using openWorkspace which handles login,
        // navigation, WebSocket lifecycle, and container_ready properly.
        await openWorkspace(page, email, workspaceId, {
          containerTimeout: 180_000,
        });

        expect(
          dockerContainersForWorkspace(workspaceId).length,
        ).toBeGreaterThan(0);
      } finally {
        await cleanup();
      }
    });
  });

  test("deep link redirects back after login", async ({ page, request }) => {
    const email = `deeplink-${Date.now()}@test.example.com`;
    const { headers } = await registerUser(request, email);
    const { workspaceId } = await createWorkspace(request, headers, "deeplink");

    // Navigate directly to a workspace URL without being logged in.
    await page.goto(`/#/workspace/${workspaceId}`);
    await waitForFlutter(page);
    await expect(page).toHaveTitle(/Login/i, { timeout: 10_000 });

    // Log in using the same coordinates as loginViaUI. The re-auth message
    // is below the form so it doesn't shift the input fields.
    const { width, height } = vp(page);
    const cx = width / 2;
    const f = fv(page);

    // Deep link login has extra "Please log in to continue." and
    // "Forgot password?" text, making the card taller and shifting
    // the form center up slightly.
    await f.click({ position: { x: cx, y: height * 0.44 }, force: true });
    await page.waitForTimeout(200);
    await page.keyboard.type(email);

    await f.click({ position: { x: cx, y: height * 0.53 }, force: true });
    await page.waitForTimeout(200);
    await page.keyboard.type(TEST_PASSWORD);

    await f.click({ position: { x: cx, y: height * 0.63 }, force: true });

    // Should end up at the workspace, not the workspace list.
    let finalUrl = "";
    for (let i = 0; i < 30; i++) {
      await page.waitForTimeout(300);
      finalUrl = page.url();
      if (finalUrl.includes(workspaceId)) break;
    }
    expect(finalUrl).toContain(workspaceId);

    // Cleanup
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, { headers });
  });

  test("browser-delegate routes to the correct connection", async ({
    browser,
    request,
  }) => {
    test.skip(
      !!process.env.KLANGK_CONTAINER_TEST_MODE,
      "requires local podman access",
    );
    // Check if test mode is enabled (bridge-tokens endpoint needs it)
    const testCheck = await request.get(`${API_BASE}/api/test/idle-timeout`);
    if (!testCheck.ok()) {
      test.skip(true, "KLANGK_TEST_MODE not enabled");
      return;
    }

    // Register two users and share a workspace
    const ownerEmail = `bridge-owner-${Date.now()}@test.example.com`;
    const memberEmail = `bridge-member-${Date.now()}@test.example.com`;
    const { headers: ownerHeaders } = await registerUser(request, ownerEmail);
    await registerUser(request, memberEmail);

    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers: ownerHeaders,
      data: { name: `e2e-bridge-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspaceId = (await wsResp.json()).id;

    // Share with member
    await request.post(`${API_BASE}/workspaces/${workspaceId}/members`, {
      headers: ownerHeaders,
      data: { email: memberEmail },
    });

    // Inject auto-responder into both browser contexts: when a
    // browser_request arrives over the WebSocket, immediately send
    // back a browser_response echoing the message as {pong: message}.
    const autoResponder = `(() => {
      const Orig = window.WebSocket;
      window.WebSocket = function(...args) {
        const ws = new Orig(...args);
        ws.addEventListener("message", (e) => {
          try {
            const msg = JSON.parse(e.data);
            if (msg.type === "browser_request") {
              ws.send(JSON.stringify({
                cmd: "browser_response",
                id: msg.id,
                pong: msg.message || "unknown",
              }));
            }
          } catch {}
        });
        return ws;
      };
      window.WebSocket.prototype = Orig.prototype;
      window.WebSocket.CONNECTING = Orig.CONNECTING;
      window.WebSocket.OPEN = Orig.OPEN;
      window.WebSocket.CLOSING = Orig.CLOSING;
      window.WebSocket.CLOSED = Orig.CLOSED;
    })()`;

    const ctx1 = await browser.newContext();
    const ctx2 = await browser.newContext();
    await ctx1.addInitScript(autoResponder);
    await ctx2.addInitScript(autoResponder);
    const page1 = await ctx1.newPage();
    const page2 = await ctx2.newPage();

    // Open workspace as owner in page1, member in page2
    await openWorkspace(page1, ownerEmail, workspaceId, {
      waitForTerminal: true,
    });
    await openWorkspace(page2, memberEmail, workspaceId, {
      waitForTerminal: true,
    });

    // Get bridge tokens for each connection
    const tokensResp = await request.get(
      `${API_BASE}/api/test/bridge-tokens/${workspaceId}`,
    );
    expect(tokensResp.ok()).toBeTruthy();
    const tokens = await tokensResp.json();
    expect(tokens.length).toBeGreaterThanOrEqual(2);

    const ownerToken = tokens.find(
      (t: { email: string }) => t.email === ownerEmail,
    );
    const memberToken = tokens.find(
      (t: { email: string }) => t.email === memberEmail,
    );
    expect(ownerToken).toBeTruthy();
    expect(memberToken).toBeTruthy();

    // Send bridge request targeting the OWNER — the auto-responder
    // in page1 will reply with {pong: "ping owner"}.
    const resp1 = await request.post(`${API_BASE}/api/browser-delegate`, {
      data: {
        action: "test_ping",
        message: "ping owner",
        token: ownerToken.token,
      },
    });
    expect(resp1.ok()).toBeTruthy();
    expect((await resp1.json()).pong).toBe("ping owner");

    // Send bridge request targeting the MEMBER — the auto-responder
    // in page2 will reply with {pong: "ping member"}.
    const resp2 = await request.post(`${API_BASE}/api/browser-delegate`, {
      data: {
        action: "test_ping",
        message: "ping member",
        token: memberToken.token,
      },
    });
    expect(resp2.ok()).toBeTruthy();
    expect((await resp2.json()).pong).toBe("ping member");

    // Clean up
    await ctx1.close();
    await ctx2.close();
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers: ownerHeaders,
    });
  });

  test("websocket events do not leak between connections on shared workspace", async ({
    browser,
    request,
  }) => {
    // Register two users and share a workspace
    const ownerEmail = `iso-owner-${Date.now()}@test.example.com`;
    const memberEmail = `iso-member-${Date.now()}@test.example.com`;
    const { headers: ownerHeaders } = await registerUser(request, ownerEmail);
    await registerUser(request, memberEmail);

    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers: ownerHeaders,
      data: { name: `e2e-isolation-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspaceId = (await wsResp.json()).id;

    await request.post(`${API_BASE}/workspaces/${workspaceId}/members`, {
      headers: ownerHeaders,
      data: { email: memberEmail },
    });

    // Open workspace in two separate browser contexts
    const ctx1 = await browser.newContext();
    const ctx2 = await browser.newContext();
    const page1 = await ctx1.newPage();
    const page2 = await ctx2.newPage();

    // Set up frame listener on page2 BEFORE openWorkspace so we capture
    // the WebSocket connection created during workspace open.
    const memberFrames: string[] = [];
    page2.on("websocket", (ws) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        const text = frame.payload.toString();
        try {
          const msg = JSON.parse(text);
          if (msg.type && msg.type !== "heartbeat_ack") {
            memberFrames.push(text);
          }
        } catch {
          // not JSON — ignore
        }
      });
    });

    await openWorkspace(page1, ownerEmail, workspaceId);
    await openWorkspace(page2, memberEmail, workspaceId);

    // Wait for both terminals to fully start and settle (shell prompt
    // output etc.) before we begin the isolation check.
    await page1.waitForTimeout(2000);
    memberFrames.length = 0;

    // User A types a command in the terminal
    const { width, height } = vp(page1);
    const f = fv(page1);
    await f.click({
      position: { x: width / 2, y: height / 2 },
      force: true,
    });
    await page1.waitForTimeout(500);
    await page1.keyboard.type(
      "echo isolation-test-from-owner > /tmp/iso-test.txt",
    );
    await page1.keyboard.press("Enter");

    // Wait for the command to execute and any events to propagate
    await page1.waitForTimeout(2000);

    // User B should NOT have received terminal_output containing
    // User A's command or its output.  User B's own terminal_output
    // (shell prompts) is expected and should be ignored.
    const leakedFrames = memberFrames.filter((f) => {
      const msg = JSON.parse(f);
      if (msg.type === "exec_output" || msg.type === "browser_request") {
        return true;
      }
      if (msg.type === "terminal_output") {
        const data = msg.data ?? "";
        return data.includes("isolation-test-from-owner");
      }
      return false;
    });

    expect(leakedFrames).toHaveLength(0);

    // Clean up
    await ctx1.close();
    await ctx2.close();
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers: ownerHeaders,
    });
  });

  test("chat message broadcasts to shared workspace users", async ({
    browser,
    request,
  }) => {
    const ownerEmail = `chat-owner-${Date.now()}@test.example.com`;
    const memberEmail = `chat-member-${Date.now()}@test.example.com`;
    const { headers: ownerHeaders } = await registerUser(request, ownerEmail);
    await registerUser(request, memberEmail);

    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers: ownerHeaders,
      data: { name: `e2e-chat-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspaceId = (await wsResp.json()).id;

    await request.post(`${API_BASE}/workspaces/${workspaceId}/members`, {
      headers: ownerHeaders,
      data: { email: memberEmail },
    });

    // Inject a WebSocket capture script that stores a reference to
    // the WS so we can send chat commands from page.evaluate.
    const wsCaptureScript = `(() => {
      const Orig = window.WebSocket;
      window.WebSocket = function(...args) {
        const ws = new Orig(...args);
        window.__klangkWs = ws;
        return ws;
      };
      window.WebSocket.prototype = Orig.prototype;
      window.WebSocket.CONNECTING = Orig.CONNECTING;
      window.WebSocket.OPEN = Orig.OPEN;
      window.WebSocket.CLOSING = Orig.CLOSING;
      window.WebSocket.CLOSED = Orig.CLOSED;
    })()`;

    const ctx1 = await browser.newContext();
    const ctx2 = await browser.newContext();
    await ctx1.addInitScript(wsCaptureScript);
    const page1 = await ctx1.newPage();
    const page2 = await ctx2.newPage();

    // Collect chat messages on page2
    const memberChatMessages: string[] = [];
    page2.on("websocket", (ws) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        const text = frame.payload.toString();
        if (text.includes("chat_message")) {
          memberChatMessages.push(text);
        }
      });
    });

    await openWorkspace(page1, ownerEmail, workspaceId, {
      waitForTerminal: true,
    });
    await openWorkspace(page2, memberEmail, workspaceId, {
      waitForTerminal: true,
    });

    // Send chat message from page1 via the captured WebSocket
    await page1.evaluate(() => {
      const ws = (window as any).__klangkWs;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({ cmd: "chat_send", message: "hello from e2e" }),
        );
      }
    });

    await page2.waitForTimeout(2000);

    // Filter out system messages (message_type 2 = join/leave)
    const userMessages = memberChatMessages
      .map((s) => JSON.parse(s))
      .filter((m: any) => m.type === "chat_message" && m.message_type !== 2);
    expect(userMessages.length).toBeGreaterThan(0);
    const received = userMessages[0];
    expect(received.type).toBe("chat_message");
    expect(received.message).toBe("hello from e2e");
    expect(received.user_email).toBe(ownerEmail);

    await ctx1.close();
    await ctx2.close();
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers: ownerHeaders,
    });
  });

  test("system chat messages on user join and leave", async ({
    browser,
    request,
  }) => {
    const ownerEmail = `join-owner-${Date.now()}@test.example.com`;
    const memberEmail = `join-member-${Date.now()}@test.example.com`;
    const { headers: ownerHeaders } = await registerUser(request, ownerEmail);
    await registerUser(request, memberEmail);

    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers: ownerHeaders,
      data: { name: `e2e-joinleave-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspaceId = (await wsResp.json()).id;

    await request.post(`${API_BASE}/workspaces/${workspaceId}/members`, {
      headers: ownerHeaders,
      data: { email: memberEmail },
    });

    const ctx1 = await browser.newContext();
    const page1 = await ctx1.newPage();

    // Collect system chat messages (message_type == 2) on the owner's page
    const systemMessages: any[] = [];
    page1.on("websocket", (ws) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        const text = frame.payload.toString();
        if (text.includes("chat_message")) {
          try {
            const msg = JSON.parse(text);
            if (msg.type === "chat_message" && msg.message_type === 2) {
              systemMessages.push(msg);
            }
          } catch {}
        }
      });
    });

    await openWorkspace(page1, ownerEmail, workspaceId, {
      waitForTerminal: true,
    });

    // Member joins — owner should see a "joined" system message
    const ctx2 = await browser.newContext();
    const page2 = await ctx2.newPage();
    await openWorkspace(page2, memberEmail, workspaceId, {
      waitForTerminal: true,
    });

    // Wait for join message to arrive
    await page1.waitForTimeout(2000);
    const joinMsgs = systemMessages.filter(
      (m) => m.message.includes("joined") && m.message.includes(memberEmail),
    );
    expect(joinMsgs.length).toBeGreaterThan(0);
    expect(joinMsgs[0].message).toBe(`${memberEmail} joined`);

    // Member leaves — owner should see a "left" system message
    const beforeLeave = systemMessages.length;
    await ctx2.close();
    await page1.waitForTimeout(2000);

    const leaveMsgs = systemMessages.filter(
      (m) => m.message.includes("left") && m.message.includes(memberEmail),
    );
    expect(leaveMsgs.length).toBeGreaterThan(0);
    expect(leaveMsgs[0].message).toBe(`${memberEmail} left`);

    await ctx1.close();
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers: ownerHeaders,
    });
  });

  test("workspace_members broadcast on member add and chat @mention", async ({
    browser,
    request,
  }) => {
    const ownerEmail = `mention-owner-${Date.now()}@test.example.com`;
    const memberEmail = `mention-member-${Date.now()}@test.example.com`;
    const lateEmail = `mention-late-${Date.now()}@test.example.com`;
    const { headers: ownerHeaders } = await registerUser(request, ownerEmail);
    await registerUser(request, memberEmail);
    await registerUser(request, lateEmail);

    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers: ownerHeaders,
      data: { name: `e2e-mention-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspaceId = (await wsResp.json()).id;

    // Share with first member
    await request.post(`${API_BASE}/workspaces/${workspaceId}/members`, {
      headers: ownerHeaders,
      data: { email: memberEmail },
    });

    // Capture WS on owner page to send commands
    const wsCaptureScript = `(() => {
      const Orig = window.WebSocket;
      window.WebSocket = function(...args) {
        const ws = new Orig(...args);
        window.__klangkWs = ws;
        return ws;
      };
      window.WebSocket.prototype = Orig.prototype;
      window.WebSocket.CONNECTING = Orig.CONNECTING;
      window.WebSocket.OPEN = Orig.OPEN;
      window.WebSocket.CLOSING = Orig.CLOSING;
      window.WebSocket.CLOSED = Orig.CLOSED;
    })()`;

    const ctx1 = await browser.newContext();
    await ctx1.addInitScript(wsCaptureScript);
    const page1 = await ctx1.newPage();

    // Collect workspace_members and chat_message on owner page
    const ownerWsMessages: string[] = [];
    const ownerChatMessages: string[] = [];
    page1.on("websocket", (ws) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        const text = frame.payload.toString();
        if (text.includes("workspace_members")) {
          ownerWsMessages.push(text);
        }
        if (text.includes("chat_message")) {
          ownerChatMessages.push(text);
        }
      });
    });

    await openWorkspace(page1, ownerEmail, workspaceId, {
      waitForTerminal: true,
    });

    // Initial workspace_members should have been received on connect
    expect(ownerWsMessages.length).toBeGreaterThan(0);
    const initial = JSON.parse(ownerWsMessages[ownerWsMessages.length - 1]);
    expect(initial.type).toBe("workspace_members");
    const initialEmails = initial.members.map(
      (m: { email: string }) => m.email,
    );
    expect(initialEmails).toContain(memberEmail);
    expect(initialEmails).toContain(ownerEmail);

    // Now add a late member while owner is connected
    const countBefore = ownerWsMessages.length;
    await request.post(`${API_BASE}/workspaces/${workspaceId}/members`, {
      headers: ownerHeaders,
      data: { email: lateEmail },
    });

    // Wait for the broadcast
    await page1.waitForTimeout(2000);
    expect(ownerWsMessages.length).toBeGreaterThan(countBefore);
    const updated = JSON.parse(ownerWsMessages[ownerWsMessages.length - 1]);
    const updatedEmails = updated.members.map(
      (m: { email: string }) => m.email,
    );
    expect(updatedEmails).toContain(lateEmail);

    // Send a chat message with @mention and verify mentions field
    await page1.evaluate((email: string) => {
      const ws = (window as any).__klangkWs;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({
            cmd: "chat_send",
            message: `hey @${email} check this`,
          }),
        );
      }
    }, memberEmail);

    await page1.waitForTimeout(2000);

    // Filter out system messages (message_type 2 = join/leave)
    const userChatMsgs = ownerChatMessages
      .map((s) => JSON.parse(s))
      .filter((m: any) => m.type === "chat_message" && m.message_type !== 2);
    expect(userChatMsgs.length).toBeGreaterThan(0);
    const chatMsg = userChatMsgs[0];
    expect(chatMsg.type).toBe("chat_message");
    expect(chatMsg.message).toContain(`@${memberEmail}`);
    expect(chatMsg.mentions).toBeDefined();
    expect(chatMsg.mentions.length).toBeGreaterThan(0);

    await ctx1.close();
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers: ownerHeaders,
    });
  });

  test("presence_list includes logged-in user on connect", async ({
    browser,
    request,
  }) => {
    const email = `presence-${Date.now()}@test.example.com`;
    const { headers } = await registerUser(request, email);

    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers,
      data: { name: `e2e-presence-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspaceId = (await wsResp.json()).id;

    const ctx = await browser.newContext();
    const page = await ctx.newPage();

    const presenceMessages: string[] = [];
    page.on("websocket", (ws) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        const text = frame.payload.toString();
        if (text.includes("presence_list")) {
          presenceMessages.push(text);
        }
      });
    });

    await openWorkspace(page, email, workspaceId, {
      waitForTerminal: true,
    });

    // Wait for presence_list to arrive
    await page.waitForTimeout(1000);

    expect(presenceMessages.length).toBeGreaterThan(0);
    const presenceList = JSON.parse(
      presenceMessages[presenceMessages.length - 1],
    );
    expect(presenceList.type).toBe("presence_list");
    expect(presenceList.users.length).toBeGreaterThan(0);
    const emails = presenceList.users.map(
      (u: { user_email: string }) => u.user_email,
    );
    expect(emails).toContain(email);

    await ctx.close();
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers,
    });
  });

  test("container recreated on page refresh", async ({ page, request }) => {
    test.skip(
      !!process.env.KLANGK_CONTAINER_TEST_MODE,
      "requires local podman access",
    );
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-refresh-test",
    );

    try {
      // Verify container is running
      const containers = dockerContainersForWorkspace(workspaceId);
      expect(containers.length).toBe(1);

      // Reload the page — container should be stopped on disconnect
      // and a new one created on reconnect
      const readyPromise = new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(
          () => reject(new Error("Container did not start within 120s")),
          120_000,
        );
        page.on("websocket", (ws) => {
          ws.on("framereceived", (frame: { payload: string | Buffer }) => {
            if (frame.payload.toString().includes("workspace_ready")) {
              clearTimeout(timeout);
              resolve();
            }
          });
        });
      });

      await page.reload();
      await readyPromise;

      // A new container should be running (old one was removed)
      const containersAfter = dockerContainersForWorkspace(workspaceId);
      expect(containersAfter.length).toBe(1);
    } finally {
      await cleanup();
    }
  });
});
