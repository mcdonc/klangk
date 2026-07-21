import { test, expect } from "@playwright/test";

// Dist smoke test (#1611) — runs only in release.yml's `dist-smoke-test`
// job against a klangkd started from an installed wheel (not editable, not
// the devenv source tree). Proves exactly one thing: the frontend assets
// shipped in the wheel are complete enough for the Flutter app to boot and
// render the login page through the real nginx → UDS → uvicorn path.
//
// Catches, before images are pushed + the GitHub release is created:
//   - Frontend not included in wheel (404 / blank page)
//   - _DEFAULT_FRONTEND_DIR resolves wrong post-install (same)
//   - Flutter build broken / incomplete (flutter-view never attaches)
//   - main.dart.js missing or corrupt (engine doesn't boot)
//   - klangkd entrypoint registration broken (server never starts — caught
//     by the workflow's /health poll before Playwright runs)
//   - nginx not found or config render broken (nginx refuses to start)
//   - UDS proxy_pass misconfigured (nginx 502s)
//   - location / missing in full template (static files not served)
//
// Deliberately minimal: one test, one browser (chromium), no app-level
// interactions. The full e2e suite (frontend-e2e-tests.yml) covers
// cross-browser + workspace lifecycle; this is the release-gate smoke.

test.describe("Dist smoke (installed wheel)", () => {
  test("login page renders through nginx → UDS → uvicorn", async ({ page }) => {
    await page.goto("/");

    // Flutter Web shows "Loading, please wait" until main.dart.js has
    // booted the engine. Wait for that text to clear.
    await page.waitForFunction(
      () => !document.body.textContent?.includes("Loading, please wait"),
      { timeout: 90_000 },
    );

    // The engine attaches <flutter-view> once the first frame is ready.
    // Mirrors the e2e suite's waitForFlutter() helper.
    await page.waitForSelector("flutter-view", { timeout: 10_000 });

    // The page title is "Login" only when the auth form is the rendered
    // route — so a non-Login title here means either the Flutter app
    // didn't route correctly or the auth config endpoint is broken.
    await expect(page).toHaveTitle(/Login/i);
  });
});
