import { test, expect } from "@playwright/test";
import {
  registerUser,
  createWorkspace,
  openWorkspace,
  API_BASE,
} from "./helpers";

test.describe("shared workspace name in app bar", () => {
  test("displays the workspace name for a shared workspace member", async ({
    page,
    request,
  }) => {
    // Owner creates a workspace with a distinctive name
    const ownerEmail = `share-name-owner-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const wsName = `SharedNameTest-${Date.now()}`;
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      wsName,
    );

    try {
      // Register a second user and share the workspace with them
      const memberEmail = `share-name-member-${Date.now()}@test.example.com`;
      await registerUser(request, memberEmail);
      const addResp = await request.post(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/members`,
        { headers: owner.headers, data: { email: memberEmail } },
      );
      expect(addResp.ok()).toBe(true);

      // Member opens the shared workspace
      await openWorkspace(page, memberEmail, workspaceId);

      // The page title should contain the workspace name, not just "Workspace"
      await expect(page).toHaveTitle(new RegExp(wsName), { timeout: 15_000 });
    } finally {
      await cleanup();
    }
  });
});
