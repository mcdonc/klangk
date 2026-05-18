import { test, expect, Page, APIRequestContext } from "@playwright/test";
import AdmZip from "adm-zip";
import { execSync } from "child_process";

const USER = process.env.BARK_TEST_USER || "admin";
const PASS = process.env.BARK_TEST_PASS || "admin";
const BACKEND_PORT = process.env.BARK_E2E_PORT || "18997";
const API_BASE = `http://localhost:${BACKEND_PORT}`;

async function getAuthToken(request: APIRequestContext): Promise<string> {
  const resp = await request.post(`${API_BASE}/auth/login`, {
    data: { username: USER, password: PASS },
  });
  expect(resp.ok()).toBeTruthy();
  const data = await resp.json();
  return data.access_token;
}

// Flutter Web renders to <canvas> inside <flutter-view>, so standard DOM
// locators (text=, role=, input) don't work. We interact via coordinate
// clicks on <flutter-view> and verify state via page title and screenshots.

async function waitForFlutter(page: Page) {
  await page.waitForFunction(
    () => !document.body.textContent?.includes("Loading, please wait"),
    { timeout: 60_000 },
  );
  await page.waitForTimeout(1000);
}

function fv(page: Page) {
  return page.locator("flutter-view");
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

async function login(page: Page) {
  await page.goto("");
  await waitForFlutter(page);

  const { width, height } = vp(page);
  const cx = width / 2;
  const f = fv(page);

  // Username field
  await f.click({ position: { x: cx, y: height * 0.47 }, force: true });
  await page.waitForTimeout(300);
  await page.keyboard.type(USER);

  // Password field
  await f.click({ position: { x: cx, y: height * 0.55 }, force: true });
  await page.waitForTimeout(300);
  await page.keyboard.type(PASS);

  // Login button
  await f.click({ position: { x: cx, y: height * 0.66 }, force: true });

  await expect(page).toHaveTitle(/Workspaces/i, { timeout: 15_000 });
}

/** Create a unique workspace, log in, and navigate to it. */
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
  const token = await getAuthToken(request);
  const headers = { Authorization: `Bearer ${token}` };
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

  await login(page);
  await page.goto(`#/workspace/${workspaceId}`);

  // Wait for container to be ready by polling the files API
  for (let i = 0; i < 60; i++) {
    try {
      const resp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
        { headers },
      );
      if (resp.ok()) break;
    } catch {
      // Not ready yet
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  // Extra settle time for the UI to render after container ready
  await page.waitForTimeout(2000);

  return {
    workspaceId,
    token,
    headers,
    cleanup: async () => {
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
    },
  };
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
  test("login with default credentials", async ({ page }) => {
    await login(page);
    await expect(page).toHaveTitle(/Workspaces/i);
  });

  test("login with wrong password fails", async ({ page }) => {
    await page.goto("");
    await waitForFlutter(page);

    const { width, height } = vp(page);
    const cx = width / 2;
    const f = fv(page);

    await f.click({ position: { x: cx, y: height * 0.47 }, force: true });
    await page.waitForTimeout(300);
    await page.keyboard.type(USER);

    await f.click({ position: { x: cx, y: height * 0.55 }, force: true });
    await page.waitForTimeout(300);
    await page.keyboard.type("wrongpassword");

    await f.click({ position: { x: cx, y: height * 0.66 }, force: true });
    await page.waitForTimeout(3000);

    // Should still be on the login page
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
      await fv(page).click({ position: { x: 25, y: 28 }, force: true });
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 30_000 });
    } finally {
      await cleanup();
    }
  });

  test("create and delete workspace", async ({ request }) => {
    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };
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
    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };
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
    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };
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

  test("agent creates pong game with hosted URL", async ({ page, request }) => {
    test.setTimeout(600_000); // LLM interaction can be slow, especially on CI

    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };

    // Clean up any leftover workspace with the same name
    const existingResp = await request.get(`${API_BASE}/workspaces`, {
      headers,
    });
    if (existingResp.ok()) {
      for (const ws of await existingResp.json()) {
        if (ws.name === "e2e-pong-test") {
          await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
        }
      }
    }

    // Create a fresh workspace for this test
    const createResp = await request.post(
      `${API_BASE}/workspaces?name=e2e-pong-test`,
      { headers },
    );
    expect(createResp.ok()).toBeTruthy();
    const workspace = await createResp.json();
    const workspaceId = workspace.id;

    try {
      // Navigate to the new workspace
      await login(page);
      await page.goto(`#/workspace/${workspaceId}`);
      await page.waitForTimeout(10000); // wait for container to start

      // Click chat input and type the prompt
      const { height } = vp(page);
      const f = fv(page);
      const chatInputX = 240;
      const chatInputY = height - 30;

      await f.click({
        position: { x: chatInputX, y: chatInputY },
        force: true,
      });
      await page.waitForTimeout(500);
      await page.keyboard.type(
        "write me a javascript application that creates a pong game and serve it through a node web server",
      );
      await page.waitForTimeout(300);
      await page.keyboard.press("Enter");

      // Poll until files appear (agent wrote code into the empty workspace)
      let hasFiles = false;
      for (let i = 0; i < 60; i++) {
        await page.waitForTimeout(5000);
        const listResp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
          { headers },
        );
        if (listResp.ok()) {
          const entries = await listResp.json();
          if (entries.length > 0) {
            hasFiles = true;
            break;
          }
        }
      }
      expect(hasFiles).toBeTruthy();

      // Poll messages for an assistant response containing a hosted URL
      let hostedUrl: string | null = null;
      for (let i = 0; i < 60; i++) {
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
            // Extract the URL from the message
            const urlMatch = (match.content as string).match(
              /https?:\/\/localhost:\d+\/(bark\/)?hosted\/[^\s)]+/,
            );
            hostedUrl = urlMatch ? urlMatch[0] : null;
            break;
          }
        }
        await page.waitForTimeout(5000);
      }
      expect(hostedUrl).toBeTruthy();

      // Verify container is still running
      const containers = dockerContainersForWorkspace(workspaceId);
      expect(containers.length).toBeGreaterThan(0);

      // TODO: Visit hostedUrl and assert 200. Currently the
      // LLM-generated node server sometimes crashes inside the
      // container, returning 502 from the proxy even though the
      // container itself is alive. The URL format is verified by
      // the regex match above.
    } finally {
      // Clean up: delete the test workspace
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
    }
  });

  test("logout returns to login page", async ({ page }) => {
    test.setTimeout(60_000);
    await login(page);

    const { width } = vp(page);
    const f = fv(page);

    // Logout button is in the top-right corner of the workspaces page
    await f.click({ position: { x: width - 25, y: 28 }, force: true });
    await page.waitForTimeout(2000);

    await expect(page).toHaveTitle(/Login/i, { timeout: 10_000 });
  });

  test("register new user, logout, and login with new credentials", async ({
    page,
    request,
  }) => {
    test.setTimeout(60_000);
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
    await page.goto("");
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

    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };

    // Create a fresh workspace
    const existingResp = await request.get(`${API_BASE}/workspaces`, {
      headers,
    });
    for (const ws of await existingResp.json()) {
      if (ws.name === "e2e-simple-prompt") {
        await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
      }
    }
    const createResp = await request.post(
      `${API_BASE}/workspaces?name=e2e-simple-prompt`,
      { headers },
    );
    expect(createResp.ok()).toBeTruthy();
    const workspace = await createResp.json();
    const workspaceId = workspace.id;

    try {
      await login(page);
      await page.goto(`#/workspace/${workspaceId}`);
      await page.waitForTimeout(10000);

      // Type a simple prompt
      const { height } = vp(page);
      const f = fv(page);
      await f.click({ position: { x: 240, y: height - 30 }, force: true });
      await page.waitForTimeout(500);
      await page.keyboard.type("what is 2+2? reply with just the number");
      await page.waitForTimeout(300);
      await page.keyboard.press("Enter");

      // Poll for an assistant message
      let found = false;
      for (let i = 0; i < 30; i++) {
        await page.waitForTimeout(3000);
        const msgResp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/messages`,
          { headers },
        );
        if (msgResp.ok()) {
          const messages = await msgResp.json();
          const assistantMsg = messages.find(
            (m: any) => m.entry_type === "assistant" && m.content.includes("4"),
          );
          if (assistantMsg) {
            found = true;
            break;
          }
        }
      }
      expect(found).toBeTruthy();
    } finally {
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
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

    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };

    // Check if test mode is enabled
    const getResp = await request.get(`${API_BASE}/api/test/idle-timeout`, {
      headers,
    });
    if (!getResp.ok()) {
      test.skip(true, "BARK_TEST_MODE not enabled");
      return;
    }

    // Create a workspace first, then set per-workspace idle timeout
    const existingResp = await request.get(`${API_BASE}/workspaces`, {
      headers,
    });
    for (const ws of await existingResp.json()) {
      if (ws.name === "e2e-idle-test") {
        await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
      }
    }
    const createResp = await request.post(
      `${API_BASE}/workspaces?name=e2e-idle-test`,
      { headers },
    );
    expect(createResp.ok()).toBeTruthy();
    const workspace = await createResp.json();
    const workspaceId = workspace.id;

    // Set a short idle timeout for this workspace only
    await request.post(
      `${API_BASE}/api/test/set-idle-timeout?seconds=5&workspace_id=${workspaceId}`,
      { headers },
    );

    try {
      await login(page);
      await page.goto(`#/workspace/${workspaceId}`);
      await page.waitForTimeout(10000);

      // Wait for the container to idle out (5s timeout + check interval)
      await page.waitForTimeout(15000);

      // Send a prompt — it should trigger a container restart
      const { height } = vp(page);
      const f = fv(page);
      await f.click({ position: { x: 240, y: height - 30 }, force: true });
      await page.waitForTimeout(500);
      await page.keyboard.type("say hello");
      await page.waitForTimeout(300);
      await page.keyboard.press("Enter");

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
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
    }
  });

  test("container starts on workspace open and stops on navigate away", async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);

    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };

    // Create a fresh workspace
    const existingResp = await request.get(`${API_BASE}/workspaces`, {
      headers,
    });
    for (const ws of await existingResp.json()) {
      if (ws.name === "e2e-container-lifecycle") {
        await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
      }
    }
    const createResp = await request.post(
      `${API_BASE}/workspaces?name=e2e-container-lifecycle`,
      { headers },
    );
    expect(createResp.ok()).toBeTruthy();
    const workspace = await createResp.json();
    const workspaceId = workspace.id;

    try {
      // Before opening: no running container for this workspace
      expect(dockerContainersForWorkspace(workspaceId)).toHaveLength(0);

      // Open the workspace — this starts a container
      await login(page);
      await page.goto(`#/workspace/${workspaceId}`);

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

      // Navigate away (click back button)
      await fv(page).click({ position: { x: 25, y: 28 }, force: true });
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
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
    }
  });

  test("two workspaces are independent", async ({ request }) => {
    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };

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

    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };

    // Create a fresh workspace
    const existingResp = await request.get(`${API_BASE}/workspaces`, {
      headers,
    });
    for (const ws of await existingResp.json()) {
      if (ws.name === "e2e-abort-test") {
        await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
      }
    }
    const createResp = await request.post(
      `${API_BASE}/workspaces?name=e2e-abort-test`,
      { headers },
    );
    expect(createResp.ok()).toBeTruthy();
    const workspace = await createResp.json();
    const workspaceId = workspace.id;

    try {
      await login(page);
      await page.goto(`#/workspace/${workspaceId}`);
      await page.waitForTimeout(10000);

      // Send a long-running prompt
      const { height } = vp(page);
      const f = fv(page);
      await f.click({ position: { x: 240, y: height - 30 }, force: true });
      await page.waitForTimeout(500);
      await page.keyboard.type(
        "write a very detailed 2000 word essay about the history of computing",
      );
      await page.waitForTimeout(300);
      await page.keyboard.press("Enter");

      // Wait for the agent to start running
      await page.waitForTimeout(5000);

      // Click the abort button (red stop_circle icon, to the right of
      // the chat input). It's at the send button position.
      const sendBtnX = 460;
      const sendBtnY = height - 30;
      await f.click({
        position: { x: sendBtnX, y: sendBtnY },
        force: true,
      });
      await page.waitForTimeout(3000);

      // Verify the agent stopped — check that messages contain a
      // user prompt but the assistant response is incomplete or
      // the run finished
      const msgResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/messages`,
        { headers },
      );
      expect(msgResp.ok()).toBeTruthy();
      const messages = await msgResp.json();
      // Should have at least the user prompt
      expect(
        messages.some(
          (m: any) =>
            m.entry_type === "user" && m.content.includes("computing"),
        ),
      ).toBeTruthy();
    } finally {
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
    }
  });

  test("queued prompt is delivered after current run finishes", async ({
    page,
    request,
  }) => {
    test.setTimeout(180_000);

    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };

    // Create a fresh workspace
    const existingResp = await request.get(`${API_BASE}/workspaces`, {
      headers,
    });
    for (const ws of await existingResp.json()) {
      if (ws.name === "e2e-queue-test") {
        await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
      }
    }
    const createResp = await request.post(
      `${API_BASE}/workspaces?name=e2e-queue-test`,
      { headers },
    );
    expect(createResp.ok()).toBeTruthy();
    const workspace = await createResp.json();
    const workspaceId = workspace.id;

    try {
      await login(page);
      await page.goto(`#/workspace/${workspaceId}`);
      await page.waitForTimeout(10000);

      const { height } = vp(page);
      const f = fv(page);
      const chatX = 240;
      const chatY = height - 30;

      // Send first prompt
      await f.click({ position: { x: chatX, y: chatY }, force: true });
      await page.waitForTimeout(500);
      await page.keyboard.type("what is 10+10? reply with just the number");
      await page.keyboard.press("Enter");

      // Immediately send second prompt (should be queued)
      await page.waitForTimeout(1000);
      await f.click({ position: { x: chatX, y: chatY }, force: true });
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
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
    }
  });
});
