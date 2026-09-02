import { test, expect, Page, APIRequestContext } from "@playwright/test";
import {
  registerUser,
  createWorkspace,
  openWorkspace,
  API_BASE,
} from "./helpers";

// #3023/#3026: the Terminal-tab `terminal`-permission gate at the UI
// level. The predicate (terminal_tab_gate.dart) and the null-pane
// behavior (ide_layout.dart) are unit-tested; this spec exercises the
// path a regression would actually take — workspace_page.dart passing
// the /api/v1/my-permissions list into the gate. A wrong list or
// reverted conditional there leaves every unit test green while the tab
// mounts (or not) for the wrong members.

/** Grant `permissions` to a user on a workspace by appending user ACEs to
 *  the workspace ACL via the Advanced ACL editor's PUT endpoint — the
 *  same surface a human owner would use (#3023's custom-ACL criterion).
 *  The owner's existing entries (the `*` row) are kept untouched. */
async function grantViaAcl(
  request: APIRequestContext,
  ownerHeaders: Record<string, string>,
  workspaceId: string,
  userId: string,
  permissions: string[],
) {
  const aclResp = await request.get(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/acl`,
    { headers: ownerHeaders },
  );
  expect(aclResp.ok()).toBeTruthy();
  const existing = await aclResp.json();
  const entries = [
    ...existing.map((ace: any) => ({
      action: ace.action,
      principal_type: ace.principal_type,
      permission: ace.permission,
      user_id: ace.user_id || null,
      group_id: ace.group_id || null,
      system_principal: ace.system_principal ?? null,
    })),
    // action 1 = Allow, principal_type 1 = User.
    ...permissions.map((permission) => ({
      action: 1,
      principal_type: 1,
      permission,
      user_id: userId,
      group_id: null,
      system_principal: null,
    })),
  ];
  const putResp = await request.put(
    `${API_BASE}/api/v1/workspaces/${workspaceId}/acl`,
    { headers: ownerHeaders, data: entries },
  );
  expect(putResp.ok()).toBeTruthy();
}

/** Turn on Flutter's semantics tree so the canvas-rendered tab labels
 *  become visible to text locators. openWorkspace clicks the a11y
 *  invoker when it is already up — the early return covers that case;
 *  the invoker click covers the race where it attached too late for
 *  that (the prompt re-appears a few seconds after each route load). */
async function ensureSemantics(page: Page) {
  if (
    await page
      .getByText("Files", { exact: true })
      .first()
      .isVisible()
      .catch(() => false)
  ) {
    return;
  }
  const btn = page.getByRole("button", { name: /^Enable accessibility$/i });
  await btn.waitFor({ state: "attached", timeout: 30_000 });
  await btn.evaluate((el) => (el as HTMLElement).click());
  await page.waitForTimeout(500);
}

/** Owner + workspace + a genuine non-admin member whose ONLY grants are
 *  `permissions`, as direct user ACEs — no role group, so none of the
 *  default buckets' incidental `terminal` leaks in. The only delta
 *  between the two tests below is one ACE. */
async function setupMemberWithGrants(
  request: APIRequestContext,
  permissions: string[],
): Promise<{
  memberEmail: string;
  workspaceId: string;
  cleanup: () => Promise<void>;
}> {
  const ownerEmail = `term-gate-owner-${Date.now()}@test.example.com`;
  const owner = await registerUser(request, ownerEmail);
  const { workspaceId, cleanup } = await createWorkspace(
    request,
    owner.headers,
    "term-gate",
  );
  const memberEmail = `term-gate-member-${Date.now()}@test.example.com`;
  const member = await registerUser(request, memberEmail, { admin: false });
  expect(member.userId).toBeTruthy();
  await grantViaAcl(
    request,
    owner.headers,
    workspaceId,
    member.userId!,
    permissions,
  );
  return { memberEmail, workspaceId, cleanup };
}

test.describe("Terminal tab permission gate (#3023)", () => {
  test("files-only member (join-workspace + files-view) sees no Terminal tab", async ({
    page,
    request,
  }) => {
    const { memberEmail, workspaceId, cleanup } = await setupMemberWithGrants(
      request,
      ["join-workspace", "files-view"],
    );
    try {
      // join-workspace alone is the connect gate (#2975): the page
      // renders and the container starts with no terminal grant.
      await openWorkspace(page, memberEmail, workspaceId);
      await ensureSemantics(page);

      // The Files tab renders — and proves the permission list arrived,
      // so the absence assertion below is not just "too early to tell".
      await expect(
        page.getByText("Files", { exact: true }).first(),
      ).toBeVisible({ timeout: 15_000 });

      // No `terminal` grant → no Terminal tab in the strip. "Terminal"
      // appears nowhere else in the app (only the tab label).
      await expect(page.getByText("Terminal", { exact: true })).toHaveCount(0);
    } finally {
      await cleanup();
    }
  });

  test("terminal-holding member still sees the Terminal tab", async ({
    page,
    request,
  }) => {
    const { memberEmail, workspaceId, cleanup } = await setupMemberWithGrants(
      request,
      ["join-workspace", "files-view", "terminal"],
    );
    try {
      await openWorkspace(page, memberEmail, workspaceId);
      await ensureSemantics(page);

      // The identical setup plus one `terminal` ACE mounts the tab —
      // the permission is the only variable.
      await expect(
        page.getByText("Terminal", { exact: true }).first(),
      ).toBeVisible({ timeout: 15_000 });
      await expect(
        page.getByText("Files", { exact: true }).first(),
      ).toBeVisible({ timeout: 15_000 });
    } finally {
      await cleanup();
    }
  });
});
