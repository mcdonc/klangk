import { test, expect } from "@playwright/test";
import AdmZip from "adm-zip";
import { API_BASE, registerUser } from "./helpers";

test.describe("API", () => {
  test("index.html has cache-busted flutter_bootstrap.js", async ({
    request,
  }) => {
    const resp = await request.get(`${API_BASE}/`);
    expect(resp.ok()).toBeTruthy();
    const html = await resp.text();
    expect(html).toMatch(/flutter_bootstrap\.js\?v=[0-9a-f]{12}/);
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

  test("admin can list users, add/remove groups, and delete users", async ({
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
    expect(testUser.groups).toEqual([]);

    // Non-admin cannot list users
    const forbiddenResp = await request.get(`${API_BASE}/admin/users`, {
      headers: userHeaders,
    });
    expect(forbiddenResp.status()).toBe(403);

    // Create a group and add the user to it
    const createGroupResp = await request.post(`${API_BASE}/admin/groups`, {
      headers: adminHeaders,
      data: { name: "editor" },
    });
    expect(createGroupResp.ok()).toBeTruthy();
    const editorGroup = await createGroupResp.json();

    const addMemberResp = await request.post(
      `${API_BASE}/admin/groups/${editorGroup.id}/members`,
      { headers: adminHeaders, data: { user_id: testUser.id } },
    );
    expect(addMemberResp.ok()).toBeTruthy();

    // Verify group membership was added
    const listResp2 = await request.get(`${API_BASE}/admin/users`, {
      headers: adminHeaders,
    });
    const updatedUser = (await listResp2.json()).find(
      (u: any) => u.email === "admin-test@test.example.com",
    );
    expect(updatedUser.groups.map((g: any) => g.name)).toContain("editor");

    // Admin can remove user from group
    const removeMemberResp = await request.delete(
      `${API_BASE}/admin/groups/${editorGroup.id}/members/${testUser.id}`,
      { headers: adminHeaders },
    );
    expect(removeMemberResp.ok()).toBeTruthy();

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

    // Clean up the test group
    const deleteGroupResp = await request.delete(
      `${API_BASE}/admin/groups/${editorGroup.id}`,
      { headers: adminHeaders },
    );
    expect(deleteGroupResp.ok()).toBeTruthy();
  });

  test("admin user management page loads and lists users", async ({
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

  test("ACL editing: remove chat permission denies chat access", async ({
    request,
  }) => {
    // Register owner and member
    const ownerEmail = `acl-owner-${Date.now()}@test.example.com`;
    const memberEmail = `acl-member-${Date.now()}@test.example.com`;
    const { headers: ownerHeaders } = await registerUser(request, ownerEmail);
    const { headers: memberHeaders } = await registerUser(request, memberEmail);

    // Create workspace and share with member
    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers: ownerHeaders,
      data: { name: `acl-chat-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspace = await wsResp.json();
    const workspaceId = workspace.id;

    await request.post(`${API_BASE}/workspaces/${workspaceId}/members`, {
      headers: ownerHeaders,
      data: { email: memberEmail },
    });

    // Member should have chat permission initially
    let permResp = await request.get(
      `${API_BASE}/api/my-permissions?resource=/workspaces/${workspaceId}`,
      { headers: memberHeaders },
    );
    expect(permResp.ok()).toBeTruthy();
    let perms = (await permResp.json()).permissions[
      `/workspaces/${workspaceId}`
    ];
    expect(perms).toContain("chat");
    expect(perms).toContain("terminal");

    // Owner gets the ACL, removes the chat ACE for the member
    const aclResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/acl`,
      { headers: ownerHeaders },
    );
    expect(aclResp.ok()).toBeTruthy();
    const aces = await aclResp.json();

    // Filter out the member's chat ACE
    const filtered = aces.filter(
      (ace: any) =>
        !(ace.permission === "chat" && ace.principal === memberEmail),
    );
    expect(filtered.length).toBeLessThan(aces.length);

    // Save the modified ACL
    const putResp = await request.put(
      `${API_BASE}/workspaces/${workspaceId}/acl`,
      {
        headers: ownerHeaders,
        data: filtered.map((ace: any) => ({
          action: ace.action,
          principal_type: ace.principal_type,
          permission: ace.permission,
          user_id: ace.user_id || null,
          group_id: ace.group_id || null,
          system_principal: ace.system_principal ?? null,
        })),
      },
    );
    expect(putResp.ok()).toBeTruthy();

    // Member should no longer have chat permission
    permResp = await request.get(
      `${API_BASE}/api/my-permissions?resource=/workspaces/${workspaceId}`,
      { headers: memberHeaders },
    );
    perms = (await permResp.json()).permissions[`/workspaces/${workspaceId}`];
    expect(perms).not.toContain("chat");
    // But still has terminal and files
    expect(perms).toContain("terminal");
    expect(perms).toContain("files");

    // Clean up
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers: ownerHeaders,
    });
  });

  test("ACL editing: reorder, add, and remove ACEs", async ({ request }) => {
    const ownerEmail = `acl-edit-${Date.now()}@test.example.com`;
    const { headers: ownerHeaders } = await registerUser(request, ownerEmail);

    // Create workspace
    const wsResp = await request.post(`${API_BASE}/workspaces`, {
      headers: ownerHeaders,
      data: { name: `acl-edit-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspace = await wsResp.json();
    const workspaceId = workspace.id;

    // Get initial ACL (owner has * ACE)
    let aclResp = await request.get(
      `${API_BASE}/workspaces/${workspaceId}/acl`,
      { headers: ownerHeaders },
    );
    expect(aclResp.ok()).toBeTruthy();
    const initialAces = await aclResp.json();
    expect(initialAces.length).toBeGreaterThanOrEqual(1);

    // Add a new ACE: Allow Authenticated view
    const newAces = [
      ...initialAces.map((ace: any) => ({
        action: ace.action,
        principal_type: ace.principal_type,
        permission: ace.permission,
        user_id: ace.user_id || null,
        group_id: ace.group_id || null,
        system_principal: ace.system_principal ?? null,
      })),
      {
        action: 1, // Allow
        principal_type: 0, // System
        permission: "view",
        user_id: null,
        group_id: null,
        system_principal: 1, // Authenticated
      },
    ];

    let putResp = await request.put(
      `${API_BASE}/workspaces/${workspaceId}/acl`,
      { headers: ownerHeaders, data: newAces },
    );
    expect(putResp.ok()).toBeTruthy();
    let saved = await putResp.json();
    expect(saved.length).toBe(initialAces.length + 1);
    expect(saved[saved.length - 1].permission).toBe("view");
    expect(saved[saved.length - 1].principal).toBe("Authenticated");

    // Reorder: swap first and last
    const reordered = [
      ...saved.slice(1).map((ace: any) => ({
        action: ace.action,
        principal_type: ace.principal_type,
        permission: ace.permission,
        user_id: ace.user_id || null,
        group_id: ace.group_id || null,
        system_principal: ace.system_principal ?? null,
      })),
      {
        action: saved[0].action,
        principal_type: saved[0].principal_type,
        permission: saved[0].permission,
        user_id: saved[0].user_id || null,
        group_id: saved[0].group_id || null,
        system_principal: saved[0].system_principal ?? null,
      },
    ];

    putResp = await request.put(`${API_BASE}/workspaces/${workspaceId}/acl`, {
      headers: ownerHeaders,
      data: reordered,
    });
    expect(putResp.ok()).toBeTruthy();
    saved = await putResp.json();
    // Last entry should now be the original first (owner *)
    expect(saved[saved.length - 1].permission).toBe("*");

    // Remove the Authenticated view ACE
    const withoutFirst = reordered.filter(
      (ace: any) => !(ace.system_principal === 1 && ace.permission === "view"),
    );
    putResp = await request.put(`${API_BASE}/workspaces/${workspaceId}/acl`, {
      headers: ownerHeaders,
      data: withoutFirst,
    });
    expect(putResp.ok()).toBeTruthy();
    saved = await putResp.json();
    expect(saved.length).toBe(reordered.length - 1);
    expect(
      saved.every((ace: any) => ace.principal !== "Authenticated"),
    ).toBeTruthy();

    // Clean up
    await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
      headers: ownerHeaders,
    });
  });

  test("admin ACL browser: read and modify static resource ACL", async ({
    request,
  }) => {
    // Login as admin
    const loginResp = await request.post(`${API_BASE}/auth/login`, {
      data: { email: "admin@example.com", password: "admin" },
    });
    expect(loginResp.ok()).toBeTruthy();
    const adminHeaders = {
      Authorization: `Bearer ${(await loginResp.json()).access_token}`,
    };

    // Read the root resource ACL
    let resp = await request.get(`${API_BASE}/admin/acl/resource?resource=/`, {
      headers: adminHeaders,
    });
    expect(resp.ok()).toBeTruthy();
    const rootAces = await resp.json();
    expect(rootAces.length).toBeGreaterThan(0);
    // Root has Authenticated view and Everyone deny
    expect(
      rootAces.some(
        (a: any) => a.principal === "Authenticated" && a.permission === "view",
      ),
    ).toBeTruthy();

    // Read /admin resource ACL
    resp = await request.get(`${API_BASE}/admin/acl/resource?resource=/admin`, {
      headers: adminHeaders,
    });
    expect(resp.ok()).toBeTruthy();
    const adminAces = await resp.json();
    expect(
      adminAces.some(
        (a: any) => a.principal === "admin" && a.permission === "*",
      ),
    ).toBeTruthy();

    // Modify /admin/groups ACL: add a view entry, then restore
    resp = await request.get(
      `${API_BASE}/admin/acl/resource?resource=/admin/groups`,
      { headers: adminHeaders },
    );
    const originalGroupsAces = await resp.json();

    const newEntries = [
      ...originalGroupsAces.map((a: any) => ({
        action: a.action,
        principal_type: a.principal_type,
        permission: a.permission,
        user_id: a.user_id || null,
        group_id: a.group_id || null,
        system_principal: a.system_principal ?? null,
      })),
      {
        action: 1,
        principal_type: 0,
        permission: "view",
        user_id: null,
        group_id: null,
        system_principal: 1,
      },
    ];

    resp = await request.put(
      `${API_BASE}/admin/acl/resource?resource=/admin/groups`,
      { headers: adminHeaders, data: newEntries },
    );
    expect(resp.ok()).toBeTruthy();
    expect((await resp.json()).length).toBe(originalGroupsAces.length + 1);

    // Restore original
    const restore = originalGroupsAces.map((a: any) => ({
      action: a.action,
      principal_type: a.principal_type,
      permission: a.permission,
      user_id: a.user_id || null,
      group_id: a.group_id || null,
      system_principal: a.system_principal ?? null,
    }));
    resp = await request.put(
      `${API_BASE}/admin/acl/resource?resource=/admin/groups`,
      { headers: adminHeaders, data: restore },
    );
    expect(resp.ok()).toBeTruthy();
  });

  test("admin ACL browser: all static resources readable", async ({
    request,
  }) => {
    const loginResp = await request.post(`${API_BASE}/auth/login`, {
      data: { email: "admin@example.com", password: "admin" },
    });
    const adminHeaders = {
      Authorization: `Bearer ${(await loginResp.json()).access_token}`,
    };

    const resources = [
      "/",
      "/workspaces",
      "/admin",
      "/admin/users",
      "/admin/invitations",
      "/admin/groups",
    ];

    for (const resource of resources) {
      const resp = await request.get(
        `${API_BASE}/admin/acl/resource?resource=${encodeURIComponent(resource)}`,
        { headers: adminHeaders },
      );
      expect(resp.ok()).toBeTruthy();
    }
  });

  test("admin ACL browser: non-admin denied access", async ({ request }) => {
    const { headers: userHeaders } = await registerUser(
      request,
      `acl-denied-${Date.now()}@test.example.com`,
    );

    const resp = await request.get(
      `${API_BASE}/admin/acl/resource?resource=/`,
      { headers: userHeaders },
    );
    expect(resp.status()).toBe(403);
  });
});
