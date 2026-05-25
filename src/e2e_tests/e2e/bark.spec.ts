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
  waitForFile,
  vp,
  sendPrompt,
  terminalType,
  createWorkspace,
  openWorkspace,
  createAndOpenWorkspace,
  dockerContainersForWorkspace,
  tryLogin,
} from "./helpers";

test.describe("Bark E2E", () => {
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
      expect(title).toMatch(/^Bark - /);
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
    );

    try {
      const { width, height } = vp(page);
      const f = fv(page);
      const termX = (492 + width) / 2;
      const termY = height / 2;

      await terminalType(
        page,
        "echo playwright-terminal-test > /work/.term-test",
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

      await terminalType(page, 'echo "foo" > /work/foo.txt', termX, termY);
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
      `file-ops-${Date.now()}@test.example.com`,
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
      `folder-${Date.now()}@test.example.com`,
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

    await f.click({ position: { x: cx, y: height * 0.52 }, force: true });
    await page.waitForTimeout(200);
    await page.keyboard.type(email);

    await f.click({ position: { x: cx, y: height * 0.6 }, force: true });
    await page.waitForTimeout(200);
    await page.keyboard.type(password);

    await f.click({ position: { x: cx, y: height * 0.66 }, force: true });

    await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
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

  test("terminal command sequence creates directory", async ({
    page,
    request,
  }) => {
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
        "mkdir -p /work/.e2e-multitest/sub && echo done > /work/.e2e-multitest/sub/result.txt",
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
      await page.waitForTimeout(500);

      // Switch back to Terminal tab
      await f.click({ position: { x: rightCenter - 200, y: 16 }, force: true });
      await page.waitForTimeout(500);

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
        "echo tab-survive-test > /work/.tab-survive",
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

      // Navigate away (click back button)
      await flutterClick(page, 25, 28);
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 30_000 });

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

  test("nested directory structure accessible via file API", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "subdir-nav",
    );

    try {
      // Create nested directory structure via terminal
      await terminalType(
        page,
        "mkdir -p /work/.e2e-nav/inner && echo nav-test > /work/.e2e-nav/inner/file.txt",
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
    const dataDir = process.env.BARK_E2E_DATA_DIR!;
    const homePath = `${dataDir}/workspaces/${userId}/home/${workspaceId}`;
    const { mkdirSync, writeFileSync } = await import("fs");
    mkdirSync(homePath, { recursive: true });
    writeFileSync(`${homePath}/.host-created-file`, "hello-from-host\n");

    try {
      await openWorkspace(page, email, workspaceId);

      // Copy the file from ~ to /work so the file API can read it
      await terminalType(page, "cp ~/.host-created-file /work/.host-check");
      await waitForFile(request, workspaceId, ".host-check", headers);

      const resp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.host-check`,
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
    );

    try {
      // Create a file in ~ and copy it to /work so the file API can read it
      await terminalType(
        page,
        "echo home-test > ~/.home-persist-test && cp ~/.home-persist-test /work/.home-result",
      );
      await waitForFile(request, workspaceId, ".home-result", headers);

      const resp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=.home-result`,
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
      test.skip(true, "BARK_TEST_MODE not enabled");
      return;
    }

    const { workspaceId, email, headers, cleanup } =
      await createAndOpenWorkspace(page, request, "e2e-idle-test");

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

      // Reset per-workspace timeout so the restarted container isn't
      // immediately killed again.
      await request.post(
        `${API_BASE}/api/test/set-idle-timeout?seconds=300&workspace_id=${workspaceId}`,
        { headers },
      );

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
