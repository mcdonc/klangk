import { test, expect, Page } from "@playwright/test";
import {
  API_BASE,
  TEST_PASSWORD,
  loginViaUI,
  registerUser,
  waitForFlutter,
} from "./helpers";

// Password-reuse gate (#2582), end to end through the real UI: the settings
// page's Change Password form drives /api/v1/auth/change-password against a
// server started with KLANGKD_PASSWORD_HISTORY_COUNT=3 (global-setup).
//
// Flutter Web renders to canvas, so plain locators don't see the form. We
// enable the accessibility tree (the "Enable accessibility" overlay button),
// which exposes the form fields as labeled semantic inputs, then type into
// them and click the real Update Password button. The gate itself is
// verified via the API side-channel (which password still authenticates) —
// asserting on rendered error text would just re-test Flutter, not the gate.

/** Enable Flutter's semantics tree so the form fields become DOM inputs.
 *
 *  The prompt is a <flt-semantics-placeholder role=button> (not a real
 *  <button>), typically tiny/offscreen — so force-click it by role. */
async function enableSemantics(page: Page) {
  const fields = page.getByLabel("Current Password");
  if ((await fields.count()) > 0) return; // already enabled

  const btn = page.getByRole("button", { name: "Enable accessibility" });
  await btn.waitFor({ state: "attached", timeout: 10_000 });
  // Zero-sized element — no click point; dispatch the event directly
  // (Flutter listens on capture, so a synthetic event works).
  await btn.dispatchEvent("click");
  await page.waitForTimeout(500);
  await expect(page.getByLabel("Current Password")).toBeVisible({
    timeout: 5_000,
  });
}

/** Fill one Flutter semantic input, retrying until the framework
 *  accepts the edit — Flutter can drop an input event when the
 *  semantics tree rebuilds under load (the DOM value snaps back to the
 *  controller's text). Without this, a field silently stays empty and
 *  the form's client-side validation swallows the submit. */
async function fillSticking(
  page: Page,
  field: import("@playwright/test").Locator,
  text: string,
  label: string,
) {
  for (let attempt = 0; attempt < 6; attempt++) {
    await field.fill(text);
    await page.waitForTimeout(150);
    const value = await field.inputValue();
    if (value === text) return;
    console.log(
      `fill of ${label} bounced (${JSON.stringify(value)}), retrying`,
    );
  }
  throw new Error(
    `fill of ${label} never stuck: ${JSON.stringify(await field.inputValue())}`,
  );
}

async function submitChange(
  page: Page,
  current: string,
  next: string,
): Promise<number> {
  const currentField = page.getByLabel("Current Password", {
    exact: true,
  });
  const newField = page.getByLabel("New Password", { exact: true });
  const confirmField = page.getByLabel("Confirm New Password");
  // fill() sets the value + input event — the supported way to drive
  // Flutter semantic inputs (keystroke-level select-all does not work
  // there; typing would append to the previous attempt's text).
  await fillSticking(page, currentField, current, "current");
  await fillSticking(page, newField, next, "new");
  await fillSticking(page, confirmField, next, "confirm");
  // Wait for the POST's response rather than sleeping: under parallel
  // workers the server round-trip (up to count+1 PBKDF2 verifies plus
  // SQLite contention) can exceed any fixed delay.
  const [resp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().endsWith("/api/v1/auth/change-password") &&
        r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Update Password" }).click(),
  ]);
  // Wait out the client-side aftermath before the next submit: the
  // button stays disabled (_changing) and the semantics tree rebuilds
  // (verdict text) briefly after the response arrives — clicking or
  // filling in that window silently loses the submit.
  await expect(
    page.getByRole("button", { name: "Update Password" }),
  ).toBeEnabled({ timeout: 5_000 });
  await expect(
    page
      .getByText(
        /Password updated\.|choose a different one|Passwords do not match/,
      )
      .first(),
  ).toBeVisible({ timeout: 5_000 });
  await page.waitForTimeout(300);
  return resp.status();
}

async function apiLoginStatus(
  request: import("@playwright/test").APIRequestContext,
  email: string,
  password: string,
): Promise<number> {
  const resp = await request.post(`${API_BASE}/api/v1/auth/login`, {
    data: { identifier: email, password },
  });
  return resp.status();
}

test.describe("Password reuse gate (#2582)", () => {
  test("settings page change-password enforces history", async ({
    page,
    request,
  }) => {
    const email = `pw-history-${Date.now()}@test.example.com`;
    const newPassword = "brand-new-pass-1";
    await registerUser(request, email);

    // The gate is advertised on /api/v1/config.
    const config = await request.get(`${API_BASE}/api/v1/config`);
    expect(config.ok()).toBeTruthy();
    expect((await config.json()).password_history_count).toBe(3);

    // Log in through the UI, open settings, then force a full reload —
    // loginViaUI dismisses the a11y prompt, and a hash-only navigation
    // wouldn't re-show it. After the reload the prompt is back; enable
    // semantics so the form fields become labeled DOM inputs.
    await loginViaUI(page, email, TEST_PASSWORD);
    await page.goto("/#/settings");
    await expect(page).toHaveTitle(/Settings/i, { timeout: 10_000 });
    await page.reload();
    await waitForFlutter(page);
    await enableSemantics(page);

    // 1. Reusing the current password is rejected; the password is
    //    unchanged afterwards (old password still authenticates).
    expect(await submitChange(page, TEST_PASSWORD, TEST_PASSWORD)).toBe(400);
    expect(await apiLoginStatus(request, email, TEST_PASSWORD)).toBe(200);

    // 2. A novel password change succeeds through the UI.  The server
    //    revokes all sessions on password change (#3152), so the client
    //    is kicked back to login — re-login and navigate to settings.
    expect(await submitChange(page, TEST_PASSWORD, newPassword)).toBe(200);
    expect(await apiLoginStatus(request, email, newPassword)).toBe(200);

    await loginViaUI(page, email, newPassword);
    await page.goto("/#/settings");
    await expect(page).toHaveTitle(/Settings/i, { timeout: 10_000 });
    await page.reload();
    await waitForFlutter(page);
    await enableSemantics(page);

    // 3. Changing back to the just-retired password is rejected — the
    //    new one is still current, the old one no longer works.
    expect(await submitChange(page, newPassword, TEST_PASSWORD)).toBe(400);
    expect(await apiLoginStatus(request, email, newPassword)).toBe(200);
    expect(await apiLoginStatus(request, email, TEST_PASSWORD)).toBe(401);
  });
});
