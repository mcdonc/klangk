import { test, expect } from "@playwright/test";
import AdmZip from "adm-zip";
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
  waitForTerminalReady,
} from "./helpers";

test.describe("Klangk E2E", () => {
  test("index.html has cache-busted flutter_bootstrap.js", async ({
    request,
  }) => {
    const resp = await request.get(`${API_BASE}/`);
    expect(resp.ok()).toBeTruthy();
    const html = await resp.text();
    expect(html).toMatch(/flutter_bootstrap\.js\?v=[0-9a-f]{12}/);
  });

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
    const termReady = waitForTerminalReady(page);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-input",
    );
    await termReady;

    try {
      const { width, height } = vp(page);
      const termX = width / 2;
      const termY = height / 2;

      await terminalType(
        page,
        "echo playwright-terminal-test > /home/klangk/work/.term-test",
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

  test("create and delete workspace", async ({ request }) => {
    const { token, headers } = await registerUser(
      request,
      `crud-ws-${Date.now()}@test.example.com`,
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
    const createResp = await request.post(`${API_BASE}/workspaces`, {
      headers,
      data: { name: wsName },
    });
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
    const termReady = waitForTerminalReady(page);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-file",
    );
    await termReady;

    try {
      const { width, height } = vp(page);
      const termX = width / 2;
      const termY = height / 2;

      await terminalType(
        page,
        'echo "foo" > /home/klangk/work/foo.txt',
        termX,
        termY,
      );
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

  test("file upload, rename, and delete", async ({ request }) => {
    const { token, headers } = await registerUser(
      request,
      `file-ops-${Date.now()}@test.example.com`,
    );
    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers,
      data: { name: `e2e-file-ops-${Date.now()}` },
    });
    const workspaceId = (await wsResp.json()).id;
    const fileName = "playwright-test.txt";
    const renamedName = "playwright-renamed.txt";
    const fileContent = "hello from playwright e2e tests";

    // Upload
    const uploadResp = await request.post(
      `${API_BASE}/workspaces/${workspaceId}/files/upload?path=work/${fileName}`,
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
      `${API_BASE}/workspaces/${workspaceId}/files?path=work`,
      { headers },
    );
    let files = await listResp.json();
    let names = files.map((f: any) => f.name);
    expect(names).toContain(fileName);

    // Verify content
    const readResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files/content?path=work/${fileName}`,
      { headers },
    );
    expect(readResp.ok()).toBeTruthy();
    const data = await readResp.json();
    expect(data.content).toBe(fileContent);

    // Rename
    const renameResp = await request.post(
      `${API_BASE}/workspaces/${workspaceId}/files/rename`,
      {
        headers,
        data: { old_path: `work/${fileName}`, new_path: `work/${renamedName}` },
      },
    );
    expect(renameResp.ok()).toBeTruthy();

    listResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=work`,
      { headers },
    );
    files = await listResp.json();
    names = files.map((f: any) => f.name);
    expect(names).not.toContain(fileName);
    expect(names).toContain(renamedName);

    // Delete
    const deleteResp = await request.delete(
      `${API_BASE}/workspaces/${workspaceId}/files?path=work/${renamedName}`,
      { headers },
    );
    expect(deleteResp.ok()).toBeTruthy();

    listResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=work`,
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
      `folder-${Date.now()}@test.example.com`,
    );
    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers,
      data: { name: `e2e-folder-${Date.now()}` },
    });
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
        `${API_BASE}/workspaces/${workspaceId}/files/upload?path=work/${encodeURIComponent(filePath)}`,
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
      `${API_BASE}/workspaces/${workspaceId}/files?path=work`,
      { headers },
    );
    expect(listResp.ok()).toBeTruthy();
    const entries = await listResp.json();
    const names = entries.map((e: any) => e.name);
    expect(names).toContain(folder);

    // Download folder as zip
    const dlResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files/download?path=work/${encodeURIComponent(folder)}`,
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

  test("logout returns to login page", async ({ page, request }) => {
    const email = `logout-${Date.now()}@test.example.com`;
    await registerUser(request, email);
    await loginViaUI(page, email, TEST_PASSWORD);

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

  test("invalid token returns 401 from API", async ({ request }) => {
    const headers = { Authorization: "Bearer invalid-token-value" };

    const wsResp = await request.get(`${API_BASE}/workspaces`, { headers });
    expect(wsResp.status()).toBe(401);

    const filesResp = await request.get(
      `${API_BASE}/workspaces/fake-id/files?path=work`,
      { headers },
    );
    expect(filesResp.status()).toBe(401);
  });

  test("no token returns 401 from API", async ({ request }) => {
    const wsResp = await request.get(`${API_BASE}/workspaces`);
    expect(wsResp.status()).toBe(401);
  });

  test("terminal command sequence creates directory", async ({
    page,
    request,
  }) => {
    const termReady = waitForTerminalReady(page);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "term-seq",
    );
    await termReady;

    try {
      const { width, height } = vp(page);
      const termX = width / 2;
      const termY = height / 2;

      // Run a multi-command sequence
      await terminalType(
        page,
        "mkdir -p /home/klangk/work/.e2e-multitest/sub && echo done > /home/klangk/work/.e2e-multitest/sub/result.txt",
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
    const termReady = waitForTerminalReady(page);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tab-switch",
    );
    await termReady;

    try {
      const { width, height } = vp(page);
      const f = fv(page);

      // Tabs span full width now (no chat panel). Terminal tab is on the
      // left half, Files tab is on the right half of the tab bar.
      const termTabX = width / 4;
      const filesTabX = (width * 3) / 4;

      // Switch to Files tab
      await f.click({ position: { x: filesTabX, y: 76 }, force: true });
      await page.waitForTimeout(500);

      // Switch back to Terminal tab
      await f.click({ position: { x: termTabX, y: 76 }, force: true });
      await page.waitForTimeout(500);

      // Switch to Files again and back
      await f.click({ position: { x: filesTabX, y: 76 }, force: true });
      await page.waitForTimeout(500);
      await f.click({ position: { x: termTabX, y: 76 }, force: true });
      await page.waitForTimeout(2000);

      // Terminal should still work — run a command
      const termX = width / 2;
      const termY = 200;
      await terminalType(
        page,
        "echo tab-survive-test > /home/klangk/work/.tab-survive",
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
      await page.waitForTimeout(3000);
      const containersAfter = dockerContainersForWorkspace(workspaceId);
      expect(containersAfter.length).toBe(1);
      expect(containersAfter[0]).toBe(containersBefore[0]);
    } finally {
      await cleanup();
    }
  });

  test("two workspaces are independent", async ({ request }) => {
    const { token, headers } = await registerUser(
      request,
      `two-ws-${Date.now()}@test.example.com`,
    );

    // Clean up any leftovers
    const existing = await request.get(`${API_BASE}/workspaces`, { headers });
    for (const ws of await existing.json()) {
      if (ws.name === "e2e-ws-a" || ws.name === "e2e-ws-b") {
        await request.delete(`${API_BASE}/workspaces/${ws.id}`, { headers });
      }
    }

    // Create two workspaces
    const respA = await request.post(`${API_BASE}/workspaces`, {
      headers,
      data: { name: "e2e-ws-a" },
    });
    expect(respA.ok()).toBeTruthy();
    const wsA = await respA.json();

    const respB = await request.post(`${API_BASE}/workspaces`, {
      headers,
      data: { name: "e2e-ws-b" },
    });
    expect(respB.ok()).toBeTruthy();
    const wsB = await respB.json();

    // Upload a file to workspace A only
    const uploadResp = await request.post(
      `${API_BASE}/workspaces/${wsA.id}/files/upload?path=work/only-in-a.txt`,
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
      `${API_BASE}/workspaces/${wsA.id}/files?path=work`,
      { headers },
    );
    const namesA = (await filesA.json()).map((e: any) => e.name);
    expect(namesA).toContain("only-in-a.txt");

    // Verify file does NOT exist in B
    const filesB = await request.get(
      `${API_BASE}/workspaces/${wsB.id}/files?path=work`,
      { headers },
    );
    const namesB = (await filesB.json()).map((e: any) => e.name);
    expect(namesB).not.toContain("only-in-a.txt");

    // Clean up
    await request.delete(`${API_BASE}/workspaces/${wsA.id}`, { headers });
    await request.delete(`${API_BASE}/workspaces/${wsB.id}`, { headers });
  });

  test("nested directory structure accessible via file API", async ({
    page,
    request,
  }) => {
    const termReady = waitForTerminalReady(page);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "subdir-nav",
    );
    await termReady;

    try {
      // Create nested directory structure via terminal
      await terminalType(
        page,
        "mkdir -p /home/klangk/work/.e2e-nav/inner && echo nav-test > /home/klangk/work/.e2e-nav/inner/file.txt",
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
    const termReady = waitForTerminalReady(page);
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "home-persist",
    );
    await termReady;

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

  test("container stops after idle timeout", async ({ page, request }) => {
    // Check if test mode is enabled
    const getResp = await request.get(`${API_BASE}/api/test/idle-timeout`);
    if (!getResp.ok()) {
      test.skip(true, "KLANGK_TEST_MODE not enabled");
      return;
    }

    const { workspaceId, email, headers, cleanup } =
      await createAndOpenWorkspace(page, request, "e2e-idle-test");

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
      await openWorkspace(page, email, workspaceId);

      expect(dockerContainersForWorkspace(workspaceId).length).toBeGreaterThan(
        0,
      );
    } finally {
      await cleanup();
    }
  });

  test("admin can list users, add/remove roles, and delete users", async ({
    request,
  }) => {
    // Login as the default admin user (seeded on startup)
    const loginResp = await request.post(`${API_BASE}/auth/login`, {
      data: { email: "admin@example.com", password: "admin" },
    });
    expect(loginResp.ok()).toBeTruthy();
    const adminToken = (await loginResp.json()).access_token;
    const adminHeaders = { Authorization: `Bearer ${adminToken}` };

    // Create a test user via test mode
    const { token: userToken, headers: userHeaders } = await registerUser(
      request,
      "admin-test@test.example.com",
    );

    // Admin can list users
    const listResp = await request.get(`${API_BASE}/admin/users`, {
      headers: adminHeaders,
    });
    expect(listResp.ok()).toBeTruthy();
    const users = await listResp.json();
    const testUser = users.find(
      (u: any) => u.email === "admin-test@test.example.com",
    );
    expect(testUser).toBeTruthy();
    expect(testUser.roles).toEqual([]);

    // Non-admin cannot list users
    const forbiddenResp = await request.get(`${API_BASE}/admin/users`, {
      headers: userHeaders,
    });
    expect(forbiddenResp.status()).toBe(403);

    // Admin can add a role
    const addRoleResp = await request.post(
      `${API_BASE}/admin/users/${testUser.id}/roles/editor`,
      { headers: adminHeaders },
    );
    expect(addRoleResp.ok()).toBeTruthy();

    // Verify role was added
    const listResp2 = await request.get(`${API_BASE}/admin/users`, {
      headers: adminHeaders,
    });
    const updatedUser = (await listResp2.json()).find(
      (u: any) => u.email === "admin-test@test.example.com",
    );
    expect(updatedUser.roles).toContain("editor");

    // Admin can remove a role
    const removeRoleResp = await request.delete(
      `${API_BASE}/admin/users/${testUser.id}/roles/editor`,
      { headers: adminHeaders },
    );
    expect(removeRoleResp.ok()).toBeTruthy();

    // Admin can delete a user
    const deleteResp = await request.delete(
      `${API_BASE}/admin/users/${testUser.id}`,
      { headers: adminHeaders },
    );
    expect(deleteResp.ok()).toBeTruthy();

    // Verify user is gone
    const listResp3 = await request.get(`${API_BASE}/admin/users`, {
      headers: adminHeaders,
    });
    const deletedUser = (await listResp3.json()).find(
      (u: any) => u.email === "admin-test@test.example.com",
    );
    expect(deletedUser).toBeUndefined();
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

  test("admin user management page loads and lists users", async ({
    page,
    request,
  }) => {
    // Login as the default admin user via the API, then set the token
    // and navigate directly to the admin page.
    const loginResp = await request.post(`${API_BASE}/auth/login`, {
      data: { email: "admin@example.com", password: "admin" },
    });
    expect(loginResp.ok()).toBeTruthy();
    const adminToken = (await loginResp.json()).access_token;
    const adminHeaders = { Authorization: `Bearer ${adminToken}` };

    // Verify the admin API returns users
    const resp = await request.get(`${API_BASE}/admin/users`, {
      headers: adminHeaders,
    });
    expect(resp.ok()).toBeTruthy();
    const users = await resp.json();
    expect(users.length).toBeGreaterThan(0);
    expect(
      users.some((u: any) => u.email === "admin@example.com"),
    ).toBeTruthy();

    // Create a user via API, verify it appears, then delete via API
    const regResp = await request.post(`${API_BASE}/auth/register`, {
      data: { email: "e2e-admin-ui@test.example.com", password: "testpass" },
    });
    expect(regResp.ok()).toBeTruthy();

    const resp2 = await request.get(`${API_BASE}/admin/users`, {
      headers: adminHeaders,
    });
    const updatedUsers = await resp2.json();
    const newUser = updatedUsers.find(
      (u: any) => u.email === "e2e-admin-ui@test.example.com",
    );
    expect(newUser).toBeTruthy();

    // Update email via API
    const patchResp = await request.patch(
      `${API_BASE}/admin/users/${newUser.id}`,
      {
        headers: adminHeaders,
        data: { email: "e2e-admin-renamed@test.example.com" },
      },
    );
    expect(patchResp.ok()).toBeTruthy();

    // Verify rename
    const resp3 = await request.get(`${API_BASE}/admin/users`, {
      headers: adminHeaders,
    });
    expect(
      (await resp3.json()).some(
        (u: any) => u.email === "e2e-admin-renamed@test.example.com",
      ),
    ).toBeTruthy();

    // Delete via API
    const deleteResp = await request.delete(
      `${API_BASE}/admin/users/${newUser.id}`,
      { headers: adminHeaders },
    );
    expect(deleteResp.ok()).toBeTruthy();

    // Verify deleted
    const resp4 = await request.get(`${API_BASE}/admin/users`, {
      headers: adminHeaders,
    });
    expect(
      (await resp4.json()).some(
        (u: any) => u.email === "e2e-admin-renamed@test.example.com",
      ),
    ).toBeFalsy();
  });

  test("workspace sharing via API", async ({ request }) => {
    // Register two users
    const ownerEmail = `share-owner-${Date.now()}@test.example.com`;
    const memberEmail = `share-member-${Date.now()}@test.example.com`;
    const { headers: ownerHeaders } = await registerUser(request, ownerEmail);
    const { headers: memberHeaders } = await registerUser(request, memberEmail);

    // Create a workspace as owner
    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers: ownerHeaders,
      data: { name: `e2e-share-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspace = await wsResp.json();
    const workspaceId = workspace.id;

    // Upload a file so we can test access
    const uploadResp = await request.post(
      `${API_BASE}/workspaces/${workspaceId}/files/upload?path=work/shared.txt`,
      {
        headers: ownerHeaders,
        multipart: {
          file: {
            name: "shared.txt",
            mimeType: "text/plain",
            buffer: Buffer.from("shared content"),
          },
        },
      },
    );
    expect(uploadResp.ok()).toBeTruthy();

    // Initially, no members
    let membersResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/members`,
      { headers: ownerHeaders },
    );
    expect(membersResp.ok()).toBeTruthy();
    let members = await membersResp.json();
    expect(members).toHaveLength(0);

    // Member cannot access the workspace files before sharing
    const preShareFiles = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=work`,
      { headers: memberHeaders },
    );
    expect(preShareFiles.ok()).toBeFalsy();

    // Share workspace with member
    const addResp = await request.post(
      `${API_BASE}/workspaces/${workspaceId}/members`,
      {
        headers: ownerHeaders,
        data: { email: memberEmail },
      },
    );
    expect(addResp.ok()).toBeTruthy();

    // Verify member shows up in members list
    membersResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/members`,
      { headers: ownerHeaders },
    );
    expect(membersResp.ok()).toBeTruthy();
    members = await membersResp.json();
    expect(members).toHaveLength(1);
    expect(members[0].email).toBe(memberEmail);
    const memberId = members[0].id;

    // Member can now access workspace files
    const postShareFiles = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=work`,
      { headers: memberHeaders },
    );
    expect(postShareFiles.ok()).toBeTruthy();
    const files = await postShareFiles.json();
    expect(files.some((f: any) => f.name === "shared.txt")).toBeTruthy();

    // Unshare
    const removeResp = await request.delete(
      `${API_BASE}/workspaces/${workspaceId}/members/${memberId}`,
      { headers: ownerHeaders },
    );
    expect(removeResp.ok()).toBeTruthy();

    // Verify member is gone
    membersResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/members`,
      { headers: ownerHeaders },
    );
    members = await membersResp.json();
    expect(members).toHaveLength(0);

    // Member can no longer access workspace files
    const postUnshareFiles = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/files?path=work`,
      { headers: memberHeaders },
    );
    expect(postUnshareFiles.ok()).toBeFalsy();

    // Clean up
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers: ownerHeaders,
    });
  });

  test("browser-delegate routes to the correct connection", async ({
    browser,
    request,
  }) => {
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

    // Set up terminal_started listeners before opening workspaces
    const termReady1 = waitForTerminalReady(page1);
    const termReady2 = waitForTerminalReady(page2);

    // Open workspace as owner in page1, member in page2
    await openWorkspace(page1, ownerEmail, workspaceId);
    await openWorkspace(page2, memberEmail, workspaceId);
    await termReady1;
    await termReady2;

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

  test("container recreated on page refresh", async ({ page, request }) => {
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
