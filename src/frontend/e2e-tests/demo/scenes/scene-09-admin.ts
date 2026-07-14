/**
 * Scene 9 — Administration (~25s)
 *
 * CONTINUITY: the hero (admin@example.com, a full admin) is logged in. She
 * clicks the **admin icon** in the app bar (the manage-accounts person-gear,
 * visible only for admins) → lands on the admin panel — a SEPARATE page
 * (`/admin/users`), not a workspace's Settings tab. The Users list already
 * shows the seeded cast (admin, reviewer, teammate, designer, clanker,
 * bootstrap) so it looks lived-in without on-camera setup.
 *
 * BEAT ORDER (per videoscript — nothing extra):
 *   1. Login → land on the Workspaces list (hold).
 *   2. Click the admin icon → admin panel loads on the Users tab (hold 5s).
 *   3. Click Groups (hold 5s) → Invitations (hold 5s) → Access Control (hold 5s).
 *
 * Pure mouse navigation; no state changes.
 *
 * Geometry (measured via Flutter semantics rects at 960×540, the recording
 * logical viewport — semantics is an overlay and doesn't affect layout, so
 * these coords are valid for the semantics-off recording too):
 *   - Admin icon (manage_accounts): x=880..920, y=8..48 → center (900, 28).
 *   - Admin tabs strip: 4 tabs × 240px, y=56..96 → center y=76.
 *       Users(0)=120  Groups(1)=360  Invitations(2)=600  Access Control(3)=840.
 */
import { test } from "@playwright/test";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  pace,
  mouseClick,
  demoLogin,
} from "../demo-helpers";

// App-bar admin icon (manage_accounts) — admin-only, sits between the email
// chip and the logout icon.
const ADMIN_ICON_X = 900;
const ADMIN_ICON_Y = 28;

// Admin panel sub-tabs (Users / Groups / Invitations / Access Control). 4 tabs
// evenly spaced across 960px; vertical center of the 40px-tall strip at y=56.
const ADMIN_TAB_Y = 76;
const adminTabX = (index: number) => (index + 0.5) * 240; // 120, 360, 600, 840

test("administration tour", async ({ page }) => {
  test.setTimeout(120_000);

  // 1. Login → land on the Workspaces list. Hold so the viewer sees the list.
  await demoLogin(page, DEMO_HERO_EMAIL, DEMO_HERO_PASSWORD);
  await pace(3000);

  // 2. Click the admin icon (app bar, top-right) → admin panel. Wait for
  //    the route to mount via the hash (the admin page doesn't update
  //    document.title, so we can't wait on the title like demoLogin does).
  await mouseClick(page, ADMIN_ICON_X, ADMIN_ICON_Y);
  await page.waitForFunction(
    () => window.location.hash.includes("admin/users"),
    { timeout: 15_000 },
  );
  await pace(1500); // let the Users list render
  await pace(5000); // Users tab (default) — viewer reads the seeded user list

  // 3. Groups tab.
  await mouseClick(page, adminTabX(1), ADMIN_TAB_Y);
  await pace(5000);

  // 4. Invitations tab.
  await mouseClick(page, adminTabX(2), ADMIN_TAB_Y);
  await pace(5000);

  // 5. Access Control tab.
  await mouseClick(page, adminTabX(3), ADMIN_TAB_Y);
  await pace(5000);
});
