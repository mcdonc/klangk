/**
 * Scene 10 — Administration (~25s)
 *
 * Tour the admin panel as the seeded admin: open the workspace, go to the
 * Settings tab (the admin panel for an admin user), and hold while narration
 * covers Users & Groups, Invitations, OIDC SSO (Google/GitHub/IdP), and the
 * single-port (8996) nginx setup. Pure mouse navigation; no state changes.
 *
 * Uses the seeded admin (KLANGK_DEMO_ADMIN_EMAIL / KLANGK_DEFAULT_USER), so the
 * Users list already shows the seeded virtual users (teammate/designer/reviewer)
 * — looks lived-in without any on-camera setup. NOTE: the admin's password is
 * the server-seeded one (DEMO_ADMIN_PASSWORD), NOT the demo-account password.
 *
 * TODO: the Users/Groups/Invitations sub-nav coordinates aren't measured yet,
 * so this scene opens Settings and holds. Measure them to extend the tour.
 */
import { test } from "@playwright/test";
import {
  DEMO_ADMIN_EMAIL,
  DEMO_ADMIN_PASSWORD,
  adminLogin,
  pace,
  ensureFreshWorkspace,
  waitForFlutter,
  demoLogin,
  waitForTerminal,
  openTab,
} from "../demo-helpers";

test("administration tour", async ({ page, request }) => {
  test.setTimeout(180_000);

  // 1. Create a throwaway workspace via the admin API (so we land somewhere
  //    after login). The admin already exists — do NOT ensureUser it (its
  //    password is the server-seeded one, not the demo password).
  const { headers } = await adminLogin(request);
  const ws = await ensureFreshWorkspace(request, headers, "admin-demo");

  // 2. Log in as the admin (lands on the Workspaces list). Hold on camera.
  await demoLogin(page, DEMO_ADMIN_EMAIL, DEMO_ADMIN_PASSWORD);
  await pace(2500);

  // 3. Open the workspace and wait for the terminal to mount.
  await page.goto(`/#/workspace/${ws.id}`, { waitUntil: "load" });
  await waitForFlutter(page);
  await waitForTerminal(page);
  await pace(1500);

  // 4. Settings tab (index 4) — the admin panel for an admin user. Hold while
  //    narrating Users/Groups, Invitations, OIDC SSO, single-port nginx.
  await openTab(page, 4); // Settings
  await pace(4000);
});
