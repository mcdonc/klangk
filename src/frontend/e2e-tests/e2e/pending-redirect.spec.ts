import { test, expect } from "@playwright/test";
import {
  TEST_PASSWORD,
  registerUser,
  loginViaUI,
  loginOnCurrentPage,
  waitForFlutter,
  flutterClick,
  vp,
} from "./helpers";

// Regression tests for #2670: the post-login "next URL" (pendingRedirect)
// must never outlive its session. A URL stashed while logged out must not
// redirect a different — possibly non-admin — user to an admin page after
// login; the new session falls back to /workspaces instead.
//
// pendingRedirect is an in-memory global, so the stash must happen inside
// the same page instance that then logs in — never reload between the two
// (loginOnCurrentPage exists for exactly that reason).
test.describe("post-login redirect (pendingRedirect, #2670)", () => {
  test("non-admin login does not inherit a stashed admin URL", async ({
    page,
    request,
  }) => {
    // A genuine non-admin — the stashed target (/admin/users) is off
    // limits to this session.
    const email = `stale-redir-${Date.now()}@test.example.com`;
    await registerUser(request, email, { admin: false });

    // Visit a protected admin route while logged out: the app stashes the
    // destination and shows the login form.
    await page.goto("/#/admin/users");
    await waitForFlutter(page);
    await expect(page).toHaveURL(/#\/login/, { timeout: 10_000 });

    // Log in as the non-admin user on the already-rendered form (no
    // reload — it would reset the in-memory stash under test).
    await loginOnCurrentPage(page, email, TEST_PASSWORD);

    // The new session must land on /workspaces, not /admin/users.
    await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
    await expect(page).toHaveURL(/#\/workspaces/, { timeout: 10_000 });
  });

  test("logout discards the previous session's destination", async ({
    page,
    request,
  }) => {
    const adminEmail = `stale-admin-${Date.now()}@test.example.com`;
    const userEmail = `stale-user-${Date.now()}@test.example.com`;
    await registerUser(request, adminEmail);
    await registerUser(request, userEmail, { admin: false });

    // Admin session browsing an admin page.
    await loginViaUI(page, adminEmail, TEST_PASSWORD);
    await page.goto("/#/admin/users");
    await waitForFlutter(page);
    await expect(page).toHaveURL(/#\/admin\/users/, { timeout: 10_000 });

    // Log out from the app bar (rightmost icon).
    const { width } = vp(page);
    await flutterClick(page, width - 25, 28);
    await expect(page).toHaveURL(/#\/login/, { timeout: 30_000 });

    // While logged out, hit the protected route again — the app stashes
    // it (fresh page instance, so the reload is fine here) and asks for
    // login.
    await page.goto("/#/admin/users");
    await waitForFlutter(page);
    await expect(page).toHaveURL(/#\/login/, { timeout: 10_000 });

    // A different, non-admin user logs in on the same browser: the
    // previous session's destination must not carry over.
    await loginOnCurrentPage(page, userEmail, TEST_PASSWORD);
    await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
    await expect(page).toHaveURL(/#\/workspaces/, { timeout: 10_000 });
  });
});
