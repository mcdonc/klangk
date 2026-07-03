/**
 * Scene 9 — Administration (~25s)
 *
 * CONTINUITY: the hero (admin@example.com, an admin-role user) opens HER OWN
 * `demo` workspace and goes to its Settings tab — which, for an admin, IS the
 * admin panel. The Users list already shows the seeded cast
 * (teammate/designer/reviewer) plus the hero, so it looks lived-in without
 * on-camera setup. Same shared `demo` workspace as Sc 4–7.
 *
 * Pure mouse navigation; no state changes.
 *
 * TODO: the Users/Groups/Invitations sub-nav coordinates aren't measured yet,
 * so this scene opens Settings and holds. Measure them to extend the tour.
 */
import { test } from "@playwright/test";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  SHARED_WORKSPACE,
  pace,
  apiLogin,
  ensureSharedWorkspace,
  openWorkspaceDemo,
  openTab,
} from "../demo-helpers";

test("administration tour", async ({ page, request }) => {
  test.setTimeout(180_000);

  // 1. Ensure the shared `demo` workspace exists (continuity).
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);

  // 2. Open the workspace (hero is an admin; the Settings tab is the admin
  //    panel for admins). Wait for the terminal to mount so the container is up.
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD, {
    waitForTerminal: true,
    holdOnListMs: 2500,
  });

  // 3. Settings tab (index 4) — the admin panel for an admin user. Hold while
  //    narrating Users/Groups, Invitations, OIDC SSO, single-port nginx.
  await openTab(page, 4); // Settings
  await pace(4000);
});
