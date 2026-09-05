import { test, expect, Page } from "@playwright/test";
import {
  API_BASE,
  createAndOpenWorkspace,
  seedFile,
  openFilesTab,
  clickFileRow,
  terminalType,
} from "./helpers";

// #3219 — runtime CSP / Trusted Types enforcement. The served policy drops
// 'unsafe-inline' from script-src (index.html's inline scripts ride SHA-256
// hash tokens) and adds require-trusted-types-for 'script'. This spec drives
// the acceptance flows (login, workspace page, terminal, file upload, file
// view, download, PDF viewer) and asserts the browser console stays clean of
// CSP and Trusted Types violations.
//
// #3228 — fully self-contained frontend. The engine's CanvasKit renderer
// used to lazily fetch per-script Noto fallback fonts from
// fonts.gstatic.com whenever a frame rasterized a codepoint the bundled
// fonts lack; the whole set is now vendored under
// assets/assets/fallback-fonts/ and web/flutter_bootstrap.js points the
// engine's fontFallbackBaseUrl there, so fallbacks resolve same-origin.
// The known-noise filter that #3219 carried for those blocked fetches is
// gone: any external-origin request — attempted or successful, for fonts,
// JS, CSS, or images — fails the suite, and the terminal flow renders CJK
// output so the fallback chain provably fires during the run.

function watchViolations(page: Page, sink: string[]) {
  // "Trusted" covers Trusted Types, TrustedScriptURL, and TrustedHTML —
  // Chromium's TT sink message is "This document requires
  // 'TrustedScriptURL' assignment. The action has been blocked.", which
  // none of the older alternatives match.
  const pattern =
    /Content Security Policy|Refused to|Trusted|trustedTypes|violates|CSP/;
  page.on("console", (msg) => {
    if (msg.type() === "error" && pattern.test(msg.text())) {
      sink.push(`console: ${msg.text()}`);
    }
    // Engine font-fallback failures (404s / permanent unavailability)
    // mean a codepoint silently degraded to tofu — visible only as a
    // warning line, so watch for the phrases too.
    if (
      (msg.type() === "warning" || msg.type() === "error") &&
      /permanently unavailable|not found \(404\)|Failed to load font/.test(
        msg.text(),
      )
    ) {
      sink.push(`font: ${msg.text()}`);
    }
  });
  page.on("pageerror", (err) => {
    if (pattern.test(String(err))) {
      sink.push(`pageerror: ${err}`);
    }
  });
}

// Records every request whose origin differs from the page origin (#3228).
// Attach before the first navigation: playwright reports attempted requests
// even when the CSP blocks them, so an attempted external fetch fails the
// suite exactly like a successful one. blob:/data: URLs are in-process
// artifacts, and same-host upgrades (ws:) are the workspace WebSocket.
// The origin comes from API_BASE, not page.url() — at attach time the page
// is still about:blank.
function watchExternalRequests(page: Page, sink: string[]) {
  const selfOrigin = new URL(API_BASE).origin;
  page.on("request", (req) => {
    const url = req.url();
    if (url.startsWith("blob:") || url.startsWith("data:")) return;
    let origin: string;
    try {
      origin = new URL(url).origin;
    } catch {
      return;
    }
    if (origin !== selfOrigin) {
      sink.push(url);
    }
  });
}

// True when a vendored fallback-font part was served from this origin —
// proves the engine's fallback chain resolved locally (it always fetches
// Roboto at boot, so this is deterministic even before any CJK renders).
async function fetchedFallbackFont(page: Page): Promise<boolean> {
  return page.evaluate(() =>
    performance
      .getEntriesByType("resource")
      .some((e) => e.name.includes("/assets/assets/fallback-fonts/")),
  );
}

// A minimal single-page PDF (same fixture as the pdf file-viewer spec).
const PDF = Buffer.from(
  "%PDF-1.1\n" +
    "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n" +
    "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n" +
    "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R>>endobj\n" +
    "4 0 obj<</Length 44>>stream\n1 0 0 1 20 80 cm BT (hi) Tj ET\nendstream endobj\n" +
    "trailer<</Root 1 0 R>>\n%%EOF\n",
  "latin1",
);

test.describe("CSP / Trusted Types (#3219)", () => {
  test("login → workspace → terminal → files → pdf → download, zero violations, zero external fetches", async ({
    page,
    request,
  }) => {
    const violations: string[] = [];
    watchViolations(page, violations);
    watchExternalRequests(page, violations);
    if (process.env.KL_CSP_DEBUG) {
      page.on("console", (m) =>
        console.log(`[console.${m.type()}]`, m.text().slice(0, 300)),
      );
      page.on("pageerror", (e) =>
        console.log("[pageerror]", String(e).slice(0, 300)),
      );
    }

    // Positive pdfium signals: count Worker constructions (pdfrx builds
    // its wasm worker from a blob: URL — a TrustedScriptURL sink that TT
    // enforcement rejects on a regression), so the spec proves the viewer
    // came up, not merely that nothing was logged.
    await page.addInitScript(() => {
      // Keep every resource-timing entry: the default 250-entry buffer
      // overflows in a full session and would evict the early font
      // fetches the #3228 assertion reads below.
      performance.setResourceTimingBufferSize(10000);
      (window as any).__workerCount = 0;
      const OrigWorker = window.Worker;
      class CountingWorker extends OrigWorker {
        constructor(scriptUrl: string | URL, options?: WorkerOptions) {
          super(scriptUrl, options);
          (window as any).__workerCount += 1;
        }
      }
      Object.defineProperty(window, "Worker", {
        configurable: true,
        writable: true,
        value: CountingWorker,
      });
    });

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "csp-tt",
      { waitForTerminal: true },
    );
    try {
      // Terminal: run a command through the flterm canvas. The cat'd CJK
      // file makes the engine's font fallback chain fire for codepoints
      // the bundled fonts lack (#3228): with the vendored set they resolve
      // same-origin, and a regression back to fonts.gstatic.com is a hard
      // CSP-violation failure above. (Seeding the text and cat-ing it
      // avoids keyboard-event quirks with non-ASCII input.)
      await seedFile(
        request,
        workspaceId,
        "/home/klangk/cjk.txt",
        "中文测试 … ✓\n",
        headers,
        "text/plain",
      );
      await terminalType(page, "echo '中文测试 … ✓' && cat ~/cjk.txt");
      await page.waitForTimeout(1500);

      // #3228 (checked before the pdf deep-link reload below — a reload
      // starts a fresh document whose resource buffer loses these): the
      // fallback chain resolved from the vendored same-origin parts.
      await expect
        .poll(() => fetchedFallbackFont(page), { timeout: 30_000 })
        .toBe(true);

      // File upload (API) + text-file view in the UI.
      await seedFile(
        request,
        workspaceId,
        "/home/klangk/notes.txt",
        "hello csp\n",
        headers,
        "text/plain",
      );
      await seedFile(
        request,
        workspaceId,
        "/home/klangk/doc.pdf",
        PDF,
        headers,
        "application/pdf",
      );
      await openFilesTab(page);
      await page.waitForTimeout(500);
      await clickFileRow(page, 0); // notes.txt viewer
      await page.waitForTimeout(1000);

      // PDF viewer (deep-link into the existing viewer): pdfrx loads a
      // script tag + wasm + a blob worker — all TT/CSP-sensitive paths.
      // The ?file= param is the absolute in-workspace path (a bare name
      // 400s at files/download and the viewer never mounts).
      await page.goto(
        `/#/workspace/${workspaceId}?file=${encodeURIComponent("/home/klangk/doc.pdf")}`,
        { waitUntil: "load" },
      );
      await page.waitForTimeout(4000);

      // The two TT sinks the review found: the pdfium_client.js asset
      // must have loaded (plain-string script.src) and the wasm Worker
      // must have been constructed (blob: URL) — both fail under a
      // Trusted Types regression. The deep-link goto reloads the document
      // (playwright goto is a real navigation even for a hash URL), so
      // poll instead of sleeping a fixed budget: the unminified bundle's
      // boot + viewer mount routinely exceeds any fixed wait on a loaded
      // runner.
      await expect
        .poll(() => page.evaluate(() => (window as any).__workerCount ?? 0), {
          timeout: 60_000,
        })
        .toBeGreaterThan(0);
      await expect
        .poll(
          () =>
            page.evaluate(() =>
              performance
                .getEntriesByType("resource")
                .some((e) => e.name.includes("pdfium_client.js")),
            ),
          { timeout: 30_000 },
        )
        .toBe(true);

      // Download round-trip.
      const dl = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/download?path=${encodeURIComponent("/home/klangk/notes.txt")}`,
        { headers },
      );
      expect(dl.ok()).toBeTruthy();

      await page.waitForTimeout(1500); // let the console settle
      expect(violations.join("\n---\n") || "(none)").toBe("(none)");
    } finally {
      await cleanup();
    }
  }, 300_000);

  // #3228 acceptance: an offline (externally-blocked) session renders the
  // UI and terminal without tofu beyond what the bundled fonts cover. Every
  // non-same-origin request is aborted at the network layer — stricter
  // than the CSP, which only reports violations — and the run must stay
  // free of both external attempts and font-failure warnings while the
  // vendored fallback parts still load.
  test("offline: externally-blocked session renders UI + CJK terminal from bundled assets", async ({
    page,
    request,
  }) => {
    const violations: string[] = [];
    watchViolations(page, violations);
    const externalAttempts: string[] = [];
    const selfOrigin = new URL(API_BASE).origin;
    await page.route("**/*", (route) => {
      const url = route.request().url();
      if (
        url.startsWith("blob:") ||
        url.startsWith("data:") ||
        new URL(url).origin === selfOrigin
      ) {
        return route.continue();
      }
      externalAttempts.push(url);
      return route.abort();
    });

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "csp-off",
      { waitForTerminal: true },
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "/home/klangk/cjk.txt",
        "中文测试 … ✓\n",
        headers,
        "text/plain",
      );
      await terminalType(page, "echo '中文测试 … ✓' && cat ~/cjk.txt");
      await expect
        .poll(() => fetchedFallbackFont(page), { timeout: 30_000 })
        .toBe(true);
      expect(
        externalAttempts.length ? externalAttempts.join("\n") : "(none)",
        "external requests under a fully self-contained frontend",
      ).toBe("(none)");
      expect(violations.join("\n---\n") || "(none)").toBe("(none)");
    } finally {
      await cleanup();
    }
  }, 300_000);
});
