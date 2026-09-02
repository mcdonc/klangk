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
 *  become visible to text locators. The invoker is a zero-sized
 *  <flt-semantics-placeholder role=button> — not a <button> tag, and with
 *  no text content — so nothing in the login path can enable semantics
 *  for us: resolve it by role and click it (the password-history /
 *  workspace-export pattern). Deciding on the flt-semantics node count,
 *  not on a tab label, keeps this race-free: a populated tree means
 *  semantics are on regardless of mount timing (the tab-strip wait is
 *  the test's own assertion), an empty one means the invoker is still
 *  there to click. */
async function ensureSemantics(page: Page) {
  const populated = () =>
    page.evaluate(() => document.querySelectorAll("flt-semantics *").length);
  if ((await populated()) > 0) return;
  const btn = page.getByRole("button", { name: /^Enable accessibility$/i });
  await btn.waitFor({ state: "attached", timeout: 30_000 });
  await btn.evaluate((el) => (el as HTMLElement).click());
  await expect
    .poll(populated, {
      timeout: 15_000,
      message: "flt-semantics tree populated",
    })
    .toBeGreaterThan(0);
}

/** Owner + workspace + a genuine non-admin member whose ONLY grants are
 *  `permissions`, as direct user ACEs — no role group, so none of the
 *  default buckets' incidental `terminal` leaks in. The only delta
 *  between the two tests below is one ACE. `tag` keeps the per-test
 *  email prefixes distinct: the tests run in parallel and a shared
 *  prefix plus a same-millisecond Date.now() would collide on
 *  registration (duplicate email → hard setup failure). */
async function setupMemberWithGrants(
  request: APIRequestContext,
  tag: string,
  permissions: string[],
): Promise<{
  memberEmail: string;
  workspaceId: string;
  cleanup: () => Promise<void>;
}> {
  const ownerEmail = `term-gate-${tag}-owner-${Date.now()}@test.example.com`;
  const owner = await registerUser(request, ownerEmail);
  const { workspaceId, cleanup } = await createWorkspace(
    request,
    owner.headers,
    "term-gate",
  );
  const memberEmail = `term-gate-${tag}-member-${Date.now()}@test.example.com`;
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
      "files-only",
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
      "term-holder",
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
