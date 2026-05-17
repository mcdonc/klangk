import { test, expect, Page, APIRequestContext } from "@playwright/test";
import AdmZip from "adm-zip";

const USER = process.env.BARK_TEST_USER || "admin";
const PASS = process.env.BARK_TEST_PASS || "admin";
const API_BASE = process.env.BARK_API_URL || "http://localhost:8997";

async function getAuthToken(request: APIRequestContext): Promise<string> {
  const resp = await request.post(`${API_BASE}/auth/login`, {
    data: { username: USER, password: PASS },
  });
  expect(resp.ok()).toBeTruthy();
  const data = await resp.json();
  return data.access_token;
}

async function getFirstWorkspaceId(
  request: APIRequestContext,
  token: string,
): Promise<string> {
  const resp = await request.get(`${API_BASE}/workspaces`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const workspaces = await resp.json();
  expect(workspaces.length).toBeGreaterThan(0);
  return workspaces[0].id;
}

// Flutter Web renders to <canvas> inside <flutter-view>, so standard DOM
// locators (text=, role=, input) don't work. We interact via coordinate
// clicks on <flutter-view> and verify state via page title and screenshots.

async function waitForFlutter(page: Page) {
  await page.waitForFunction(
    () => !document.body.textContent?.includes("Loading, please wait"),
    { timeout: 30_000 },
  );
  await page.waitForTimeout(1000);
}

function fv(page: Page) {
  return page.locator("flutter-view");
}

function vp(page: Page) {
  return page.viewportSize() || { width: 1280, height: 720 };
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

async function openFirstWorkspace(page: Page) {
  const { width } = vp(page);
  // Click on first workspace in the list
  await fv(page).click({ position: { x: width / 2, y: 110 }, force: true });
  await expect(page).not.toHaveTitle(/Workspaces/i, { timeout: 30_000 });
  await page.waitForTimeout(5000);
}

// Layout coordinates at 1280x720:
// Chat panel: x 0-486 (38%)
// Right panel: x 492-1280
// Tab bar (Terminal/Files): y ~0-32 in right panel
// Chat input: bottom of left panel, ~y 690
// Debug bar: bottom of right panel
// Back button: x ~25, y ~28

test.describe("Bark E2E", () => {
  test.describe.configure({ mode: "serial" });

  // Ensure a default workspace exists (CI starts with an empty database)
  test.beforeAll(async ({ request }) => {
    const token = await getAuthToken(request);
    const headers = { Authorization: `Bearer ${token}` };
    const listResp = await request.get(`${API_BASE}/workspaces`, { headers });
    const workspaces = await listResp.json();
    if (workspaces.length === 0) {
      await request.post(`${API_BASE}/workspaces?name=default`, { headers });
    }
  });

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

  test("navigate to workspace and see IDE layout", async ({ page }) => {
    test.setTimeout(90_000);
    await login(page);
    await openFirstWorkspace(page);

    // Verify we're in a workspace (title is "Bark - <name>")
    const title = await page.title();
    expect(title).toMatch(/^Bark - /);
    expect(title).not.toMatch(/Workspaces/i);
  });

  test("workspace shows terminal tab", async ({ page }) => {
    test.setTimeout(90_000);
    await login(page);
    await openFirstWorkspace(page);

    // Terminal is the default tab — canvas should be present (xterm.dart)
    const canvas = page.locator("canvas");
    await expect(canvas.first()).toBeVisible();
  });

  test("switch to Files tab and back", async ({ page, request }) => {
    test.setTimeout(90_000);
    await login(page);
    await openFirstWorkspace(page);

    const token = await getAuthToken(request);
    const workspaceId = await getFirstWorkspaceId(request, token);
    const headers = { Authorization: `Bearer ${token}` };

    const { width } = vp(page);
    const f = fv(page);
    const rightCenter = (492 + width) / 2;

    // Click Files tab (right side of tab bar)
    await f.click({ position: { x: rightCenter + 200, y: 16 }, force: true });
    await page.waitForTimeout(1000);

    // Verify files API works (the tab switch triggers a refresh)
    const listResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
      { headers },
    );
    expect(listResp.ok()).toBeTruthy();

    // Click Terminal tab (left side of tab bar)
    await f.click({ position: { x: rightCenter - 200, y: 16 }, force: true });
    await page.waitForTimeout(1000);

    // Verify terminal still works after tab switch by running a command
    const termX = rightCenter;
    const termY = 200;
    await f.click({ position: { x: termX, y: termY }, force: true });
    await page.waitForTimeout(500);
    await page.keyboard.type("echo tab-switch-ok > /workspace/.tab-test");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2000);

    const readResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files/content?path=.tab-test`,
      { headers },
    );
    expect(readResp.ok()).toBeTruthy();
    const data = await readResp.json();
    expect(data.content).toContain("tab-switch-ok");

    await request.delete(
      `${API_BASE}/workspaces/${workspaceId}/files?path=.tab-test`,
      { headers },
    );
  });

  test("terminal accepts keyboard input", async ({ page, request }) => {
    test.setTimeout(90_000);
    await login(page);
    await openFirstWorkspace(page);

    const token = await getAuthToken(request);
    const workspaceId = await getFirstWorkspaceId(request, token);
    const headers = { Authorization: `Bearer ${token}` };

    const { width, height } = vp(page);
    const f = fv(page);
    const termX = (492 + width) / 2;
    const termY = height / 2;

    // Click in terminal area and type a command that creates a marker file
    await f.click({ position: { x: termX, y: termY }, force: true });
    await page.waitForTimeout(500);
    await page.keyboard.type(
      "echo playwright-terminal-test > /workspace/.term-test",
    );
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2000);

    // Verify the file was created via API
    const readResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files/content?path=.term-test`,
      { headers },
    );
    expect(readResp.ok()).toBeTruthy();
    const data = await readResp.json();
    expect(data.content).toContain("playwright-terminal-test");

    // Clean up
    await request.delete(
      `${API_BASE}/workspaces/${workspaceId}/files?path=.term-test`,
      { headers },
    );
  });

  test("navigate back to workspaces", async ({ page }) => {
    test.setTimeout(90_000);
    await login(page);
    await openFirstWorkspace(page);

    // Click back arrow (top-left corner)
    await fv(page).click({ position: { x: 25, y: 28 }, force: true });
    await expect(page).toHaveTitle(/Workspaces/i, { timeout: 15_000 });
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
    test.setTimeout(90_000);
    await login(page);
    await openFirstWorkspace(page);

    const { width, height } = vp(page);
    const f = fv(page);
    const termX = (492 + width) / 2;
    const termY = height / 2;

    // Click in terminal area
    await f.click({ position: { x: termX, y: termY }, force: true });
    await page.waitForTimeout(500);

    // Type command to create a file
    await page.keyboard.type('echo "foo" > /workspace/foo.txt');
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2000);

    // Verify file exists via API
    const token = await getAuthToken(request);
    const workspaceId = await getFirstWorkspaceId(request, token);
    const headers = { Authorization: `Bearer ${token}` };

    const listResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=.`,
      { headers },
    );
    expect(listResp.ok()).toBeTruthy();
    const files = await listResp.json();
    const names = files.map((f: any) => f.name);
    expect(names).toContain("foo.txt");

    // Verify content
    const readResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files/content?path=foo.txt`,
      { headers },
    );
    expect(readResp.ok()).toBeTruthy();
    const data = await readResp.json();
    expect(data.content.trim()).toBe("foo");

    // Clean up
    await request.delete(
      `${API_BASE}/workspaces/${workspaceId}/files?path=foo.txt`,
      { headers },
    );
  });

  test("file upload, rename, and delete", async ({ request }) => {
    const token = await getAuthToken(request);
    const workspaceId = await getFirstWorkspaceId(request, token);
    const headers = { Authorization: `Bearer ${token}` };
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
  });

  test("folder upload and zip download round-trip", async ({ request }) => {
    const token = await getAuthToken(request);
    const workspaceId = await getFirstWorkspaceId(request, token);
    const headers = { Authorization: `Bearer ${token}` };
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

    // Clean up — delete the folder
    const delResp = await request.delete(
      `${API_BASE}/workspaces/${workspaceId}/files?path=${encodeURIComponent(folder)}`,
      { headers },
    );
    expect(delResp.ok()).toBeTruthy();
  });

  test("agent creates pong game with hosted URL", async ({ page, request }) => {
    test.setTimeout(300_000); // LLM interaction can be slow

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
      let hostedUrl = false;
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
            hostedUrl = true;
            break;
          }
        }
        await page.waitForTimeout(5000);
      }
      expect(hostedUrl).toBeTruthy();
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
});
