import { test, expect, Page, APIRequestContext } from "@playwright/test";
import AdmZip from "adm-zip";
import { execSync } from "child_process";

// Each test registers its own user and creates its own workspace. This ensures
// tests are fully isolated — logout in one test can't kill another test's
// containers, and parallel execution is safe because no state is shared.

const BACKEND_PORT = process.env.BARK_E2E_PORT || "18997";
const API_BASE = `http://localhost:${BACKEND_PORT}`;
const TEST_PASSWORD = "testpass";

/** Register a new user via API (test mode allows unauthenticated registration).
 *  Returns { token, headers }. */
async function registerUser(
  request: APIRequestContext,
  username: string,
): Promise<{ token: string; headers: Record<string, string> }> {
  const resp = await request.post(`${API_BASE}/auth/register`, {
    data: { username, password: TEST_PASSWORD },
  });
  expect(resp.ok()).toBeTruthy();
  const data = await resp.json();
  const token = data.access_token;
  return { token, headers: { Authorization: `Bearer ${token}` } };
}

/** Log in via the UI by typing credentials into the Flutter login form. */
async function loginViaUI(page: Page, username: string, password: string) {
  await page.goto("/");
  await waitForFlutter(page);

  const { width, height } = vp(page);
  const cx = width / 2;
  const f = fv(page);

  await f.click({ position: { x: cx, y: height * 0.47 }, force: true });
  await page.waitForTimeout(300);
  await page.keyboard.type(username);

  await f.click({ position: { x: cx, y: height * 0.55 }, force: true });
  await page.waitForTimeout(300);
  await page.keyboard.type(password);

  await f.click({ position: { x: cx, y: height * 0.66 }, force: true });
  await expect(page).toHaveTitle(/Workspaces/i, { timeout: 15_000 });
}

// Flutter Web renders to <canvas> inside <flutter-view>, so standard DOM
// locators (text=, role=, input) don't work. We interact via coordinate
// clicks on <flutter-view> and verify state via page title and screenshots.

async function waitForFlutter(page: Page) {
  await page.waitForFunction(
    () => !document.body.textContent?.includes("Loading, please wait"),
    { timeout: 90_000 },
  );
  await page.waitForTimeout(1000);
}

function fv(page: Page) {
  return page.locator("flutter-view");
}

/** Click a position on the Flutter canvas using raw mouse events.
 *  Locator clicks with force:true sometimes don't fire Flutter's tap
 *  recognizer (especially on small targets like IconButtons). Using
 *  page.mouse.move + click sends proper pointer events that Flutter
 *  handles reliably across all browser engines. */
async function flutterClick(page: Page, x: number, y: number) {
  const box = await fv(page).boundingBox();
  const absX = (box?.x ?? 0) + x;
  const absY = (box?.y ?? 0) + y;
  await page.mouse.move(absX, absY);
  await page.waitForTimeout(200);
  await page.mouse.click(absX, absY);
}

/** Poll the files API until a specific file appears. */
async function waitForFile(
  request: APIRequestContext,
  workspaceId: string,
  path: string,
  headers: Record<string, string>,
  timeout = 30_000,
) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const resp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=${encodeURIComponent(path)}`,
        { headers },
      );
      if (resp.ok()) return;
    } catch {
      // Not ready yet
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`File ${path} did not appear within ${timeout}ms`);
}

function vp(page: Page) {
  return page.viewportSize() || { width: 1280, height: 720 };
}

/** Type a prompt into the chat input and verify it was received by the backend.
 *  On slower environments (CI, WebKit), the Flutter chat widget may not be
 *  fully wired up when container_ready fires. This function retries once
 *  if the user message doesn't appear in the backend within 10s. */
async function sendPrompt(
  page: Page,
  request: APIRequestContext,
  workspaceId: string,
  headers: Record<string, string>,
  text: string,
) {
  const { height } = vp(page);

  const typeAndSend = async () => {
    await flutterClick(page, 240, height - 30);
    await page.waitForTimeout(500);
    await page.keyboard.type(text);
    await page.waitForTimeout(300);
    await page.keyboard.press("Enter");
  };

  const checkReceived = async (): Promise<boolean> => {
    for (let i = 0; i < 10; i++) {
      await page.waitForTimeout(2000);
      const msgResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/messages`,
        { headers },
      );
      if (msgResp.ok()) {
        const messages = await msgResp.json();
        if (messages.some((m: any) => m.entry_type === "user")) return true;
      }
    }
    return false;
  };

  await typeAndSend();
  if (await checkReceived()) return;
  // Retry once — the chat widget may not have been ready
  await typeAndSend();
  if (await checkReceived()) return;
  throw new Error(
    `Prompt "${text}" was not received by the backend after 2 attempts`,
  );
}

/** Click the terminal area, wait for it to be interactive, then type a command and press Enter. */
async function terminalType(
  page: Page,
  command: string,
  termX?: number,
  termY?: number,
) {
  const { width, height } = vp(page);
  const x = termX ?? (492 + width) / 2;
  const y = termY ?? height / 2;
  const f = fv(page);

  await f.click({ position: { x, y }, force: true });
  await page.waitForTimeout(2000);
  await page.keyboard.type(command);
  await page.keyboard.press("Enter");
}

/** Create a workspace via API. Returns workspace ID and cleanup function. */
async function createWorkspace(
  request: APIRequestContext,
  headers: Record<string, string>,
  namePrefix: string,
): Promise<{
  workspaceId: string;
  cleanup: () => Promise<void>;
}> {
  const name = `${namePrefix}-${Date.now()}`;
  const createResp = await request.post(
    `${API_BASE}/workspaces?name=${encodeURIComponent(name)}`,
    { headers },
  );
  if (!createResp.ok()) {
    const body = await createResp.text();
    throw new Error(
      `Workspace creation failed: ${createResp.status()} ${body}`,
    );
  }
  const workspace = await createResp.json();
  const workspaceId = workspace.id;

  return {
    workspaceId,
    cleanup: async () => {
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
    },
  };
}

/** Open a workspace in the browser and wait for the container to be ready. */
async function openWorkspace(
  page: Page,
  username: string,
  workspaceId: string,
) {
  // Set up WebSocket listener before login so we catch all WebSocket connections
  const readyPromise = new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(
      () => reject(new Error("Container did not become ready within 120s")),
      120_000,
    );
    const listenForReady = (ws: { on: Function }) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        if (frame.payload.toString().includes("container_ready")) {
          clearTimeout(timeout);
          resolve();
        }
      });
    };
    // Listen on any new WebSocket connections
    page.on("websocket", listenForReady);
  });

  await loginViaUI(page, username, TEST_PASSWORD);
  // Use full URL (not just #fragment) so the page reloads and creates a new
  // WebSocket — a hash-only change is handled internally by Flutter's router
  // without opening a new WebSocket, so our listener would never fire.
  await page.goto(`/#/workspace/${workspaceId}`);
  await readyPromise;

  // Extra settle time for the UI to render after container ready.
  // WebKit on CI needs more time for Flutter to fully process the
  // container_ready event and establish bidirectional chat.
  await page.waitForTimeout(4000);
}

/** Convenience: register user, create workspace, open it. */
async function createAndOpenWorkspace(
  page: Page,
  request: APIRequestContext,
  namePrefix: string,
): Promise<{
  workspaceId: string;
  token: string;
  headers: Record<string, string>;
  cleanup: () => Promise<void>;
}> {
  const username = `${namePrefix}-${Date.now()}`;
  const { token, headers } = await registerUser(request, username);
  const { workspaceId, cleanup } = await createWorkspace(
    request,
    headers,
    namePrefix,
  );
  await openWorkspace(page, username, workspaceId);
  return { workspaceId, token, headers, cleanup };
}

function dockerContainersForWorkspace(workspaceId: string): string[] {
  const output = execSync(
    `docker ps --filter "label=bark.workspace-id=${workspaceId}" --format "{{.ID}}"`,
    { encoding: "utf-8" },
  );
  return output.trim().split("\n").filter(Boolean);
}

// Layout coordinates at 1280x720:
// Chat panel: x 0-486 (38%)
// Right panel: x 492-1280
// Tab bar (Terminal/Files): y ~0-32 in right panel
// Chat input: bottom of left panel, ~y 690
// Debug bar: bottom of right panel
// Back button: x ~25, y ~28

test.describe("Bark E2E", () => {
  test("login with wrong password fails", async ({ page, request }) => {
    const username = `wrong-pw-${Date.now()}`;
    await registerUser(request, username);
    await expect(loginViaUI(page, username, "wrongpassword")).rejects.toThrow();
    await expect(page).toHaveTitle(/Login/i);
  });

  test("navigate to workspace and see IDE layout", async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);
    const { cleanup } = await createAndOpenWorkspace(page, request, "ide");

    try {
      const title = await page.title();
      expect(title).toMatch(/^Bark - /);
      expect(title).not.toMatch(/Workspaces/i);
    } finally {
      await cleanup();
    }
  });

  test("workspace shows terminal tab", async ({ page, request }) => {
    test.setTimeout(120_000);
    const { cleanup } = await createAndOpenWorkspace(page, request, "term");

    try {
      const canvas = page.locator("canvas");
      await expect(canvas.first()).toBeVisible();
    } finally {
      await cleanup();
    }
  });

  test("switch to Files tab and back", async ({ page, request }) => {
    test.setTimeout(120_000);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "files-tab",
    );

    try {
      const { width } = vp(page);
      const f = fv(page);
      const rightCenter = (492 + width) / 2;

      await f.click({
        position: { x: rightCenter + 200, y: 16 },
        force: true,
      });
      await page.waitForTimeout(1000);

      const listResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
        { headers },
      );
      expect(listResp.ok()).toBeTruthy();

      await f.click({
        position: { x: rightCenter - 200, y: 16 },
        force: true,
      });
      await page.waitForTimeout(1000);

      const termX = rightCenter;
      const termY = 200;
      await terminalType(
        page,
        "echo tab-switch-ok > /workspace/.tab-test",
        termX,
        termY,
      );
      await waitForFile(request, workspaceId, ".tab-test", headers);

      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.tab-test`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content).toContain("tab-switch-ok");
    } finally {
      await cleanup();
    }
  });

  test("terminal accepts keyboard input", async ({ page, request }) => {
    test.setTimeout(120_000);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-input",
    );

    try {
      const { width, height } = vp(page);
      const f = fv(page);
      const termX = (492 + width) / 2;
      const termY = height / 2;

      await terminalType(
        page,
        "echo playwright-terminal-test > /workspace/.term-test",
        termX,
        termY,
      );
      await waitForFile(request, workspaceId, ".term-test", headers);

      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.term-test`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content).toContain("playwright-terminal-test");
    } finally {
      await cleanup();
    }
  });

  test("navigate back to workspaces", async ({ page, request }) => {
    test.setTimeout(120_000);
    const { cleanup } = await createAndOpenWorkspace(page, request, "nav-back");

    try {
      await flutterClick(page, 25, 28);
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 30_000 });
    } finally {
      await cleanup();
    }
  });

  test("create and delete workspace", async ({ request }) => {
    const { token, headers } = await registerUser(
      request,
      `crud-ws-${Date.now()}`,
    );
    const wsName = "e2e-test-workspace";

    // Clean up any leftover workspace with the same name
    const existingResp = await request.get(`${API_BASE}/workspaces`, {
      headers,
    });
    if (existingResp.ok()) {
      for (const ws of await existingResp.json()) {
        if (ws.name === wsName) {
          await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
        }
      }
    }

    // Create workspace via API
    const createResp = await request.post(
      `${API_BASE}/workspaces?name=${encodeURIComponent(wsName)}`,
      { headers },
    );
    expect(createResp.ok()).toBeTruthy();
    const created = await createResp.json();
    expect(created.id).toBeTruthy();
    expect(created.name).toBe(wsName);

    // Verify it appears in the listing
    let listResp = await request.get(`${API_BASE}/workspaces`, { headers });
    expect(listResp.ok()).toBeTruthy();
    let workspaces = await listResp.json();
    expect(workspaces.some((ws: any) => ws.id === created.id)).toBeTruthy();

    // Delete it
    const deleteResp = await request.delete(
      `${API_BASE}/workspaces/${created.id}`,
      { headers },
    );
    expect(deleteResp.ok()).toBeTruthy();

    // Verify it's gone
    listResp = await request.get(`${API_BASE}/workspaces`, { headers });
    workspaces = await listResp.json();
    expect(workspaces.some((ws: any) => ws.id === created.id)).toBeFalsy();
  });

  test("terminal command creates file visible via API", async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-file",
    );

    try {
      const { width, height } = vp(page);
      const f = fv(page);
      const termX = (492 + width) / 2;
      const termY = height / 2;

      await terminalType(page, 'echo "foo" > /workspace/foo.txt', termX, termY);
      await waitForFile(request, workspaceId, "foo.txt", headers);

      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=foo.txt`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content.trim()).toBe("foo");
    } finally {
      await cleanup();
    }
  });

  test("file upload, rename, and delete", async ({ request }) => {
    const { token, headers } = await registerUser(
      request,
      `file-ops-${Date.now()}`,
    );
    const wsResp = await request.post(
      `${API_BASE}/workspaces?name=e2e-file-ops-${Date.now()}`,
      { headers },
    );
    const workspaceId = (await wsResp.json()).id;
    const fileName = "playwright-test.txt";
    const renamedName = "playwright-renamed.txt";
    const fileContent = "hello from playwright e2e tests";

    // Upload
    const uploadResp = await request.post(
      `${API_BASE}/workspaces/${workspaceId}/files/upload?path=`,
      {
        headers,
        multipart: {
          file: {
            name: fileName,
            mimeType: "text/plain",
            buffer: Buffer.from(fileContent),
          },
        },
      },
    );
    expect(uploadResp.ok()).toBeTruthy();

    // Verify upload in listing
    let listResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
      { headers },
    );
    let files = await listResp.json();
    let names = files.map((f: any) => f.name);
    expect(names).toContain(fileName);

    // Verify content
    const readResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files/content?path=${fileName}`,
      { headers },
    );
    expect(readResp.ok()).toBeTruthy();
    const data = await readResp.json();
    expect(data.content).toBe(fileContent);

    // Rename
    const renameResp = await request.post(
      `${API_BASE}/workspaces/${workspaceId}/files/rename?old_path=${fileName}&new_path=${renamedName}`,
      { headers },
    );
    expect(renameResp.ok()).toBeTruthy();

    listResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
      { headers },
    );
    files = await listResp.json();
    names = files.map((f: any) => f.name);
    expect(names).not.toContain(fileName);
    expect(names).toContain(renamedName);

    // Delete
    const deleteResp = await request.delete(
      `${API_BASE}/workspaces/${workspaceId}/files?path=${renamedName}`,
      { headers },
    );
    expect(deleteResp.ok()).toBeTruthy();

    listResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
      { headers },
    );
    files = await listResp.json();
    names = files.map((f: any) => f.name);
    expect(names).not.toContain(renamedName);

    // Clean up workspace
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, { headers });
  });

  test("folder upload and zip download round-trip", async ({ request }) => {
    const { token, headers } = await registerUser(
      request,
      `folder-${Date.now()}`,
    );
    const wsResp = await request.post(
      `${API_BASE}/workspaces?name=e2e-folder-${Date.now()}`,
      { headers },
    );
    const workspaceId = (await wsResp.json()).id;
    const folder = "test-folder";

    const testFiles: Record<string, string> = {
      [`${folder}/readme.txt`]: "This is a readme file.",
      [`${folder}/data.csv`]: "name,age\nAlice,30\nBob,25",
      [`${folder}/sub/nested.txt`]: "Nested file content here.",
    };

    // Upload each file into the folder structure
    for (const [filePath, content] of Object.entries(testFiles)) {
      const resp = await request.post(
        `${API_BASE}/workspaces/${workspaceId}/files/upload?path=${encodeURIComponent(filePath)}`,
        {
          headers,
          multipart: {
            file: {
              name: filePath.split("/").pop()!,
              mimeType: "text/plain",
              buffer: Buffer.from(content),
            },
          },
        },
      );
      expect(resp.ok()).toBeTruthy();
    }

    // Verify folder appears in listing
    const listResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
      { headers },
    );
    expect(listResp.ok()).toBeTruthy();
    const entries = await listResp.json();
    const names = entries.map((e: any) => e.name);
    expect(names).toContain(folder);

    // Download folder as zip
    const dlResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(folder)}`,
      { headers },
    );
    expect(dlResp.ok()).toBeTruthy();
    const zipBuf = Buffer.from(await dlResp.body());

    // Parse zip and verify contents match
    const zip = new AdmZip(zipBuf);
    const zipEntries = zip.getEntries();
    const zipFiles: Record<string, string> = {};
    for (const entry of zipEntries) {
      if (!entry.isDirectory) {
        zipFiles[entry.entryName] = entry.getData().toString("utf8");
      }
    }

    // Zip paths are relative to the downloaded folder
    expect(zipFiles["readme.txt"]).toBe(testFiles[`${folder}/readme.txt`]);
    expect(zipFiles["data.csv"]).toBe(testFiles[`${folder}/data.csv`]);
    expect(zipFiles["sub/nested.txt"]).toBe(
      testFiles[`${folder}/sub/nested.txt`],
    );
    expect(Object.keys(zipFiles)).toHaveLength(3);

    // Clean up workspace
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, { headers });
  });

  test("agent creates and serves a hosted app", async ({ page, request }) => {
    test.setTimeout(300_000);

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-hosted-test",
    );

    try {
      // Simple prompt — the test exercises file creation + hosted URL
      // generation, not LLM coding ability.
      await sendPrompt(
        page,
        request,
        workspaceId,
        headers,
        'create a node http server that responds with "hello world" and serve it',
      );

      // Poll for files and a hosted URL in a single loop
      let hasFiles = false;
      let hostedUrl: string | null = null;
      for (let i = 0; i < 60; i++) {
        await page.waitForTimeout(2000);

        // Check for files
        if (!hasFiles) {
          const listResp = await request.get(
            `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
            { headers },
          );
          if (listResp.ok()) {
            const entries = await listResp.json();
            if (entries.length > 0) hasFiles = true;
          }
        }

        // Check for hosted URL in messages
        const msgResp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/messages`,
          { headers },
        );
        if (msgResp.ok()) {
          const messages = await msgResp.json();
          const match = messages.find(
            (m: any) =>
              m.entry_type === "assistant" &&
              /https?:\/\/localhost:\d+\/(bark\/)?hosted\//.test(
                m.content ?? "",
              ),
          );
          if (match) {
            const urlMatch = (match.content as string).match(
              /https?:\/\/localhost:\d+\/(bark\/)?hosted\/[^\s)]+/,
            );
            hostedUrl = urlMatch ? urlMatch[0] : null;
          }
        }

        if (hasFiles && hostedUrl) break;
      }
      expect(hasFiles).toBeTruthy();
      expect(hostedUrl).toBeTruthy();

      // Verify container is still running
      const containers = dockerContainersForWorkspace(workspaceId);
      expect(containers.length).toBeGreaterThan(0);
    } finally {
      await cleanup();
    }
  });

  test("logout returns to login page", async ({ page, request }) => {
    const username = `logout-${Date.now()}`;
    await registerUser(request, username);
    await loginViaUI(page, username, TEST_PASSWORD);

    const { width } = vp(page);

    // Logout button is in the top-right corner of the workspaces page
    await flutterClick(page, width - 25, 28);
    await page.waitForTimeout(2000);

    await expect(page).toHaveTitle(/Login/i, { timeout: 30_000 });
  });

  test("register new user, logout, and login with new credentials", async ({
    page,
    request,
  }) => {
    const username = `e2e-user-${Date.now()}`;
    const password = "testpass1234";

    // Register via API
    const regResp = await request.post(`${API_BASE}/auth/register`, {
      data: { username, password },
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

    await f.click({ position: { x: cx, y: height * 0.47 }, force: true });
    await page.waitForTimeout(300);
    await page.keyboard.type(username);

    await f.click({ position: { x: cx, y: height * 0.55 }, force: true });
    await page.waitForTimeout(300);
    await page.keyboard.type(password);

    await f.click({ position: { x: cx, y: height * 0.66 }, force: true });

    await expect(page).toHaveTitle(/Workspaces/i, { timeout: 15_000 });
  });

  test("invalid token returns 401 from API", async ({ request }) => {
    const headers = { Authorization: "Bearer invalid-token-value" };

    const wsResp = await request.get(`${API_BASE}/workspaces`, { headers });
    expect(wsResp.status()).toBe(401);

    const filesResp = await request.get(
      `${API_BASE}/workspaces/fake-id/files?path=.`,
      { headers },
    );
    expect(filesResp.status()).toBe(401);

    const msgResp = await request.get(
      `${API_BASE}/workspaces/fake-id/messages`,
      { headers },
    );
    expect(msgResp.status()).toBe(401);
  });

  test("no token returns 401 from API", async ({ request }) => {
    const wsResp = await request.get(`${API_BASE}/workspaces`);
    expect(wsResp.status()).toBe(401);
  });

  test("simple prompt returns assistant message", async ({ page, request }) => {
    test.setTimeout(120_000);

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-simple-prompt",
    );

    try {
      await sendPrompt(
        page,
        request,
        workspaceId,
        headers,
        "what is 2+2? reply with just the number",
      );

      let found = false;
      for (let i = 0; i < 30; i++) {
        await page.waitForTimeout(3000);
        const msgResp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/messages`,
          { headers },
        );
        if (msgResp.ok()) {
          const messages = await msgResp.json();
          if (
            messages.some(
              (m: any) =>
                m.entry_type === "assistant" && m.content.includes("4"),
            )
          ) {
            found = true;
            break;
          }
        }
      }
      expect(found).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("terminal command sequence creates directory", async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-seq",
    );

    try {
      const { width, height } = vp(page);
      const f = fv(page);
      const termX = (492 + width) / 2;
      const termY = height / 2;

      // Click in terminal
      // Run a multi-command sequence
      await terminalType(
        page,
        "mkdir -p /workspace/.e2e-multitest/sub && echo done > /workspace/.e2e-multitest/sub/result.txt",
        termX,
        termY,
      );
      await waitForFile(
        request,
        workspaceId,
        ".e2e-multitest/sub/result.txt",
        headers,
      );

      // Verify file content
      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.e2e-multitest/sub/result.txt`,
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
    test.setTimeout(120_000);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tab-switch",
    );

    try {
      const { width, height } = vp(page);
      const f = fv(page);
      const rightCenter = (492 + width) / 2;

      // Switch to Files tab
      await f.click({ position: { x: rightCenter + 200, y: 16 }, force: true });
      await page.waitForTimeout(1000);

      // Switch back to Terminal tab
      await f.click({ position: { x: rightCenter - 200, y: 16 }, force: true });
      await page.waitForTimeout(1000);

      // Switch to Files again and back
      await f.click({ position: { x: rightCenter + 200, y: 16 }, force: true });
      await page.waitForTimeout(500);
      await f.click({ position: { x: rightCenter - 200, y: 16 }, force: true });
      await page.waitForTimeout(1000);

      // Terminal should still work — run a command
      const termX = rightCenter;
      const termY = 200;
      await terminalType(
        page,
        "echo tab-survive-test > /workspace/.tab-survive",
        termX,
        termY,
      );
      await waitForFile(request, workspaceId, ".tab-survive", headers);

      const readResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.tab-survive`,
        { headers },
      );
      expect(readResp.ok()).toBeTruthy();
      const data = await readResp.json();
      expect(data.content).toContain("tab-survive-test");
    } finally {
      await cleanup();
    }
  });

  test("container stops after idle timeout", async ({ page, request }) => {
    test.setTimeout(300_000);

    // Check if test mode is enabled
    const getResp = await request.get(`${API_BASE}/api/test/idle-timeout`);
    if (!getResp.ok()) {
      test.skip(true, "BARK_TEST_MODE not enabled");
      return;
    }

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-idle-test",
    );

    // Set a short idle timeout for this workspace only
    await request.post(
      `${API_BASE}/api/test/set-idle-timeout?seconds=5&workspace_id=${workspaceId}`,
      { headers },
    );

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

      // Reset per-workspace timeout to something long before sending the
      // prompt, so the restarted container doesn't get killed while waiting
      // for the LLM response (which can be slow under concurrent load).
      await request.post(
        `${API_BASE}/api/test/set-idle-timeout?seconds=300&workspace_id=${workspaceId}`,
        { headers },
      );

      // Navigate away and back to get a fresh WebSocket connection —
      // the old one died with the container. Navigate to root first to
      // force a full page reload (goto to the same URL may be a no-op).
      await page.goto("/");
      await waitForFlutter(page);
      await page.goto(`/#/workspace/${workspaceId}`);
      await waitForFlutter(page);
      await page.waitForTimeout(4000);

      // Send a prompt — the backend will auto-restart the container
      // when it receives a prompt on a dead Pi client.
      await sendPrompt(page, request, workspaceId, headers, "say hello");

      // Poll for a response — the container should restart and respond
      let found = false;
      for (let i = 0; i < 30; i++) {
        await page.waitForTimeout(3000);
        const msgResp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/messages`,
          { headers },
        );
        if (msgResp.ok()) {
          const messages = await msgResp.json();
          if (
            messages.some((m: any) => m.entry_type === "assistant" && m.content)
          ) {
            found = true;
            break;
          }
        }
      }
      expect(found).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("container starts on workspace open and stops on navigate away", async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);

    const username = `lifecycle-${Date.now()}`;
    const { token, headers } = await registerUser(request, username);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      headers,
      "e2e-container-lifecycle",
    );

    try {
      // Before opening: no running container for this workspace
      expect(dockerContainersForWorkspace(workspaceId)).toHaveLength(0);

      // Open the workspace — this starts a container.
      // Use full URL (not just #fragment) so the page reloads and creates
      // a new WebSocket — a hash-only change may not trigger reconnection.
      await loginViaUI(page, username, TEST_PASSWORD);
      await page.goto(`/#/workspace/${workspaceId}`);

      // Wait for container to start (poll up to 60s)
      let started = false;
      for (let i = 0; i < 60; i++) {
        if (dockerContainersForWorkspace(workspaceId).length > 0) {
          started = true;
          break;
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
      expect(started).toBeTruthy();

      // Let the workspace UI fully render after container start
      await page.waitForTimeout(3000);

      // Navigate away (click back button)
      await flutterClick(page, 25, 28);
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 30_000 });

      // Wait for container to stop (poll up to 30s)
      let stopped = false;
      for (let i = 0; i < 30; i++) {
        if (dockerContainersForWorkspace(workspaceId).length === 0) {
          stopped = true;
          break;
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
      expect(stopped).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("two workspaces are independent", async ({ request }) => {
    const { token, headers } = await registerUser(
      request,
      `two-ws-${Date.now()}`,
    );

    // Clean up any leftovers
    const existing = await request.get(`${API_BASE}/workspaces`, { headers });
    for (const ws of await existing.json()) {
      if (ws.name === "e2e-ws-a" || ws.name === "e2e-ws-b") {
        await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
      }
    }

    // Create two workspaces
    const respA = await request.post(`${API_BASE}/workspaces?name=e2e-ws-a`, {
      headers,
    });
    expect(respA.ok()).toBeTruthy();
    const wsA = await respA.json();

    const respB = await request.post(`${API_BASE}/workspaces?name=e2e-ws-b`, {
      headers,
    });
    expect(respB.ok()).toBeTruthy();
    const wsB = await respB.json();

    // Upload a file to workspace A only
    const uploadResp = await request.post(
      `${API_BASE}/workspaces/${wsA.id}/files/upload?path=only-in-a.txt`,
      {
        headers,
        multipart: {
          file: {
            name: "only-in-a.txt",
            mimeType: "text/plain",
            buffer: Buffer.from("workspace A content"),
          },
        },
      },
    );
    expect(uploadResp.ok()).toBeTruthy();

    // Verify file exists in A
    const filesA = await request.get(
      `${API_BASE}/workspaces/${wsA.id}/files?path=.`,
      { headers },
    );
    const namesA = (await filesA.json()).map((e: any) => e.name);
    expect(namesA).toContain("only-in-a.txt");

    // Verify file does NOT exist in B
    const filesB = await request.get(
      `${API_BASE}/workspaces/${wsB.id}/files?path=.`,
      { headers },
    );
    const namesB = (await filesB.json()).map((e: any) => e.name);
    expect(namesB).not.toContain("only-in-a.txt");

    // Clean up
    await request.delete(`${API_BASE}/workspaces/${wsA.id}`, { headers });
    await request.delete(`${API_BASE}/workspaces/${wsB.id}`, { headers });
  });

  test("navigate into subdirectory via file viewer", async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "subdir-nav",
    );

    try {
      // Create a nested directory structure via terminal
      const { width, height } = vp(page);
      const f = fv(page);
      const termX = (492 + width) / 2;
      await terminalType(
        page,
        "mkdir -p /workspace/.e2e-nav/inner && echo nav-test > /workspace/.e2e-nav/inner/file.txt",
        termX,
        200,
      );
      await waitForFile(
        request,
        workspaceId,
        ".e2e-nav/inner/file.txt",
        headers,
      );

      // Verify structure via API
      const innerFiles = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files?path=.e2e-nav/inner`,
        { headers },
      );
      expect(innerFiles.ok()).toBeTruthy();
      const names = (await innerFiles.json()).map((e: any) => e.name);
      expect(names).toContain("file.txt");

      // Read nested file content
      const content = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.e2e-nav/inner/file.txt`,
        { headers },
      );
      expect(content.ok()).toBeTruthy();
      expect((await content.json()).content.trim()).toBe("nav-test");
    } finally {
      await cleanup();
    }
  });

  test("abort stops a running agent", async ({ page, request }) => {
    test.setTimeout(120_000);

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-abort-test",
    );

    try {
      await sendPrompt(
        page,
        request,
        workspaceId,
        headers,
        "write a very detailed 2000 word essay about the history of computing",
      );

      // Wait for the agent to start running
      await page.waitForTimeout(5000);

      // Click the abort button (red stop_circle icon, to the right of
      // the chat input). It's at the send button position.
      const { height } = vp(page);
      await flutterClick(page, 460, height - 30);
      await page.waitForTimeout(3000);

      // Verify the agent stopped — check that messages contain the user prompt
      const msgResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/messages`,
        { headers },
      );
      expect(msgResp.ok()).toBeTruthy();
      const messages = await msgResp.json();
      expect(
        messages.some(
          (m: any) =>
            m.entry_type === "user" && m.content.includes("computing"),
        ),
      ).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("queued prompt is delivered after current run finishes", async ({
    page,
    request,
  }) => {
    test.setTimeout(180_000);

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-queue-test",
    );

    try {
      const { height } = vp(page);

      // Send first prompt (verified delivery)
      await sendPrompt(
        page,
        request,
        workspaceId,
        headers,
        "what is 10+10? reply with just the number",
      );

      // Immediately send second prompt (should be queued while first runs)
      await flutterClick(page, 240, height - 30);
      await page.waitForTimeout(300);
      await page.keyboard.type("what is 20+20? reply with just the number");
      await page.keyboard.press("Enter");

      // Poll for both responses
      let foundFirst = false;
      let foundSecond = false;
      for (let i = 0; i < 40; i++) {
        await page.waitForTimeout(3000);
        const msgResp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/messages`,
          { headers },
        );
        if (msgResp.ok()) {
          const messages = await msgResp.json();
          const assistantMsgs = messages.filter(
            (m: any) => m.entry_type === "assistant",
          );
          for (const m of assistantMsgs) {
            if (m.content.includes("20")) foundFirst = true;
            if (m.content.includes("40")) foundSecond = true;
          }
          if (foundFirst && foundSecond) break;
        }
      }
      expect(foundFirst).toBeTruthy();
      expect(foundSecond).toBeTruthy();
    } finally {
      await cleanup();
    }
  });
});
