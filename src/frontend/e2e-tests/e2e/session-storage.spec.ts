import { test, expect } from "@playwright/test";
import {
  registerUser,
  loginViaUI,
  waitForFlutter,
  TEST_PASSWORD,
} from "./helpers";

// #3193: the session JWT must live in sessionStorage (dies with the
// tab/browser), never localStorage (survives browser close). These specs
// pin the web token-store behavior the VM unit suite cannot reach — the
// widget tests exercise the SharedPreferences stub, while this is the
// production path.
test.describe("session token storage (#3193)", () => {
  test("login stores the JWT in sessionStorage, not localStorage", async ({
    page,
    request,
  }) => {
    const email = `ss-place-${Date.now()}@test.example.com`;
    await registerUser(request, email, { admin: false });

    await loginViaUI(page, email, TEST_PASSWORD);

    // Raw JWT in sessionStorage (not the JSON-quoted form the old
    // shared_preferences persistence wrote).
    const ssToken = await page.evaluate(() =>
      sessionStorage.getItem("klangk_jwt"),
    );
    expect(ssToken).toBeTruthy();
    expect(ssToken!.startsWith("eyJ")).toBe(true);

    // Nothing token-shaped may persist in localStorage.
    const lsKeys = await page.evaluate(() => Object.keys(localStorage));
    expect(lsKeys.some((k) => k.toLowerCase().includes("jwt"))).toBe(false);
    expect(
      await page.evaluate(() => localStorage.getItem("flutter.klangk_jwt")),
    ).toBeNull();
  });

  test("legacy localStorage token is migrated and scrubbed on load", async ({
    page,
    request,
  }) => {
    const email = `ss-migrate-${Date.now()}@test.example.com`;
    const { token } = await registerUser(request, email, { admin: false });

    // Seed the exact form an older build left behind: the shared_preferences
    // web backend stores values JSON-encoded under the flutter. prefix.
    await page.goto("/");
    await waitForFlutter(page);
    await page.evaluate((t) => {
      localStorage.setItem("flutter.klangk_jwt", JSON.stringify(t));
    }, token);
    await page.reload();
    await waitForFlutter(page);

    // The migrated token restores the session…
    await expect(page).toHaveTitle(/Workspaces/i, { timeout: 30_000 });
    // …in decoded (raw JWT) form, in sessionStorage…
    const ssToken = await page.evaluate(() =>
      sessionStorage.getItem("klangk_jwt"),
    );
    expect(ssToken).toBeTruthy();
    expect(ssToken!.startsWith("eyJ")).toBe(true);
    // #3218: the startup heal BINDS the migrated legacy token — the
    // stored JWT is now the DPoP-bound replacement (payload carries
    // cnf.jkt), not the raw seeded token.
    const payload = JSON.parse(
      Buffer.from(ssToken!.split(".")[1], "base64").toString(),
    );
    expect(payload.cnf?.jkt).toBeTruthy();
    // …and the persistent copy is gone.
    expect(
      await page.evaluate(() => localStorage.getItem("flutter.klangk_jwt")),
    ).toBeNull();
  });

  test("a fresh tab starts unauthenticated (session dies with the tab)", async ({
    page,
    request,
  }) => {
    const email = `ss-newtab-${Date.now()}@test.example.com`;
    await registerUser(request, email, { admin: false });
    await loginViaUI(page, email, TEST_PASSWORD);

    const tab2 = await page.context().newPage();
    await tab2.goto("/");
    await waitForFlutter(tab2);
    await expect(tab2).toHaveTitle(/Login/i, { timeout: 10_000 });
    expect(
      await tab2.evaluate(() => sessionStorage.getItem("klangk_jwt")),
    ).toBeNull();
    await tab2.close();
  });
});
